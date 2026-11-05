"""Shared reading boundaries and delivery transactions, reusable on MySQL."""
from pathlib import Path
import tempfile
import unittest

from MaiLivingWorld.shared_reading_store import SharedReadingStore, command_text, digest

OWNER, OTHER = "12345678", "87654321"


def message(mid="m1", *, text="", uid=OWNER, sid="private-one", group=None):
    return {"platform": "qq", "message_id": mid, "session_id": sid, "account_id": "99999999",
            "processed_plain_text": text, "message_info": {"user_info": {"user_id": uid},
            "group_info": {"group_id": group} if group else None}}

RID = "reading:"+"a"*64


def reading():
    return {"id": RID, "owner_id": OWNER, "scope": "private:"+OWNER, "provider": "pixiv", "resource_type": "novel",
            "resource_id": "123", "version": "1", "source_refs": [{"id": "tool:observation:"+"b"*64,
            "digest": "c"*64, "scope": "private:"+OWNER, "sensitivity": "personal"}]}


class SharedReadingChecks:
    def command(self, db, action="start", *, units=0, allow=False, mid=None, now=100, **overrides):
        body = command_text(action, RID, units=units, allow_spoilers=allow)
        event = message(mid=mid or action+str(now), text=body)
        event.update(overrides)
        return db.command(event, owner_id=OWNER, action=action, reading=reading(), unit_count=3,
                          units=units, allow_spoilers=allow, now=now)

    def prepared(self, db):
        self.command(db)
        self.command(db, "position", units=2, now=101)
        row = db.session(OWNER, RID)
        return db.prepare(OWNER, "private-one", RID, row["revision"], 2, "d"*64, "第2段真实引用", now=102)

    def test_positions_survive_reopen_and_current_owner_is_required(self):
        db = self.store()
        row = self.prepared(db)
        another = self.store()
        self.assertEqual(another.session(OWNER, RID), row)
        self.assertIsNone(another.session(OTHER, RID))
        self.assertEqual(row["owner_units"], 2)
        self.assertEqual(row["discussed_units"], 0)

    def test_only_exact_successful_delivery_advances_mentioned_unit_once(self):
        db = self.store()
        row = self.prepared(db)
        for sid, text, rev, now in (("other", row["draft"]["text"], row["revision"], 103),
                                    ("private-one", "引用："+row["draft"]["text"], row["revision"], 103),
                                    ("private-one", row["draft"]["text"], row["revision"]-1, 103),
                                    ("private-one", row["draft"]["text"], row["revision"], 703)):
            self.assertFalse(db.record_delivery(OWNER, sid, text, "f"*64, reading_id=RID, expected_revision=rev, now=now))
        self.assertTrue(db.record_delivery(OWNER, "private-one", row["draft"]["text"], "f"*64,
            reading_id=RID, expected_revision=row["revision"], now=104))
        self.assertEqual(db.session(OWNER, RID)["discussed_units"], 2)
        self.assertFalse(db.record_delivery(OWNER, "private-one", row["draft"]["text"], "f"*64,
            reading_id=RID, expected_revision=row["revision"], now=104))

    def test_owner_rewind_and_stop_invalidate_drafts_without_erasing_history(self):
        db = self.store()
        row = self.prepared(db)
        self.assertTrue(db.record_delivery(OWNER, "private-one", row["draft"]["text"], "f"*64,
            reading_id=RID, expected_revision=row["revision"], now=103))
        self.command(db, "position", units=1, now=104)
        current = db.session(OWNER, RID)
        self.assertEqual((current["owner_units"], current["discussed_units"]), (1, 2))
        self.assertIsNone(db.prepare(OWNER, "private-one", RID, current["revision"], 2, "d"*64, "第2段", now=105))
        row = db.prepare(OWNER, "private-one", RID, current["revision"], 1, "d"*64, "第1段", now=105)
        self.command(db, "stop", now=106)
        self.assertFalse(db.record_delivery(OWNER, "private-one", "第1段", "e"*64,
            reading_id=RID, expected_revision=row["revision"], now=107))
        self.assertFalse(db.session(OWNER, RID)["allow_spoilers"])
        self.command(db, "spoilers", allow=True, now=108)
        self.assertTrue(db.session(OWNER, RID)["allow_spoilers"])

    def test_commands_dedup_do_not_replay_old_boundary_and_reject_media(self):
        db = self.store()
        self.command(db)
        self.command(db, "position", units=3, mid="old", now=101)
        self.command(db, "position", units=1, mid="new", now=102)
        self.assertFalse(self.command(db, "position", units=3, mid="old", now=103))
        self.assertEqual(db.session(OWNER, RID)["owner_units"], 1)
        with self.assertRaises(ValueError):
            self.command(db, "position", units=2, mid="old", now=104)
        with self.assertRaises(ValueError):
            self.command(db, "position", units=2, mid="media", now=104, raw_message=[{"type": "image"}])
        with self.assertRaises(ValueError):
            self.command(db, "position", units=4, now=105)

    def test_version_binding_and_stale_preparation_fail_closed(self):
        db = self.store()
        self.command(db)
        self.command(db, "position", units=1, now=101)
        row = db.session(OWNER, RID)
        self.assertIsNone(db.prepare(OWNER, "private-one", RID, row["revision"]-1, 1, "d"*64, "片段", now=102))
        changed = {**reading(), "version": "2"}
        with self.assertRaises(ValueError):
            db.command(message(mid="source", text=command_text("start", RID)), owner_id=OWNER,
                       action="start", reading=changed, unit_count=3, now=103)

    def test_clear_removes_positions_and_old_messages_do_not_restore_them(self):
        db = self.store()
        row = self.prepared(db)
        self.assertTrue(db.record_delivery(OWNER, "private-one", row["draft"]["text"], "f"*64,
            reading_id=RID, expected_revision=row["revision"], now=103))
        self.command(db, "clear", now=104)
        current = db.session(OWNER, RID)
        self.assertEqual((current["status"], current["owner_units"], current["discussed_units"], current["draft"]), ("stopped", 0, 0, None))
        self.assertFalse(current["allow_spoilers"])
        self.assertFalse(self.command(db, "position", units=2, now=101))
        self.command(db, "start", now=105)
        self.assertEqual(db.session(OWNER, RID)["owner_units"], 0)


class SharedReadingStoreTests(SharedReadingChecks, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"shared_reading.sqlite3"
        self.stores = []

    def store(self):
        value = SharedReadingStore(self.path)
        self.stores.append(value)
        return value

    def tearDown(self):
        for store in self.stores:
            store.close()
        self.temp.cleanup()
