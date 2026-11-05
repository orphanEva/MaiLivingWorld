"""Persistent real-dispatch activity and bounded recovery, shared with MySQL."""
import concurrent.futures
import json
from pathlib import Path
import tempfile
import unittest

from MaiLivingWorld.attention_store import AttentionStore
from MaiLivingWorld.tests.test_attention_store import OWNER, OTHER, READING, message


class ActivityChecks:
    def claim(self, now=100, **changes):
        options = dict(candidates=[READING], day_start=0, daily_limit=20,
                       interval_seconds=0, lease_seconds=180, now=now, activity_awareness=True)
        options.update(changes)
        return self.db.claim(OWNER, **options)

    def start(self, claim, now=100):
        return self.db.start_activity(OWNER, claim["token"], proof="a"*64, signature="b"*64, now=now)

    def progress(self, now=100):
        claim = self.claim(now)
        self.assertIsNotNone(claim)
        self.assertTrue(self.start(claim, now))
        self.assertTrue(self.db.finish(OWNER, claim["token"], outcome="progress", now=now+1,
                                     activity_awareness=True, activity_proof="c"*64))
        return claim

    def test_real_dispatch_and_progress_survive_restart_with_only_hashes(self):
        claim = self.progress()
        self.assertEqual(self.db.state(OWNER, now=101)["activity"]["load"], .25)
        events = self.db.events(OWNER)
        start = next(e for e in events if e["action"] == "activity_start")
        finish = next(e for e in events if e["action"] == "finish")
        self.assertEqual(start["token_hash"], finish["activity"]["token_hash"])
        self.assertEqual(start["proof"], "a"*64)
        self.assertEqual(finish["activity"]["proof"], "c"*64)
        self.assertNotIn(claim["token"], json.dumps(events))
        self.db.close()
        self.db = self.open_store()
        self.assertEqual(self.db.state(OWNER, now=101)["activity"]["load"], .25)
        self.assertEqual(self.db.events(OWNER), events)
        self.assertFalse(self.db.finish(OWNER, claim["token"], outcome="progress", now=102,
                                       activity_awareness=True, activity_proof="c"*64))
        self.assertLess(self.db.state(OWNER, now=102)["activity"]["load"], .25)

    def test_recovery_blocks_only_new_background_and_off_switch_keeps_old_schedule(self):
        for now in (100, 102, 104, 106):
            self.progress(now)
        state = self.db.state(OWNER, now=108)
        self.assertTrue(state["activity"]["recovering"])
        self.assertIsNone(self.claim(109))
        self.assertEqual(self.db.state(OWNER, now=109)["cycle_count"], 4)
        self.assertIsNotNone(self.claim(109, activity_awareness=False))
        # Wait beyond the lease and one hour's recovery; original progress remains.
        later = self.claim(4000)
        self.assertIsNotNone(later)
        self.assertFalse(self.db.state(OWNER, now=4000)["activity"]["recovering"])

    def test_unstarted_failed_blocked_or_interrupted_work_never_adds_load(self):
        claim = self.claim()
        self.db.finish(OWNER, claim["token"], outcome="progress", now=101,
                       activity_awareness=True, activity_proof="c"*64)
        self.assertEqual(self.db.state(OWNER, now=101)["activity"]["load"], 0)
        for i, outcome in enumerate(("blocked", "failed", "interrupted")):
            now = 102+i*2
            claim = self.claim(now)
            self.assertTrue(self.start(claim, now))
            self.assertTrue(self.db.finish(OWNER, claim["token"], outcome=outcome, now=now+1,
                                          activity_awareness=True, activity_proof="c"*64))
        self.assertEqual(self.db.state(OWNER, now=109)["activity"]["load"], 0)

    def test_owner_capture_invalidates_progress_without_adding_load(self):
        claim = self.claim()
        self.assertTrue(self.start(claim))
        self.assertTrue(self.db.capture_activity(message(), owner_id=OWNER, now=101))
        self.assertFalse(self.db.finish(OWNER, claim["token"], outcome="progress", now=102,
                                       activity_awareness=True, activity_proof="c"*64))
        state = self.db.state(OWNER, now=102)
        self.assertEqual(state["last_outcome"], "interrupted")
        self.assertEqual(state["activity"]["load"], 0)
        self.assertEqual(self.db.state(OTHER, now=102)["activity"]["load"], 0)

    def test_start_requires_live_lease_and_is_idempotent(self):
        claim = self.claim()
        self.assertFalse(self.db.start_activity(OWNER, "wrong", proof="a"*64, signature="b"*64, now=100))
        self.assertTrue(self.start(claim))
        self.assertFalse(self.start(claim, 101))
        self.assertEqual(sum(e["action"] == "activity_start" for e in self.db.events(OWNER)), 1)
        self.db.capture_activity(message(), owner_id=OWNER, now=101)
        self.assertFalse(self.start(claim, 102))
        self.assertFalse(self.start(claim, 300))

    def test_concurrent_finish_has_one_progress_and_load_winner(self):
        claim = self.claim()
        self.start(claim)
        def worker(_):
            peer = self.open_store()
            try:
                return peer.finish(OWNER, claim["token"], outcome="progress", now=101,
                                   activity_awareness=True, activity_proof="c"*64)
            finally:
                peer.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sorted(executor.map(worker, range(2))), [False, True])
        self.assertEqual(self.db.state(OWNER, now=101)["activity"]["load"], .25)
        self.assertEqual(sum(e["action"] == "finish" for e in self.db.events(OWNER)), 1)

    def test_legacy_payload_defaults_and_events_remain_exact(self):
        claim = self.claim(activity_awareness=False)
        row = self.db.con.execute("SELECT payload FROM attention_control WHERE owner_id=?", (OWNER,)).fetchone()
        payload = json.loads(row["payload"])
        payload.pop("activity")
        payload.pop("lease_activity")
        with self.db.con:
            self.db.con.execute("UPDATE attention_control SET payload=? WHERE owner_id=?", (json.dumps(payload), OWNER))
        state = self.db.state(OWNER, now=101)
        self.assertEqual(state["activity"]["load"], 0)
        self.assertIsNone(state["lease"]["activity"])
        event = self.db.events(OWNER)[0]
        self.assertNotIn("activity", event)
        self.assertNotIn("lease_activity", event)
        self.assertTrue(self.start(claim, 101))

    def test_clock_rollback_and_readonly_projection_do_not_change_saved_load(self):
        self.progress()
        original = self.db.con.execute("SELECT payload FROM attention_control WHERE owner_id=?", (OWNER,)).fetchone()[0]
        self.assertEqual(self.db.state(OWNER, now=100)["activity"]["load"], .25)
        self.assertEqual(self.db.state(OWNER, now=1901)["activity"]["load"], .125)
        self.assertEqual(self.db.state(OWNER, now=3701)["activity"]["load"], 0)
        self.assertEqual(self.db.con.execute("SELECT payload FROM attention_control WHERE owner_id=?", (OWNER,)).fetchone()[0], original)


class ActivityStoreTests(ActivityChecks, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"attention.sqlite3"
        self.db = self.open_store()

    def open_store(self):
        return AttentionStore(self.path)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()
