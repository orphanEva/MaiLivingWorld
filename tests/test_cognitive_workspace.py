"""Bounded working state and outcome learning, shared by SQLite and MySQL."""
import concurrent.futures
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from MaiLivingWorld.attention_store import AttentionStore
from MaiLivingWorld.cognitive_workspace import assess, candidate_key, capability_view
from MaiLivingWorld.tests.test_attention_store import OWNER, OTHER, READING, RESEARCH, message


class WorkspaceChecks:
    def claim(self, now=100, **changes):
        options = dict(candidates=[READING], day_start=0, daily_limit=100,
                       interval_seconds=0, lease_seconds=180, now=now, integrated_workspace=True)
        options.update(changes)
        return self.db.claim(OWNER, **options)

    def start(self, claim, now=100):
        return self.db.start_activity(OWNER, claim["token"], proof="a"*64, signature="b"*64, now=now)

    def finish(self, claim, outcome="progress", now=101):
        return self.db.finish(OWNER, claim["token"], outcome=outcome, now=now,
                              activity_proof="c"*64, learn_capabilities=True)

    def test_workspace_capture_is_bounded_private_and_survives_restart(self):
        claim = self.claim()
        self.assertTrue(self.start(claim))
        self.assertTrue(self.finish(claim))
        event = message(body="只有这一次输入的私人目标")
        self.assertTrue(self.db.capture_activity(event, owner_id=OWNER, now=102, workspace_intent="advice"))
        saved = self.db.state(OWNER, now=102)
        self.assertEqual(saved["workspace"]["focus"], READING)
        self.assertEqual(saved["workspace"]["intent"], "advice")
        self.assertEqual(saved["workspace"]["pause_reason"], "owner_message")
        self.assertNotIn("私人目标", json.dumps(saved, ensure_ascii=False))
        self.assertNotIn(claim["token"], json.dumps(saved))
        self.db.close()
        self.db = self.open_store()
        self.assertEqual(self.db.state(OWNER, now=102), saved)
        self.assertFalse(self.db.capture_activity(event, owner_id=OWNER, now=103, workspace_intent="listen"))
        self.assertEqual(self.db.state(OWNER, now=103)["workspace"], saved["workspace"])
        self.assertIsNone(self.db.state(OTHER)["workspace"]["focus"])
        self.assertEqual(self.db.state(OTHER)["capabilities"], {})

    def test_real_dispatch_and_finite_outcome_are_both_required(self):
        claim = self.claim()
        self.finish(claim)
        self.assertEqual(self.db.state(OWNER)["capabilities"], {})
        claim = self.claim(102)
        self.start(claim, 102)
        self.db.finish(OWNER, claim["token"], outcome="progress", now=103, learn_capabilities=True)
        self.assertEqual(self.db.state(OWNER)["capabilities"], {})
        for index, outcome in enumerate(("progress", "blocked", "failed", "interrupted")):
            now = 104+index*2
            claim = self.claim(now)
            self.start(claim, now)
            self.finish(claim, outcome, now+1)
        learned = self.db.state(OWNER)["capabilities"]
        self.assertEqual([row["outcome"] for row in learned["reading"]], ["progress", "blocked", "failed"])
        self.assertEqual(capability_view(learned)["reading"]["attempts"], 3)
        self.assertEqual(self.db.state(OWNER)["activity"]["load"], 0)

    def test_owner_interruption_and_expiry_do_not_teach_failure(self):
        claim = self.claim()
        self.start(claim)
        self.db.capture_activity(message(), owner_id=OWNER, now=101, workspace_intent="conversation")
        self.finish(claim, "failed", 102)
        self.assertEqual(self.db.state(OWNER)["capabilities"], {})
        claim = self.claim(300)
        self.start(claim, 300)
        self.finish(claim, "blocked", 500)
        self.assertEqual(self.db.state(OWNER)["capabilities"], {})

    def test_recent_attempts_are_capped_and_point_to_real_receipts(self):
        for index in range(30):
            now = 100+index*2
            claim = self.claim(now)
            self.start(claim, now)
            self.finish(claim, "blocked", now+1)
        records = self.db.state(OWNER)["capabilities"]["reading"]
        self.assertEqual(len(records), 24)
        self.assertEqual(len({row["receipt"] for row in records}), 24)
        starts = {row["token_hash"] for row in self.db.events(OWNER, 200) if row["action"] == "activity_start"}
        self.assertTrue(all(row["receipt"] in starts for row in records))
        self.assertTrue(capability_view({"reading": records})["reading"]["needs_review"])

    def test_concurrent_finish_records_one_attempt(self):
        claim = self.claim()
        self.start(claim)
        def worker(_):
            peer = self.open_store()
            try:
                return peer.finish(OWNER, claim["token"], outcome="progress", now=101,
                                   activity_proof="c"*64, learn_capabilities=True)
            finally:
                peer.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(worker, range(2))), [False, True])
        self.assertEqual(len(self.db.state(OWNER)["capabilities"]["reading"]), 1)

    def test_write_failure_rolls_back_learning_and_keeps_retryable_lease(self):
        claim = self.claim()
        self.start(claim)
        with patch.object(self.db, "_save", side_effect=RuntimeError("simulated write failure")):
            with self.assertRaises(RuntimeError):
                self.finish(claim)
        self.assertEqual(self.db.state(OWNER)["capabilities"], {})
        self.assertTrue(self.db.valid(OWNER, claim["token"], now=101))
        self.assertFalse(any(row["action"] == "finish" for row in self.db.events(OWNER)))
        self.assertTrue(self.finish(claim))
        self.assertEqual(len(self.db.state(OWNER)["capabilities"]["reading"]), 1)

    def test_old_payload_reads_without_writing_then_migrates_on_action(self):
        claim = self.claim()
        row = self.db.con.execute("SELECT payload FROM attention_control WHERE owner_id=?", (OWNER,)).fetchone()
        original = json.loads(row["payload"])
        original.pop("workspace")
        original.pop("capabilities")
        raw = json.dumps(original)
        with self.db.con:
            self.db.con.execute("UPDATE attention_control SET payload=? WHERE owner_id=?", (raw, OWNER))
        self.assertEqual(self.db.state(OWNER)["capabilities"], {})
        self.assertIsNone(self.db.state(OWNER)["workspace"]["focus"])
        self.assertEqual(self.db.con.execute("SELECT payload FROM attention_control WHERE owner_id=?", (OWNER,)).fetchone()[0], raw)
        self.start(claim)
        self.finish(claim)
        self.assertEqual(len(self.db.state(OWNER)["capabilities"]["reading"]), 1)

    def test_weighted_selection_keeps_fairness_owner_priority_and_budget(self):
        assessment = {candidate_key(READING): {"score": .1, "reasons": ["continuity"]},
                      candidate_key(RESEARCH): {"score": .9, "reasons": ["interest"]}}
        first = self.claim(candidates=[READING, RESEARCH], assessment=assessment)
        self.assertEqual(first["focus"], RESEARCH)
        self.finish(first)
        second = self.claim(102, candidates=[READING, RESEARCH], assessment=assessment)
        self.assertEqual(second["focus"], RESEARCH)
        self.finish(second, now=103)
        third = self.claim(104, candidates=[READING, RESEARCH], assessment=assessment)
        self.assertEqual(third["focus"], READING)
        self.finish(third, now=105)
        self.assertIsNone(self.claim(106, candidates=[READING, RESEARCH], assessment=assessment, daily_limit=3))
        self.db.capture_activity(message(), owner_id=OWNER, now=107)
        self.assertIsNone(self.claim(108, candidates=[READING, RESEARCH], assessment=assessment))

    def test_invalid_assessments_and_intents_never_write(self):
        for invalid in ({}, {candidate_key(READING): {"score": float("nan"), "reasons": []}},
                        {candidate_key(READING): {"score": 1, "reasons": ["model invented reason"]}}):
            with self.assertRaises(ValueError):
                self.claim(assessment=invalid)
        with self.assertRaises(ValueError):
            self.db.capture_activity(message(), owner_id=OWNER, workspace_intent=[])
        self.assertEqual(self.db.events(OWNER), [])

    def test_weighted_rotation_eventually_gives_third_task_a_turn(self):
        third = {"kind": "project", "key": "project:c", "version": "c"*64}
        candidates = [READING, RESEARCH, third]
        assessment = {candidate_key(row): {"score": 3-index, "reasons": ["interest"]}
                      for index, row in enumerate(candidates)}
        selected = []
        for index in range(6):
            now = 100+index*2
            claim = self.claim(now, candidates=candidates, assessment=assessment)
            selected.append(claim["focus"])
            self.finish(claim, now=now+1)
        self.assertEqual(selected, [READING, READING, RESEARCH, RESEARCH, third, third])


