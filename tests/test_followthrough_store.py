"""Restart, lease, bounded polling and actual delivery invariants."""
from pathlib import Path
import tempfile
import unittest

from MaiLivingWorld.followthrough_store import FollowthroughStore, MAX_AGE, digest

OWNER, OTHER, TASK, SOURCE = "12345678", "87654321", "a"*32, "b"*64


class FollowthroughChecks:
    def draft(self, db, *, now=100, task=TASK, sid="private-one"):
        db.bind_source("cap", task, owner_id=OWNER, scope="private:"+OWNER,
                       operation="jm_start_download", source_signature=SOURCE, now=now)
        return db.draft(OWNER, sid, "cap", task, "jm_start_download", "c"*64, SOURCE, now=now)

    def activate(self, db, row, *, now=101, receipt="d"*64):
        self.assertTrue(db.record_delivery(OWNER, row["sid"], row["promise_text"], receipt,
                         item_id=row["id"], expected_revision=row["revision"], now=now))
        return db.get(OWNER, row["id"])

    def ready(self, db):
        row = self.activate(db, self.draft(db))
        claim = db.claim_poll(OWNER, day_start=0, now=102)
        self.assertEqual(row["id"], claim["id"])
        self.assertTrue(db.finish_poll(OWNER, claim["id"], claim["lease_token"], claim["revision"], "succeeded", now=103))
        return db.get(OWNER, row["id"])

    def test_real_receipt_is_only_activation_and_reporting_transition(self):
        db = self.store()
        row = self.draft(db)
        self.assertIsNone(db.claim_poll(OWNER, day_start=0, now=101))
        for index, text in enumerate(("“"+row["promise_text"]+"”", "不要说"+row["promise_text"], "他说："+row["promise_text"], "好\n"+row["promise_text"])):
            self.assertFalse(db.record_delivery(OWNER, row["sid"], text, digest(index), item_id=row["id"], expected_revision=row["revision"], now=101))
        self.activate(db, row)
        self.assertFalse(db.record_delivery(OWNER, row["sid"], row["promise_text"], "d"*64, item_id=row["id"], expected_revision=row["revision"], now=101))
        claim = db.claim_poll(OWNER, day_start=0, now=102)
        db.finish_poll(OWNER, claim["id"], claim["lease_token"], claim["revision"], "succeeded", now=103)
        due = db.due(OWNER, now=104)
        report = db.prepare_report(OWNER, "new-session", due["id"], due["revision"], now=104)
        self.assertEqual(db.get(OWNER, row["id"])["status"], "ready")
        self.assertIsNone(db.prepare_report(OWNER, "another-session", report["id"], report["revision"], now=105))
        self.assertFalse(db.record_delivery(OWNER, "private-one", report["delivery_text"], "e"*64, item_id=report["id"], expected_revision=report["revision"], now=105))
        self.assertTrue(db.record_delivery(OWNER, "new-session", report["delivery_text"], "e"*64, item_id=report["id"], expected_revision=report["revision"], now=105))
        self.assertEqual(db.get(OWNER, row["id"])["status"], "reported")
        self.assertIsNone(db.due(OWNER, now=1000))
        self.assertEqual(self.draft(db, now=1001)["status"], "reported")

    def test_drafts_unique_sentence_replace_and_expire(self):
        db = self.store()
        first = self.draft(db)
        second = self.draft(db, task="f"*32, now=102)
        self.assertNotEqual(first["promise_text"], second["promise_text"])
        self.assertEqual(db.get(OWNER, first["id"])["status"], "expired")
        self.assertIsNone(db.match_delivery(OWNER, first["sid"], first["promise_text"], now=103))
        self.assertIsNone(db.match_delivery(OWNER, second["sid"], second["promise_text"], now=702))
        renewed = self.draft(db, task="f"*32, now=703)
        self.assertNotEqual(renewed["promise_text"], second["promise_text"])
        self.assertIsNone(db.match_delivery(OWNER, second["sid"], second["promise_text"], now=704))

    def test_restart_reader_sees_lease_and_delivery(self):
        db = self.store()
        row = self.activate(db, self.draft(db))
        reader = self.store()
        self.assertEqual(reader.get(OWNER, row["id"])["status"], "active")
        claim = db.claim_poll(OWNER, day_start=0, lease_seconds=600, now=102)
        self.assertIsNone(reader.claim_poll(OWNER, day_start=0, now=402))
        self.assertFalse(reader.valid_poll(OWNER, claim["id"], claim["lease_token"], claim["revision"], now=703))
        resumed = reader.claim_poll(OWNER, day_start=0, now=703)
        self.assertNotEqual(resumed["lease_token"], claim["lease_token"])
        self.assertFalse(db.finish_poll(OWNER, claim["id"], claim["lease_token"], claim["revision"], "succeeded", now=704))
        self.assertTrue(reader.finish_poll(OWNER, resumed["id"], resumed["lease_token"], resumed["revision"], "pending", now=704))
        self.assertEqual(db.get(OWNER, row["id"])["status"], "active")

    def test_new_session_replaces_unsent_draft_with_new_unique_sentence(self):
        db = self.store()
        first = self.draft(db)
        second = self.draft(db, sid="new-private", now=102)
        self.assertEqual(first["id"], second["id"])
        self.assertNotEqual(first["promise_text"], second["promise_text"])
        self.assertIsNone(db.match_delivery(OWNER, first["sid"], first["promise_text"], now=103))
        self.activate(db, second, now=103)

    def test_budget_interval_backoff_and_unknown_are_persistent(self):
        db = self.store()
        row = self.activate(db, self.draft(db))
        self.assertIsNone(db.claim_poll(OWNER, day_start=0, daily_limit=0, now=102))
        self.assertEqual(db.get(OWNER, row["id"])["polls"], 0)
        claim = db.claim_poll(OWNER, day_start=0, daily_limit=1, now=102)
        db.finish_poll(OWNER, claim["id"], claim["lease_token"], claim["revision"], "unknown", now=103)
        saved = db.get(OWNER, row["id"])
        self.assertEqual((saved["result"], saved["status"]), ("unknown", "active"))
        self.assertGreater(saved["next_poll"], 402)
        reader = self.store()
        self.assertIsNone(reader.claim_poll(OWNER, day_start=0, daily_limit=1, now=10000))
        claim = reader.claim_poll(OWNER, day_start=86400, daily_limit=1, now=86502)
        self.assertIsNotNone(claim)
        self.assertFalse(db.finish_poll(OWNER, claim["id"], claim["lease_token"], claim["revision"], "failed", now=87000))

    def test_age_and_attempt_bounds_produce_honest_unknown(self):
        db = self.store()
        row = self.activate(db, self.draft(db))
        self.assertIsNone(db.claim_poll(OWNER, day_start=MAX_AGE, now=MAX_AGE+101))
        due = db.due(OWNER, now=MAX_AGE+102)
        self.assertEqual((due["id"], due["result"]), (row["id"], "unknown"))
        report = db.prepare_report(OWNER, "private-one", due["id"], due["revision"], now=MAX_AGE+103)
        self.assertIn("不表示任务成功或失败", report["delivery_text"])
        self.assertNotIn(TASK, report["delivery_text"])

    def test_owner_and_revision_gates_cancel_only_local(self):
        db = self.store()
        row = self.ready(db)
        self.assertEqual(db.items(OTHER), [])
        self.assertIsNone(db.get(OTHER, row["id"]))
        self.assertFalse(db.cancel(OTHER, row["id"], row["revision"], now=104))
        self.assertFalse(db.cancel(OWNER, row["id"], row["revision"]-1, now=104))
        self.assertTrue(db.cancel(OWNER, row["id"], row["revision"], now=104))
        self.assertIsNone(db.due(OWNER, now=105))
        self.assertIsNotNone(db.source("cap", TASK))

    def test_creation_source_is_immutable_and_empty_constructor_writes_nothing(self):
        db = self.store()
        self.assertEqual(db._read("SELECT COUNT(*) AS n FROM followthrough_control")[0]["n"], 0)
        self.draft(db)
        proof = db.source("cap", TASK)
        self.assertEqual(proof["source_signature"], SOURCE)
        for changes in ({"source_signature": "e"*64}, {"owner_id": OTHER, "scope": "private:"+OTHER}, {"operation": "jm_receive_pdf"}):
            kwargs = {"owner_id": OWNER, "scope": "private:"+OWNER, "operation": "jm_start_download", "source_signature": SOURCE, **changes}
            with self.assertRaises(ValueError):
                db.bind_source("cap", TASK, now=102, **kwargs)
        self.assertEqual(db.source("cap", TASK), proof)

    def test_prepare_and_expired_send_never_report(self):
        db = self.store()
        row = self.ready(db)
        report = db.prepare_report(OWNER, "private-one", row["id"], row["revision"], now=104)
        self.assertFalse(db.record_delivery(OWNER, "private-one", report["delivery_text"], "f"*64, item_id=row["id"], expected_revision=report["revision"], now=704))
        self.assertEqual(db.get(OWNER, row["id"])["status"], "ready")
        self.assertIsNotNone(db.due(OWNER, now=704))

    def test_attempt_cap_and_receipt_reuse_cannot_claim_completion(self):
        db = self.store()
        row = self.activate(db, self.draft(db))
        # Restore a persisted job already near its finite lifetime query cap.
        with db.con:
            row["polls"] = 167
            db._save(row, 101)
        claim = db.claim_poll(OWNER, day_start=0, now=102)
        db.finish_poll(OWNER, claim["id"], claim["lease_token"], claim["revision"], "pending", now=103)
        due = db.due(OWNER, now=104)
        self.assertEqual((due["polls"], due["result"]), (168, "unknown"))
        report = db.prepare_report(OWNER, row["sid"], row["id"], due["revision"], now=104)
        self.assertFalse(db.record_delivery(OWNER, row["sid"], report["delivery_text"], "d"*64,
                         item_id=row["id"], expected_revision=report["revision"], now=105))
        self.assertEqual(db.get(OWNER, row["id"])["status"], "ready")

    def test_eligible_filter_skips_charges_but_still_expires_old_active_items(self):
        db = self.store()
        row = self.activate(db, self.draft(db))
        self.assertIsNone(db.claim_poll(OWNER, day_start=0, eligible_ids=[], now=102))
        self.assertEqual(db.get(OWNER, row["id"])["polls"], 0)
        self.assertEqual(db._read("SELECT daily_checks FROM followthrough_control WHERE owner_id=?", (OWNER,))[0]["daily_checks"], 0)
        with self.assertRaises(ValueError):
            db.claim_poll(OWNER, day_start=0, eligible_ids=["arbitrary"], now=102)
        self.assertIsNone(db.claim_poll(OWNER, day_start=MAX_AGE, eligible_ids=[], now=MAX_AGE+101))
        self.assertEqual(db.get(OWNER, row["id"])["status"], "ready")
        self.assertEqual(db.get(OWNER, row["id"])["result"], "unknown")


class FollowthroughStoreTests(FollowthroughChecks, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"followthrough.sqlite3"
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.close()
        self.temp.cleanup()

    def store(self):
        store = FollowthroughStore(self.path)
        self.stores.append(store)
        return store


if __name__ == "__main__":
    unittest.main()
