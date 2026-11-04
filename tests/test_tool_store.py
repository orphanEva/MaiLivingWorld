from __future__ import annotations

import concurrent.futures
import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from MaiLivingWorld.tool_store import ToolStore
from MaiLivingWorld.growth_store import GrowthStore, source_fingerprint
from MaiLivingWorld.brain_core import BrainBook
from MaiLivingWorld.cognition import CognitiveStore
from MaiLivingWorld.experience_loop import ExperienceLoop
from MaiLivingWorld.mind import MindStore
from MaiLivingWorld.proactive_feedback import ProactiveFeedback
from MaiLivingWorld.self_state import SelfState
from MaiLivingWorld.store import Store
from MaiLivingWorld.scripts.migrate_all_sqlite_to_mysql import snapshot

DIGEST = hashlib.sha256(b'{"pixiv_id":123}').hexdigest()


class ToolStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "tool_memory.sqlite3"
        self.db = ToolStore(self.path)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def reopen(self):
        self.db.close()
        self.db = ToolStore(self.path)

    def observe(self, **overrides):
        values = dict(provider="pixiv", resource_key="illust:123", text="海边的风景插画",
                      scope="private:12345678", subject_id="12345678", now=100)
        values.update(overrides)
        return self.db.put_observation(**values)

    def reserve(self, **overrides):
        values = dict(provider="pixiv", tool="archive_work", args_digest=DIGEST,
                      request_key="trusted-message-id-1", scope="private:12345678",
                      subject_id="12345678", day_start=0, max_daily=10, now=100)
        values.update(overrides)
        return self.db.reserve_call(**values)

    def research(self, keys=("q:one", "q:two"), **overrides):
        values = dict(interval_seconds=60, daily_limit=10, day_start=0, now=100)
        values.update(overrides)
        return self.db.claim_research(keys, **values)

    def test_constructor_and_read_apis_leave_all_tables_empty(self):
        self.assertEqual(self.db.observations(), [])
        self.assertEqual(self.db.resolve([]), [])
        self.assertIsNone(self.db.task("pixiv", "1"*32))
        self.assertEqual(self.db.status()["call_budget"]["count"], 0)
        for table in ("tool_observations", "tool_calls", "tool_tasks", "tool_research"):
            self.assertEqual(self.db.con.execute("SELECT COUNT(*) FROM "+table).fetchone()[0], 0)

    def test_stable_record_and_fingerprint_survive_repeated_reads_and_restart(self):
        first = self.observe()
        self.assertRegex(first["id"], r"^tool:observation:[0-9a-f]{64}$")
        self.assertEqual(first["source"], "tool")
        self.assertEqual(first["evidence_status"], "tool_observation")
        self.assertEqual(self.observe(now=200), first)
        self.reopen()
        self.assertEqual(self.db.resolve([first["id"]]), [first])
        self.assertEqual(source_fingerprint(self.observe(now=300)), source_fingerprint(first))

    def test_changed_content_advances_timestamp_even_during_clock_regression(self):
        before = self.observe()
        after = self.observe(text="山林的风景插画", now=99)
        self.assertEqual(after["id"], before["id"])
        self.assertGreater(after["timestamp"], before["timestamp"])
        self.assertNotEqual(source_fingerprint(after), source_fingerprint(before))

    def test_url_only_changes_invalidate_fingerprint_without_changing_identity(self):
        before = self.observe(url="https://www.pixiv.net/artworks/123")
        after = self.observe(url="https://www.pixiv.net/artworks/124", now=101)
        self.assertNotEqual(source_fingerprint(before), source_fingerprint(after))
        self.assertEqual(before["id"], after["id"])

    def test_private_and_confidential_constraints_cannot_be_laundered_public(self):
        first = self.observe()
        public = self.observe(scope="public:pixiv", subject_id="", sensitivity="public", now=101)
        self.assertEqual(public["scope"], first["scope"])
        self.assertEqual(public["subject_id"], first["subject_id"])
        self.assertEqual(public["sensitivity"], "personal")
        other = self.observe(scope="group:98765432", subject_id="87654321", now=102)
        self.assertEqual((other["scope"], other["subject_id"], other["sensitivity"]), ("global:tool", "", "unknown"))
        again = self.observe(scope="public", sensitivity="public", now=103)
        self.assertEqual(again["scope"], "global:tool")
        self.assertEqual(again["sensitivity"], "unknown")
        secret = self.observe(text="这条工具观察需要保密", now=104)
        self.assertEqual(secret["sensitivity"], "confidential")
        self.assertEqual(self.observe(now=105)["sensitivity"], "confidential")

    def test_public_observation_becomes_private_when_a_private_constraint_arrives(self):
        self.observe(scope="public:pixiv", subject_id="", sensitivity="public")
        private = self.observe(now=101)
        self.assertEqual(private["scope"], "private:12345678")
        self.assertEqual(private["sensitivity"], "personal")

    def test_origin_identity_is_bounded_and_shared_between_aliases(self):
        a = self.observe(origin_key="original:illust:123")
        b = self.observe(resource_key="version:123:1", origin_key="original:illust:123")
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(a["origin_key"], b["origin_key"])
        self.assertLessEqual(len(a["origin_key"]), 191)

    def test_retraction_survives_restart_and_reactivation_has_a_new_version(self):
        first = self.observe()
        self.assertTrue(self.db.retract_observation("pixiv", "illust:123", now=101))
        self.assertFalse(self.db.retract_observation("pixiv", "illust:123", now=102))
        self.reopen()
        self.assertEqual(self.db.resolve([first["id"]]), [])
        self.assertEqual(self.db.observations(), [])
        after = self.observe(now=103)
        self.assertEqual(first["id"], after["id"])
        self.assertNotEqual(source_fingerprint(first), source_fingerprint(after))

    def test_origin_retraction_withdraws_metadata_and_all_excerpts_only_for_that_work(self):
        originals = [self.observe(resource_key=key, origin_key="work:novel:7") for key in (
            "work:novel:7", "work:novel:7:excerpt:1:0", "work:novel:7:excerpt:2:4000")]
        other_work = self.observe(resource_key="work:novel:8", origin_key="work:novel:8")
        other_provider = self.observe(provider="cap", resource_key="work:novel:7", origin_key="work:novel:7")
        # Same NFKC and whitespace normalization as put_observation; clock regression
        # must still advance every withdrawn source's version timestamp.
        self.assertEqual(self.db.retract_origin("pixiv", "  work:novel:７  ", now=99), 3)
        self.assertEqual(self.db.resolve([row["id"] for row in originals]), [])
        for original in originals:
            raw = self.db.con.execute("SELECT active,timestamp FROM tool_observations WHERE id=?",
                                      (original["id"],)).fetchone()
            self.assertEqual(raw["active"], 0)
            self.assertGreater(raw["timestamp"], original["timestamp"])
        self.assertEqual(self.db.resolve([other_work["id"], other_provider["id"]]), [other_work, other_provider])
        self.reopen()
        self.assertEqual(self.db.retract_origin("pixiv", "work:novel:7", now=101), 0)
        self.assertEqual(self.db.status()["observations"], {"active": 2, "inactive": 3})

    def test_invalid_input_does_not_replace_a_valid_record(self):
        first = self.observe()
        for values in ({"text": "x"*601}, {"text": None}, {"sensitivity": "safe"},
                       {"url": "https://example.test/media?token=secret"},
                       {"url": "https://user:pass@example.test/media"}, {"now": float("nan")},
                       {"now": True}, {"provider": "../bad"}):
            with self.assertRaises(ValueError):
                self.observe(**values)
        self.assertEqual(self.db.resolve([first["id"]]), [first])

    def test_calls_deduplicate_across_restart_and_keep_no_raw_request_key(self):
        first = self.reserve()
        self.assertTrue(first["allowed"])
        self.db.complete_call(first["id"], "uncertain", "result_unknown", now=101)
        self.reopen()
        duplicate = self.reserve(now=102)
        self.assertEqual(duplicate, {"id": first["id"], "allowed": False, "status": "uncertain"})
        row = dict(self.db.con.execute("SELECT * FROM tool_calls").fetchone())
        self.assertNotIn("trusted-message-id-1", json.dumps(row))
        self.assertEqual(row["args_digest"], DIGEST)
        self.assertEqual(self.db.status()["call_budget"]["count"], 1)

    def test_failed_and_uncertain_calls_use_global_daily_budget_across_providers(self):
        a = self.reserve(max_daily=2)
        self.db.complete_call(a["id"], "failed", now=101)
        b = self.reserve(provider="cap", request_key="message2", max_daily=2)
        self.db.complete_call(b["id"], "uncertain", now=101)
        self.assertEqual(self.reserve(request_key="message3", max_daily=2)["status"], "budget_exhausted")
        self.assertTrue(self.reserve(request_key="tomorrow", day_start=86400, now=86500, max_daily=2)["allowed"])
        self.assertFalse(self.reserve(request_key="clock-backward", max_daily=99)["allowed"])

    def test_concurrent_call_budget_has_one_winner(self):
        self.competing_call_budget()

    def test_concurrent_call_budget_with_existing_controller_has_one_winner(self):
        self.observe()
        self.competing_call_budget()

    def competing_call_budget(self):
        barrier = threading.Barrier(4)

        def worker(index):
            db = ToolStore(self.path)
            try:
                barrier.wait(timeout=10)
                return db.reserve_call("pixiv", "archive_work", DIGEST, "message"+str(index),
                                       "private:12345678", "12345678", 0, 1, now=100)
            finally:
                db.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(worker, range(4)))
        self.assertEqual(sum(result["allowed"] for result in results), 1)

    def test_failed_call_insert_does_not_consume_budget(self):
        self.db.con.execute("CREATE TRIGGER reject_call BEFORE INSERT ON tool_calls "
                            "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.reserve(max_daily=1)
        self.assertEqual(self.db.status()["call_budget"]["count"], 0)
        self.assertEqual(self.db.con.execute("SELECT COUNT(*) FROM tool_research").fetchone()[0], 0)

    def test_task_binding_is_idempotent_and_cannot_be_rebound(self):
        task_id = "1234567890abcdef"*2
        self.db.bind_task("pixiv", task_id, "private:12345678", "12345678", "archive_work", now=100)
        self.db.bind_task("pixiv", task_id, "private:12345678", "12345678", "archive_work", now=101)
        for scope, actor, operation in (("group:98765432", "12345678", "archive_work"),
                                        ("private:12345678", "87654321", "archive_work"),
                                        ("private:12345678", "12345678", "create_account_migration")):
            with self.assertRaisesRegex(ValueError, "task_binding_conflict"):
                self.db.bind_task("pixiv", task_id, scope, actor, operation, now=102)
        self.reopen()
        self.assertEqual(self.db.task("pixiv", task_id)["created"], 100)
        self.assertEqual(self.db.task("pixiv", task_id)["operation"], "archive_work")
        with self.assertRaises(ValueError):
            self.db.bind_task("pixiv", "https://example.test/task", "public", "", "archive_work")

    def test_research_rotates_without_growth_revision_and_restarts_keep_budget(self):
        first = self.research(daily_limit=2)
        self.assertEqual(first["question_key"], "q:one")
        self.db.finish_research(first["question_key"], True, now=101, token=first["token"])
        self.assertIsNone(self.research(now=159, daily_limit=2))
        self.reopen()
        second = self.research(now=160, daily_limit=2)
        self.assertEqual(second["question_key"], "q:two")
        self.db.finish_research(second["question_key"], False, now=161, token=second["token"])
        self.assertIsNone(self.research(now=300, daily_limit=2))
        self.assertEqual(self.db.status()["research"]["daily_count"], 2)
        self.assertIsNotNone(self.research(day_start=86400, now=86500, daily_limit=2))
        self.assertEqual(self.db.status()["research"]["daily_count"], 1)

    def test_failed_research_backoff_and_stale_finish_token(self):
        first = self.research(("q:one",))
        self.db.finish_research("q:one", False, now=101, token=first["token"])
        self.assertIsNone(self.research(("q:one",), now=160))
        second = self.research(("q:one",), now=161)
        with self.assertRaisesRegex(ValueError, "research_lease_lost"):
            self.db.finish_research("q:one", True, now=162, token=first["token"])
        self.db.finish_research("q:one", False, now=162, token=second["token"])
        self.assertIsNone(self.research(("q:one",), now=281))
        self.assertIsNotNone(self.research(("q:one",), now=282))

    def test_unfinished_research_is_not_repeated_until_lease_and_cooldown_expire(self):
        first = self.research(("q:one",))
        self.reopen()
        self.assertIsNone(self.research(("q:one",), now=160))
        second = self.research(("q:one",), now=700)
        self.assertNotEqual(first["token"], second["token"])
        self.assertEqual(second["attempted"], 2)

    def test_concurrent_research_claim_has_one_winner(self):
        barrier = threading.Barrier(2)

        def worker(_):
            db = ToolStore(self.path)
            try:
                barrier.wait(timeout=10)
                return db.claim_research(["q:one", "q:two"], interval_seconds=60,
                                         daily_limit=4, day_start=0, now=100)
            finally:
                db.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(item is not None for item in pool.map(worker, range(2))), 1)

    def test_local_rotation_is_persistent_and_consumes_no_network_budget(self):
        keys = ["q:two", "q:one"]
        self.assertEqual(self.db.next_local_question(keys), "q:one")
        self.reopen()
        self.assertEqual(self.db.next_local_question(keys), "q:two")
        self.assertEqual(self.db.next_local_question(keys), "q:one")
        self.assertEqual(self.db.next_local_question(["q:new"]), "q:new")
        self.assertIsNone(self.db.next_local_question([]))
        status = self.db.status()
        self.assertEqual(status["research"]["daily_count"], 0)
        self.assertEqual(status["research"]["attempts"], 0)
        self.assertEqual(status["call_budget"]["count"], 0)

    def test_status_never_contains_observation_or_result_bodies(self):
        self.observe(text="private-observation-marker")
        call = self.reserve()
        self.db.complete_call(call["id"], result_summary="private-result-marker", now=101)
        text = json.dumps(self.db.status())
        self.assertNotIn("private-observation-marker", text)
        self.assertNotIn("private-result-marker", text)
        self.assertNotIn("12345678", text)

    def test_old_eight_ledger_backup_needs_no_tool_database(self):
        backup = Path(self.temp.name) / "backup"
        backup.mkdir()
        for name, cls in (("living_world", Store), ("brain_book", BrainBook), ("mind", MindStore),
                          ("cognition", CognitiveStore), ("experience_loop", ExperienceLoop),
                          ("proactive_feedback", ProactiveFeedback), ("self_state", SelfState), ("growth", GrowthStore)):
            db = cls(backup / (name+".sqlite3"))
            db.close()
        rows, missing = snapshot(backup)
        self.assertEqual(missing, [])
        self.assertEqual(rows["tool_observations"], [])
        self.assertEqual(rows["tool_research"], [])
        self.assertFalse((backup / "tool_memory.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