class WorkspaceStoreTests(WorkspaceChecks, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"attention.sqlite3"
        self.db = self.open_store()

    def open_store(self):
        return AttentionStore(self.path)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()


class ActionValuationTests(unittest.TestCase):
    def scores(self, growth=None, state=None, details=None):
        return assess([READING, RESEARCH], details or {candidate_key(READING): {"topic": "森林"},
            candidate_key(RESEARCH): {"topic": "游戏"}}, growth or {}, state or {})

    def test_interest_and_source_validated_feeling_change_choice(self):
        baseline = self.scores()
        curious = self.scores({"emotions": [{"topic": "游戏", "feeling": "curious", "intensity": .5}]})
        self.assertGreater(baseline[candidate_key(READING)]["score"], baseline[candidate_key(RESEARCH)]["score"])
        self.assertGreater(curious[candidate_key(RESEARCH)]["score"], curious[candidate_key(READING)]["score"])
        negative = self.scores({"interests": [{"topic": "游戏", "weight": -.8}]})
        self.assertLess(negative[candidate_key(RESEARCH)]["score"], baseline[candidate_key(RESEARCH)]["score"])

    def test_load_commitment_traits_and_attempts_all_have_bounded_influence(self):
        baseline = self.scores()
        result = self.scores({"traits": {"patience": .9, "curiosity": .1}},
            {"activity": {"load": .8}, "pending_commitments": 1,
             "capabilities": {"research": [{"outcome": "blocked"}]*3}})
        self.assertLess(result[candidate_key(RESEARCH)]["score"], baseline[candidate_key(RESEARCH)]["score"])
        self.assertIn("experience", result[candidate_key(RESEARCH)]["reasons"])
        self.assertIn("commitment", result[candidate_key(READING)]["reasons"])
        self.assertIn("patience", result[candidate_key(READING)]["reasons"])

    def test_unrelated_feelings_have_no_effect(self):
        self.assertEqual(self.scores(), self.scores({"emotions": [{"topic": "天气", "feeling": "disappointed", "intensity": .5}]}))
