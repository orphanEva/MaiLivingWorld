"""Source-only current topic persistence and receipt compare-and-swap."""
import json
from pathlib import Path
import tempfile
import unittest

from MaiLivingWorld.mcp_runtime import digest
from MaiLivingWorld.reference_store import MAX_PRIOR_MENTIONS, MENTION_TTL, ReferenceStore

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

    def test_long_term_mentions_survive_new_topic_but_clear_does_not_revive_on_replay(self):
        db = self.store()
        db.capture(OWNER, digest("input"), digest("question"), now=100)
        first = reference()
        self.assertTrue(db.delivered(OWNER, digest("sent"), digest("text"), [first], confirmed_refs=[first],
            expected_revision=1, input_receipt=digest("input"), ttl_seconds=3600, now=101))
        db.capture(OWNER, digest("next"), digest("topic change"), now=103)
        reader = self.store()
        state = reader.state(OWNER, now=104)
        self.assertEqual(state["focus_refs"], [])
        self.assertEqual(state["prior_mentions"], {first["digest"]: 101})
        self.assertEqual(reader.state(OTHER, now=104)["prior_mentions"], {})
        db.capture(OWNER, digest("clear"), digest("clear command"), clear_mentions=True, now=105)
        self.assertFalse(db.delivered(OWNER, digest("sent"), digest("text"), [first], confirmed_refs=[first],
            expected_revision=4, input_receipt=digest("clear"), ttl_seconds=3600, now=106))
        self.assertEqual(reader.state(OWNER, now=107)["prior_mentions"], {})

    def test_legacy_list_and_dict_support_focus_without_long_term_proof(self):
        db = self.store()
        self.assertTrue(self.focus(db))
        for i, payload in enumerate(([reference()], {"refs": [reference()], "mentioned_at": 101})):
            with db.con:
                db.con.execute("UPDATE reference_control SET payload=? WHERE owner_id=?", (json.dumps(payload), OWNER))
            self.assertTrue(db.capture(OWNER, digest(["neutral", i]), digest("neutral"), keep_focus=True, now=102+i))
            state = db.state(OWNER, now=104)
            self.assertEqual(state["focus_refs"], [reference()])
            self.assertEqual(state["prior_mentions"], {})
        for raw in ("null", "not JSON", json.dumps({"refs": [reference()], "mentioned_at": None,
                                                     "prior_mentions": {"a"*64: None, "b"*64: "101"}})):
            with db.con:
                db.con.execute("UPDATE reference_control SET payload=? WHERE owner_id=?", (raw, OWNER))
            state = db.state(OWNER, now=104)
            self.assertEqual(state["mentioned_at"], 0)
            self.assertFalse(state["active"])
            self.assertEqual(state["prior_mentions"], {})

    def test_long_term_mentions_limit_expiry_and_clock_rollback(self):
        db = self.store()
        db.capture(OWNER, digest("input"), digest("question"), now=100)
        refs = []
        for i in range(MAX_PRIOR_MENTIONS+1):
            ref = {**reference(), "id": "tool:observation:"+digest(["id", i]), "digest": digest(["version", i])}
            self.assertTrue(db.delivered(OWNER, digest(["send", i]), digest(["text", i]), [ref], confirmed_refs=[ref],
                expected_revision=i+1, input_receipt=digest("input"), ttl_seconds=3600, now=101+i))
            refs.append(ref)
        last_send = 101 + MAX_PRIOR_MENTIONS
        state = db.state(OWNER, now=last_send)
        self.assertEqual(set(state["prior_mentions"]), {r["digest"] for r in refs[1:]})
        self.assertEqual(db.state(OWNER, now=last_send+MENTION_TTL-1)["prior_mentions"], {refs[-1]["digest"]: last_send})
        self.assertEqual(db.state(OWNER, now=last_send+MENTION_TTL)["prior_mentions"], {})
        self.assertEqual(db.state(OWNER, now=100)["prior_mentions"], {})
        self.assertEqual(db.state(OWNER, now=last_send)["revision"], state["revision"])

    def test_later_chunk_cannot_refresh_unrepeated_history(self):
        db = self.store()
        db.capture(OWNER, digest("input"), digest("question"), now=100)
        first, second = reference(), {**reference("c"), "digest": "d"*64}
        self.assertTrue(db.delivered(OWNER, digest("one"), digest("text one"), [first], confirmed_refs=[first],
            expected_revision=1, input_receipt=digest("input"), ttl_seconds=3600, now=101))
        self.assertTrue(db.delivered(OWNER, digest("two"), digest("text two"), [first, second], confirmed_refs=[second],
            expected_revision=2, input_receipt=digest("input"), ttl_seconds=3600, now=110))
        self.assertEqual(db.state(OWNER, now=111)["prior_mentions"], {first["digest"]: 101, second["digest"]: 110})


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
