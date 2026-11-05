"""Source-only current topic persistence and receipt compare-and-swap."""
from pathlib import Path
import tempfile
import unittest

from MaiLivingWorld.mcp_runtime import digest
from MaiLivingWorld.reference_store import ReferenceStore

OWNER, OTHER = "12345678", "87654321"


def reference(letter="a"):
    return {"id": "tool:observation:"+letter*64, "digest": "b"*64, "scope": "private:"+OWNER, "sensitivity": "personal"}


class ReferenceChecks:
    def focus(self, db, *, refs=None, now=100):
        self.assertTrue(db.capture(OWNER, digest("input"), digest("question"), now=now))
        return db.delivered(OWNER, digest("send"), digest("text"), refs or [reference()],
            expected_revision=db.state(OWNER)["revision"], input_receipt=digest("input"), ttl_seconds=3600, now=now+1)

    def test_reopen_keeps_only_source_references_and_expires_without_mutation(self):
        db = self.store()
        self.assertTrue(self.focus(db))
        reader = self.store()
        before = reader.state(OWNER, now=102)
        self.assertTrue(before["active"])
        self.assertEqual(before["focus_refs"], [reference()])
        self.assertFalse(reader.state(OWNER, now=3701)["active"])
        self.assertEqual(reader.state(OWNER, now=4000)["revision"], before["revision"])
        self.assertEqual(reader.state(OTHER, now=102)["focus_refs"], [])

    def test_new_topic_or_clear_rejects_late_send_and_input_replays(self):
        db = self.store()
        self.assertTrue(self.focus(db))
        old_revision = db.state(OWNER)["revision"]
        self.assertTrue(db.capture(OWNER, digest("clear"), digest("clear command"), now=103))
        self.assertFalse(db.delivered(OWNER, digest("late"), digest("text"), [reference()],
            expected_revision=old_revision, input_receipt=digest("input"), ttl_seconds=3600, now=104))
        self.assertFalse(db.capture(OWNER, digest("input"), digest("question"), keep_focus=True, now=105))
        self.assertEqual(db.state(OWNER)["focus_refs"], [])

    def test_duplicate_send_does_not_renew_focus_or_rewrite_new_input(self):
        db = self.store()
        self.assertTrue(self.focus(db))
        self.assertTrue(db.capture(OWNER, digest("followup"), digest("later"), keep_focus=True, now=103))
        before = db.state(OWNER, now=104)
        self.assertFalse(db.delivered(OWNER, digest("send"), digest("text"), [reference("c")],
            expected_revision=before["revision"], input_receipt=digest("followup"), ttl_seconds=3600, now=104))
        self.assertEqual(db.state(OWNER, now=104), before)

    def test_multiple_actual_sources_remain_ambiguous_and_new_input_revokes_revision(self):
        db = self.store()
        self.assertTrue(self.focus(db, refs=[reference("a"), reference("c")]))
        old = db.state(OWNER)
        self.assertTrue(db.capture(OWNER, digest("followup"), digest("later"), keep_focus=True, now=103))
        self.assertEqual(len(db.state(OWNER)["focus_refs"]), 2)
        self.assertFalse(db.delivered(OWNER, digest("stale"), digest("text"), [reference()],
            expected_revision=old["revision"], input_receipt=digest("input"), ttl_seconds=3600, now=104))

    def test_invalid_sources_are_rejected_before_writing(self):
        db = self.store()
        self.assertTrue(db.capture(OWNER, digest("input"), digest("question"), now=100))
        before = db.state(OWNER)
        for refs in ([{**reference(), "id": "projects:result:"+"a"*64}], [reference()]*4,
                     [{**reference(), "digest": "wrong"}]):
            with self.subTest(refs=refs), self.assertRaises(ValueError):
                db.delivered(OWNER, digest("bad"), digest("text"), refs, expected_revision=before["revision"],
                             input_receipt=digest("input"), ttl_seconds=3600, now=101)
        self.assertEqual(db.state(OWNER), before)

    def test_unknown_media_suspends_focus_without_discarding_or_reviving_references(self):
        db = self.store()
        self.assertTrue(self.focus(db))
        self.assertTrue(db.capture(OWNER, digest("media"), digest("unknown image"), keep_focus=True, invalidate_focus=True, now=103))
        state = self.store().state(OWNER, now=104)
        self.assertEqual(state["focus_refs"], [reference()])
        self.assertFalse(state["active"])
        db.capture(OWNER, digest("followup"), digest("later"), keep_focus=True, now=105)
        self.assertFalse(db.state(OWNER, now=106)["active"])


class ReferenceStoreTests(ReferenceChecks, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"reference.sqlite3"
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.close()
        self.temp.cleanup()

    def store(self):
        store = ReferenceStore(self.path)
        self.stores.append(store)
        return store
