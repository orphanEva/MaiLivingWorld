from __future__ import annotations

import concurrent.futures
import copy
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from MaiLivingWorld.growth_store import GrowthStore, HALF_LIFE, source_fingerprint
from MaiLivingWorld.scripts.migrate_all_sqlite_to_mysql import snapshot, ALL_TABLES
from MaiLivingWorld.brain_core import BrainBook
from MaiLivingWorld.cognition import CognitiveStore
from MaiLivingWorld.experience_loop import ExperienceLoop
from MaiLivingWorld.mind import MindStore
from MaiLivingWorld.proactive_feedback import ProactiveFeedback
from MaiLivingWorld.self_state import SelfState
from MaiLivingWorld.store import Store


def records(prefix="A", count=3, scope="private:12345678"):
    return [{"id": prefix+str(i), "text": f"{prefix}来源{i}的不同真实经历与学习结果",
             "scope": scope, "subject_id": "12345678", "sensitivity": "personal",
             "evidence_status": "recorded_event", "timestamp": 100.0+i,
             "source": "brain_event", "kind": "event"} for i in range(count)]


def interest(rows, delta=0.1, topic="编程"):
    return {"interests": [{"topic": topic, "delta": delta, "evidence_ids": [r["id"] for r in rows]}]}


def belief(rows, statement="工具需要根据任务选择", topic="编程工具"):
    return {"beliefs": [{"topic": topic, "statement": statement, "confidence": 0.8,
                         "evidence_ids": [r["id"] for r in rows]}]}


class GrowthStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.path = self.directory / "growth.sqlite3"
        self.db = GrowthStore(self.path)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def submit(self, proposal, rows=None, now=100.0, mode="active"):
        rows = records() if rows is None else rows
        job_id = self.db.enqueue(rows, mode=mode, now=now)
        job = self.db.claim(now=now, mode=mode)
        self.assertEqual(job["id"], job_id)
        result = self.db.complete(job_id, job["token"], proposal, rows, now=now+1)
        return result

    def test_lazy_schema_and_reference_only_idempotent_queue_survive_restart(self):
        self.assertEqual(self.db.con.execute("SELECT COUNT(*) FROM growth_state").fetchone()[0], 0)
        rows = records()
        first = self.db.enqueue(rows, now=100)
        self.assertEqual(self.db.enqueue(list(reversed(rows)), now=200), first)
        raw = self.db.con.execute("SELECT payload FROM growth_jobs WHERE id=?", (first,)).fetchone()[0]
        self.assertNotIn(rows[0]["text"], raw)
        self.db.close()
        self.db = GrowthStore(self.path)
        self.assertEqual(self.db.status()["last_enqueued"], 100)
        job = self.db.claim(now=201)
        self.assertEqual(job["id"], first)
        self.assertEqual(job["sources"][0]["digest"], source_fingerprint(rows[0]))

    def test_concurrent_claim_has_one_winner(self):
        self.db.enqueue(records(), now=100)
        barrier = threading.Barrier(3)

        def claim(_):
            db = GrowthStore(self.path)
            try:
                barrier.wait(timeout=10)
                return db.claim(now=100)
            finally:
                db.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            claimed = list(pool.map(claim, range(3)))
        self.assertEqual(sum(job is not None for job in claimed), 1)

    def test_expired_lease_can_resume_but_old_token_cannot_complete(self):
        rows = records()
        jid = self.db.enqueue(rows, now=100)
        old = self.db.claim(now=100, lease_seconds=10)
        self.db.close()
        self.db = GrowthStore(self.path)
        self.assertIsNone(self.db.claim(now=109))
        new = self.db.claim(now=110)
        self.assertNotEqual(old["token"], new["token"])
        with self.assertRaisesRegex(ValueError, "lease_lost"):
            self.db.complete(jid, old["token"], interest(rows), rows, now=111)
        self.db.complete(jid, new["token"], interest(rows), rows, now=111)
        self.assertEqual(self.db.state(now=111)["interests"][0]["weight"], 0.1)

    def test_defer_does_not_consume_retry_and_three_failures_stop(self):
        jid = self.db.enqueue(records(), now=100)
        job = self.db.claim(now=100)
        deferred = self.db.defer(jid, job["token"], "budget", 130, now=101)
        self.assertEqual(deferred["attempts"], 0)
        self.assertIsNone(self.db.claim(now=129))
        stamp = 130
        for attempt in range(1, 4):
            job = self.db.claim(now=stamp)
            result = self.db.fail(jid, job["token"], "model_failed", now=stamp+1)
            self.assertEqual(result["attempts"], attempt)
            stamp = result["next_attempt"]
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(self.db.claim(now=stamp+1000))

    def test_mode_filter_and_observe_never_change_growth_or_consume_sources(self):
        rows = records()
        observe = self.db.enqueue(rows, mode="observe", now=100)
        active = self.db.enqueue(rows, mode="active", now=101)
        job = self.db.claim(now=102, mode="observe")
        self.assertEqual(job["id"], observe)
        result = self.db.complete(observe, job["token"], interest(rows), rows, now=103)
        self.assertTrue(result["observed"])
        self.assertFalse(result["changed"])
        self.assertEqual(self.db.state(now=103)["revision"], 0)
        self.assertEqual(self.db.status()["consumed"], 0)
        self.assertEqual(self.db.history(), [])
        self.assertEqual(self.db.claim(now=104, mode="active")["id"], active)

    def test_store_rejects_untrusted_proposal_types_nonfinite_and_missing_evidence(self):
        rows = records()
        jid = self.db.enqueue(rows, now=100)
        job = self.db.claim(now=100)
        invalid = [[], {"owner_id": "98765432"}, interest(rows, float("nan")), interest(rows, True),
                   {"interests": [{"topic": "编程", "delta": 1, "evidence_ids": []}]},
                   {"traits": [{"trait": "loyalty", "delta": 0.01, "evidence_ids": ["A0"]}]},
                   {"knowledge": [{"topic": "算法", "statement": "测试声明", "confidence": 2, "evidence_ids": ["A0"]}]},
                   {"interests": interest(rows)["interests"] * 2}]
        for proposal in invalid:
            with self.subTest(proposal=proposal), self.assertRaises(ValueError):
                self.db.complete(jid, job["token"], proposal, rows, now=101)
            self.assertEqual(self.db.state(now=101)["revision"], 0)
            self.assertEqual(self.db.status()["consumed"], 0)

    def test_changed_missing_or_growth_derived_sources_cannot_be_used(self):
        rows = records()
        jid = self.db.enqueue(rows, now=100)
        job = self.db.claim(now=100)
        for field, changed in (("text", "新的来源内容"), ("scope", "public"),
                               ("subject_id", "87654321"), ("sensitivity", "public"),
                               ("evidence_status", "unverified_observation"), ("timestamp", 111.0)):
            altered = copy.deepcopy(rows)
            altered[0][field] = changed
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "source_changed"):
                self.db.complete(jid, job["token"], interest(rows), altered, now=101)
        with self.assertRaisesRegex(ValueError, "source_changed"):
            self.db.complete(jid, job["token"], interest(rows), rows[1:], now=101)
        for field, value in (("id", "growth:knowledge:derived"), ("source", "growth")):
            derived = copy.deepcopy(rows)
            derived[0][field] = value
            with self.assertRaisesRegex(ValueError, "derived_source"):
                self.db.enqueue(derived, now=102)

    def test_interest_and_mood_limits_and_same_id_revision_does_not_accumulate(self):
        rows = records()
        proposal = interest(rows, 4)
        proposal["mood"] = {"valence_delta": 4, "energy_delta": -4, "evidence_ids": ["A0"]}
        self.submit(proposal, rows)
        state = self.db.state(now=101)
        self.assertEqual(state["interests"][0]["weight"], 0.1)
        self.assertEqual(state["mood"], {"valence": 0.1, "energy": 0.4})
        self.assertEqual(state["traits"]["patience"], 0.5)
        revised = copy.deepcopy(rows)
        revised[0]["text"] += "追加一次内容"
        result = self.submit(proposal, revised, now=200)
        self.assertFalse(result["changed"])
        self.assertEqual(self.db.state(now=201)["interests"][0]["weight"], 0.1)
        self.assertEqual(self.db.status()["consumed"], 3)

    def test_mood_decay_is_a_read_view_and_survives_restart(self):
        rows = records()
        self.submit({"mood": {"valence_delta": .1, "energy_delta": .1,
                              "evidence_ids": ["A0"]}}, rows)
        raw = self.db.con.execute("SELECT payload FROM growth_state").fetchone()[0]
        view = self.db.state(now=101+HALF_LIFE)
        self.assertEqual(view["mood"], {"valence": .05, "energy": .55})
        self.assertEqual(raw, self.db.con.execute("SELECT payload FROM growth_state").fetchone()[0])
        self.db.close()
        self.db = GrowthStore(self.path)
        self.assertEqual(self.db.state(now=101+HALF_LIFE), view)

    def test_traits_require_three_independent_new_experiences(self):
        for prefix, kind in (("B", "only_two"), ("C", "same_text"), ("D", "same_origin")):
            rows = records(prefix)
            if kind == "only_two":
                rows = rows[:2]
            elif kind == "same_text":
                for row in rows:
                    row["text"] = "Same Experience！"
            else:
                for row in rows:
                    row["origin_key"] = "same-original-message"
            result = self.submit({"traits": [{"trait": "curiosity", "delta": .8,
                                               "evidence_ids": [r["id"] for r in rows]}]}, rows)
            self.assertEqual(result["traits"], 0)
        self.assertEqual(self.db.state(now=101)["traits"]["curiosity"], .5)

    def test_traits_step_and_daily_absolute_change_are_bounded(self):
        for i in range(4):
            rows = records("T"+str(i))
            self.submit({"traits": [{"trait": "curiosity", "delta": .5,
                                      "evidence_ids": [r["id"] for r in rows]}]}, rows, now=100+i*10)
        self.assertEqual(self.db.state(now=200)["traits"]["curiosity"], .53)
        rows = records("next-day")
        self.submit({"traits": [{"trait": "curiosity", "delta": -.9,
                                  "evidence_ids": [r["id"] for r in rows]}]}, rows, now=86500)
        self.assertEqual(self.db.state(now=86501)["traits"]["curiosity"], .52)

    def test_belief_revision_keeps_stable_key_and_private_source_dependencies(self):
        first = records("private", 1)
        self.submit(belief(first), first)
        previous = self.db.state(now=101)["beliefs"][0]
        new = records("public", 1, "public")
        new[0]["sensitivity"] = "public"
        self.submit(belief(new, "不同工具各自有适合的任务", "编程 工具"), new, now=200)
        latest = self.db.state(now=201)["beliefs"][0]
        self.assertEqual(latest["key"], previous["key"])
        self.assertEqual(latest["revision"], 2)
        self.assertEqual({r["scope"] for r in latest["sources"]}, {"private:12345678", "public"})
        self.assertEqual(self.db.history()[0]["before"]["beliefs"][0]["text"], previous["text"])

    def test_question_requires_fresh_evidence_to_resolve(self):
        first = records("Q", 1)
        item = {"topic": "查找方法", "question": "怎样检索有序数据", "status": "resolved", "evidence_ids": ["Q0"]}
        jid = self.db.enqueue(first, now=100)
        job = self.db.claim(now=100)
        with self.assertRaisesRegex(ValueError, "question_missing"):
            self.db.complete(jid, job["token"], {"questions": [item]}, first, now=101)
        item["status"] = "open"
        self.db.complete(jid, job["token"], {"questions": [item]}, first, now=101)
        new = records("answer", 1)
        item = {**item, "status": "resolved", "evidence_ids": ["answer0"]}
        self.submit({"questions": [item]}, new, now=200)
        self.assertEqual(self.db.state(now=201)["questions"][0]["status"], "resolved")

    def test_revision_checked_inside_commit_and_job_remains_retryable(self):
        a, b = records("A", 1), records("B", 1)
        aid = self.db.enqueue(a, now=100)
        bid = self.db.enqueue(b, now=101)
        ajob, bjob = self.db.claim(now=102), self.db.claim(now=102)
        self.db.complete(aid, ajob["token"], belief(a), a, now=103, expected_revision=0)
        with self.assertRaisesRegex(ValueError, "revision_conflict"):
            self.db.complete(bid, bjob["token"], belief(b), b, now=104, expected_revision=0)
        self.assertEqual(self.db.state(now=104)["revision"], 1)
        self.assertEqual(self.db.status()["jobs"]["running"], 1)

    def test_growth_commit_is_atomic_across_state_history_consumption_and_job(self):
        rows = records()
        jid = self.db.enqueue(rows, now=100)
        job = self.db.claim(now=100)
        with self.db.con:
            self.db.con.execute("CREATE TRIGGER reject_history BEFORE INSERT ON growth_history "
                                "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.complete(jid, job["token"], interest(rows), rows, now=101)
        self.assertEqual(self.db.state(now=101)["revision"], 0)
        self.assertEqual(self.db.status()["consumed"], 0)
        self.assertEqual(self.db.status()["jobs"]["running"], 1)
        self.assertEqual(self.db.history(), [])

    def test_rollback_preserves_consumption_and_invalidates_pending_worker(self):
        rows = records()
        self.submit(interest(rows), rows)
        bid = self.db.enqueue(records("B"), now=110)
        waiting = self.db.claim(now=110)
        restored = self.db.rollback(1, now=111)
        self.assertEqual(restored["revision"], 2)
        self.assertEqual(restored["interests"], [])
        self.assertEqual(self.db.status()["consumed"], 3)
        self.assertEqual(self.db.history()[0]["kind"], "rollback")
        with self.assertRaisesRegex(ValueError, "lease_lost"):
            self.db.complete(bid, waiting["token"], interest(records("B")), records("B"), now=112)
        with self.assertRaisesRegex(ValueError, "nothing_to_rollback"):
            self.db.rollback(2, now=112)

    def test_changed_source_conflict_requires_explicit_retire_before_rebuild(self):
        rows = records("R", 1)
        self.submit(belief(rows), rows)
        key = self.db.state(now=101)["beliefs"][0]["key"]
        revised = copy.deepcopy(rows)
        revised[0]["text"] = "修正后的原始来源内容"
        jid = self.db.enqueue(revised, now=110)
        job = self.db.claim(now=110)
        with self.assertRaisesRegex(ValueError, "source_revision_conflict"):
            self.db.complete(jid, job["token"], belief(revised, "基于修正来源的新看法"), revised, now=111)
        retired = self.db.retire(key, 1, now=112)
        self.assertEqual(retired["beliefs"], [])
        self.assertEqual(retired["revision"], 2)
        self.assertEqual(self.db.history()[0]["kind"], "retire")
        reclaimed = self.db.claim(now=113)
        self.db.complete(jid, reclaimed["token"], belief(revised), revised, now=114, expected_revision=2)
        self.assertEqual(self.db.state(now=114)["beliefs"][0]["sources"][0]["digest"], source_fingerprint(revised[0]))

    def test_state_capacity_preserves_previous_data(self):
        self.db.state(now=100)
        row = self.db.con.execute("SELECT payload FROM growth_state").fetchone()[0]
        payload = json.loads(row)
        payload["beliefs"] = [{"key": f"b:{i:024x}", "topic": str(i), "text": "已存知识",
                               "confidence": .8, "status": "active", "sources": [], "revision": 1} for i in range(32)]
        with self.db.con:
            self.db.con.execute("UPDATE growth_state SET payload=? WHERE slot=1", (json.dumps(payload),))
        rows = records()
        jid = self.db.enqueue(rows, now=100)
        job = self.db.claim(now=100)
        with self.assertRaisesRegex(ValueError, "state_capacity"):
            self.db.complete(jid, job["token"], belief(rows), rows, now=101)
        self.assertEqual(len(self.db.state(now=101)["beliefs"]), 32)

    def test_source_capacity_never_discards_previous_private_dependencies(self):
        for i in range(24):
            rows = records("source"+str(i), 1)
            self.submit(belief(rows), rows, now=100+i*2)
        previous = self.db.state(now=150)
        rows = records("public-extra", 1, "public")
        rows[0]["sensitivity"] = "public"
        jid = self.db.enqueue(rows, now=151)
        job = self.db.claim(now=151)
        with self.assertRaisesRegex(ValueError, "source_capacity"):
            self.db.complete(jid, job["token"], belief(rows), rows, now=152)
        self.assertEqual(self.db.state(now=152), previous)
        self.assertEqual(len(previous["beliefs"][0]["sources"]), 24)

    def test_rollback_does_not_replenish_daily_trait_budget(self):
        for i in range(3):
            rows = records("budget"+str(i))
            self.submit({"traits": [{"trait": "patience", "delta": .01,
                                      "evidence_ids": [r["id"] for r in rows]}]}, rows, now=100+i*2)
        self.assertEqual(self.db.rollback(3, now=107)["traits"]["patience"], .52)
        rows = records("fresh-after-rollback")
        result = self.submit({"traits": [{"trait": "patience", "delta": .01,
                                          "evidence_ids": [r["id"] for r in rows]}]}, rows, now=108)
        self.assertFalse(result["changed"])
        self.assertEqual(self.db.state(now=109)["traits"]["patience"], .52)

    def test_old_six_or_seven_ledger_backups_do_not_need_growth_file(self):
        old = self.directory / "old"
        old.mkdir()
        for filename, cls in (("living_world", Store), ("brain_book", BrainBook), ("mind", MindStore),
                              ("cognition", CognitiveStore), ("experience_loop", ExperienceLoop),
                              ("proactive_feedback", ProactiveFeedback)):
            store = cls(old / (filename + ".sqlite3"))
            store.close()
        rows, missing = snapshot(old)
        self.assertEqual(missing, [])
        self.assertEqual(set(rows), set(ALL_TABLES))
        self.assertEqual(rows["growth_jobs"], [])
        identity = SelfState(old / "self_state.sqlite3")
        before = identity.configure_owner("12345678")
        identity.close()
        rows, missing = snapshot(old)
        self.assertEqual(missing, [])
        self.assertEqual(rows["self_identity"][0]["agent_id"], before["agent_id"])
        self.assertEqual(rows["growth_state"], [])
        self.assertFalse((old / "growth.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
