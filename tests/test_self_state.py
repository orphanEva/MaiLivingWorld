from __future__ import annotations

import concurrent.futures
from contextlib import closing
import sqlite3
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from MaiLivingWorld.self_state import SelfState
from MaiLivingWorld.brain_core import BrainBook
from MaiLivingWorld.cognition import CognitiveStore
from MaiLivingWorld.experience_loop import ExperienceLoop
from MaiLivingWorld.mind import MindStore
from MaiLivingWorld.proactive_feedback import ProactiveFeedback
from MaiLivingWorld.store import Store
from MaiLivingWorld.scripts.migrate_all_sqlite_to_mysql import snapshot, ALL_TABLES


class SelfStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.path = self.directory / "self_state.sqlite3"
        self.db = SelfState(self.path)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_constructor_leaves_schema_empty_for_migration(self):
        self.assertEqual(self.db.con.execute("SELECT COUNT(*) FROM self_identity").fetchone()[0], 0)
        self.assertEqual(self.db.history(), [])

    def test_identity_and_owner_survive_reopen(self):
        first = self.db.state()
        self.assertEqual(uuid.UUID(first["agent_id"]).version, 4)
        self.assertEqual(first["owner_id"], "")
        self.assertEqual(first["owner_platform"], "")
        self.assertEqual(first["revision"], 0)
        bound = self.db.configure_owner("12345678", now=100)
        self.db.close()
        self.db = SelfState(self.path)
        self.assertEqual(self.db.state(), bound)
        self.assertEqual(bound["agent_id"], first["agent_id"])
        self.assertEqual(bound["owner_platform"], "qq")

    def test_explicit_owner_change_is_audited_and_idempotent(self):
        first = self.db.configure_owner("12345678", now=100)
        self.assertEqual(self.db.configure_owner(" 12345678 ", now=200), first)
        self.assertEqual(len(self.db.history()), 1)
        second = self.db.configure_owner("87654321", now=300)
        self.assertEqual(second["agent_id"], first["agent_id"])
        self.assertEqual(second["revision"], 2)
        self.assertEqual(second["created"], first["created"])
        self.assertEqual(self.db.history(limit=1)[0]["previous_owner_id"], "12345678")
        self.assertEqual(self.db.history()[1]["previous_owner_id"], "")
        self.assertEqual(self.db.con.execute("SELECT COUNT(*) FROM self_identity").fetchone()[0], 1)

    def test_invalid_owner_or_timestamp_cannot_clear_existing_binding(self):
        original = self.db.configure_owner("12345678", now=100)
        for invalid in ("", "主人", "1234", "１２３４５６", "123456789012345678901", None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.db.configure_owner(invalid)
            self.assertEqual(self.db.state(), original)
        for invalid in (float("nan"), float("inf"), -1):
            with self.subTest(timestamp=invalid), self.assertRaises(ValueError):
                self.db.configure_owner("87654321", now=invalid)
            self.assertEqual(self.db.state(), original)

    def test_owner_and_audit_commit_together(self):
        original = self.db.configure_owner("12345678", now=100)
        with self.db.con:
            self.db.con.execute("DROP TABLE self_owner_history")
        with self.assertRaises(sqlite3.OperationalError):
            self.db.configure_owner("87654321", now=200)
        self.assertEqual(self.db.state(), original)

    def test_two_connections_reuse_one_identity(self):
        other = SelfState(self.path)
        try:
            self.assertEqual(other.state(), self.db.state())
            changed = other.configure_owner("12345678")
            self.assertEqual(self.db.state(), changed)
        finally:
            other.close()
        separate = SelfState(self.directory / "another-instance.sqlite3")
        try:
            self.assertNotEqual(self.db.state()["agent_id"], separate.state()["agent_id"])
        finally:
            separate.close()

    def test_concurrent_owner_updates_keep_one_identity_and_audit_chain(self):
        first = self.db.state()
        barrier = threading.Barrier(3)

        def change(owner):
            connection = SelfState(self.path)
            try:
                barrier.wait(timeout=10)
                return connection.configure_owner(owner)
            finally:
                connection.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(change, ("12345678", "23456789", "34567890")))
        self.assertTrue(all(row["agent_id"] == first["agent_id"] for row in results))
        history = list(reversed(self.db.history()))
        self.assertEqual([row["revision"] for row in history], [1, 2, 3])
        self.assertEqual(history[0]["previous_owner_id"], "")
        for previous, current in zip(history, history[1:]):
            self.assertEqual(current["previous_owner_id"], previous["owner_id"])
        self.assertEqual(self.db.state()["owner_id"], history[-1]["owner_id"])

    def test_concurrent_first_reads_and_budget_reservations_are_atomic(self):
        barrier = threading.Barrier(3)

        def reserve(_):
            connection = SelfState(self.path)
            try:
                barrier.wait(timeout=10)
                identity = connection.state()["agent_id"]
                accepted = connection.reserve_thought(
                    now=1100, day_start=1000, min_interval_seconds=0, daily_limit=1)
                return identity, accepted
            finally:
                connection.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(reserve, range(3)))
        self.assertEqual(len({identity for identity, _ in results}), 1)
        self.assertEqual(sum(accepted for _, accepted in results), 1)
        self.assertEqual(self.db.state()["thought_count"], 1)

    def test_thought_budget_survives_reopen_and_owner_change(self):
        budget = {"day_start": 1000, "min_interval_seconds": 60, "daily_limit": 2}
        self.assertTrue(self.db.reserve_thought(now=1100, **budget))
        self.assertFalse(self.db.reserve_thought(now=1101, **budget))
        self.db.configure_owner("12345678")
        self.db.close()
        self.db = SelfState(self.path)
        self.assertEqual(self.db.state()["thought_count"], 1)
        self.assertFalse(self.db.reserve_thought(now=1159, **budget))
        self.assertTrue(self.db.reserve_thought(now=1160, **budget))
        self.assertFalse(self.db.reserve_thought(now=2000, **budget))
        self.assertTrue(self.db.reserve_thought(now=88000, **{**budget, "day_start": 87400}))
        self.assertEqual(self.db.state()["thought_count"], 1)
        self.assertEqual(self.db.state()["last_thought"], 88000)
        self.assertFalse(self.db.reserve_thought(now=89000, **budget))

    def test_zero_budget_disables_attempts_and_other_connection_cannot_reset_count(self):
        budget = {"now": 1100, "day_start": 1000, "min_interval_seconds": 0, "daily_limit": 0}
        self.assertFalse(self.db.reserve_thought(**budget))
        budget["daily_limit"] = 1
        self.assertTrue(self.db.reserve_thought(**budget))
        other = SelfState(self.path)
        try:
            self.assertFalse(other.reserve_thought(**budget))
            self.assertEqual(other.state()["thought_count"], 1)
        finally:
            other.close()

    def test_old_six_ledger_backup_needs_no_new_identity_file(self):
        old_directory = self.directory / "old-backup"
        old_directory.mkdir()
        for filename, cls in (
            ("living_world", Store), ("brain_book", BrainBook), ("mind", MindStore),
            ("cognition", CognitiveStore), ("experience_loop", ExperienceLoop),
            ("proactive_feedback", ProactiveFeedback),
        ):
            ledger = cls(old_directory / (filename + ".sqlite3"))
            ledger.close()
        rows, missing = snapshot(old_directory)
        self.assertEqual(missing, [])
        self.assertEqual(set(rows), set(ALL_TABLES))
        self.assertEqual(rows["self_identity"], [])
        self.assertEqual(rows["self_owner_history"], [])
        self.assertFalse((old_directory / "self_state.sqlite3").exists())

    def test_identity_snapshot_preserves_original_data(self):
        before = self.db.configure_owner("12345678", now=123)
        self.db.reserve_thought(now=150, day_start=100, min_interval_seconds=1, daily_limit=1)
        rows, _ = snapshot(self.directory)
        self.assertEqual(rows["self_identity"][0]["agent_id"], before["agent_id"])
        self.assertEqual(rows["self_identity"][0]["thought_count"], 1)
        self.assertEqual(rows["self_owner_history"][0]["owner_id"], "12345678")

    def test_optional_identity_file_when_present_is_still_strictly_validated(self):
        broken = self.directory / "incomplete-backup"
        broken.mkdir()
        with closing(sqlite3.connect(broken / "self_state.sqlite3")) as connection:
            connection.execute("CREATE TABLE unrelated(id INTEGER)")
        with self.assertRaisesRegex(RuntimeError, "缺少预期表 self_identity"):
            snapshot(broken)


if __name__ == "__main__":
    unittest.main()
