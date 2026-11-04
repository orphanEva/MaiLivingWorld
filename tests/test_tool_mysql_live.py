"""Tool-ledger MySQL checks; run only against the explicitly enabled CI database."""
from __future__ import annotations

import concurrent.futures
import hashlib
import os
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from MaiLivingWorld.brain_core import BrainBook
from MaiLivingWorld.cognition import CognitiveStore
from MaiLivingWorld.experience_loop import ExperienceLoop
from MaiLivingWorld.growth_store import GrowthStore, source_fingerprint
from MaiLivingWorld.mind import MindStore
from MaiLivingWorld.proactive_feedback import ProactiveFeedback
from MaiLivingWorld.self_state import SelfState
from MaiLivingWorld.store import Store
from MaiLivingWorld.tool_store import ToolStore
from MaiLivingWorld.unified_memory import collect_memories, resolve_memories
from MaiLivingWorld.scripts.migrate_all_sqlite_to_mysql import ALL_TABLES, apply, snapshot

DIGEST = hashlib.sha256(b"test-only-arguments").hexdigest()


@unittest.skipUnless(os.getenv("MLW_LIVE_MYSQL") == "1", "Only with ephemeral MySQL CI")
class ToolMySQLLiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.settings = {
            "host": os.environ["MLW_MYSQL_HOST"],
            "port": int(os.getenv("MLW_MYSQL_PORT", "3306")),
            "user": os.environ["MLW_MYSQL_USER"],
            "password": os.environ["MLW_MYSQL_PASSWORD"],
            "database": os.environ["MLW_MYSQL_DATABASE"],
            "table_prefix": "tqa_"+uuid.uuid4().hex[:10]+"_",
        }
        self.stores = []

    def tearDown(self):
        for db in self.stores:
            db.close()
        import pymysql
        connection = pymysql.connect(
            **{k: v for k, v in self.settings.items() if k != "table_prefix"},
            charset="utf8mb4", autocommit=True)
        try:
            with connection.cursor() as cursor:
                for table in reversed(ALL_TABLES):
                    cursor.execute("DROP TABLE IF EXISTS `"+self.settings["table_prefix"]+table+"`")
        finally:
            connection.close()
            self.temp.cleanup()

    def store(self):
        db = ToolStore(self.path / "unused.sqlite3", mysql_settings=self.settings)
        self.stores.append(db)
        return db

    def reserve(self, store, key="message1", maximum=10, now=100, day=0):
        return store.reserve_call("pixiv", "archive_work", DIGEST, key,
                                  "private:12345678", "12345678", day, maximum, now=now)

    def test_cross_connection_observation_versions_privacy_and_retraction(self):
        writer, reader = self.store(), self.store()
        self.assertEqual(reader.observations(), [])
        first = writer.put_observation("pixiv", "illust:1", "风景画的色彩", "private:12345678", now=100)
        direct = reader.resolve([first["id"]])[0]
        self.assertEqual(source_fingerprint(first), source_fingerprint(direct))
        recalled = collect_memories(tools=reader, query="色彩", now=101)
        self.assertEqual(recalled[0]["id"], first["id"])
        current = writer.put_observation("pixiv", "illust:1", "修订后的绘画资料", "public", sensitivity="public", now=102)
        reread = resolve_memories([first["id"]], tools=reader)[first["id"]]
        self.assertEqual(source_fingerprint(current), source_fingerprint(reread))
        self.assertNotEqual(source_fingerprint(first), source_fingerprint(reread))
        self.assertEqual(reread["sensitivity"], "personal")
        writer.retract_observation("pixiv", "illust:1", now=103)
        self.assertEqual(resolve_memories([first["id"]], tools=reader), {})

    def test_global_call_budget_is_atomic_for_competing_connections(self):
        self.store()
        barrier = threading.Barrier(3)

        def worker(index):
            db = ToolStore(self.path / "worker.sqlite3", mysql_settings=self.settings)
            try:
                barrier.wait(timeout=10)
                return self.reserve(db, "message"+str(index), maximum=1)
            finally:
                db.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(worker, range(3)))
        self.assertEqual(sum(row["allowed"] for row in results), 1)
        db = self.store()
        winner = next(row for row in results if row["allowed"])
        db.complete_call(winner["id"], "uncertain", now=101)
        self.assertFalse(self.reserve(db, "new-message", maximum=1)["allowed"])
        self.assertTrue(self.reserve(db, "tomorrow", maximum=1, now=86500, day=86400)["allowed"])
        self.assertEqual(db.status()["call_budget"]["count"], 1)

    def test_research_counter_resets_by_day_and_rollback_keeps_budget_unspent(self):
        db = self.store()
        original = db.con.execute

        def fail_insert(sql, parameters=()):
            if sql.startswith("INSERT INTO tool_calls"):
                raise RuntimeError("synthetic-insert-failure")
            return original(sql, parameters)

        db.con.execute = fail_insert
        try:
            with self.assertRaisesRegex(RuntimeError, "synthetic-insert-failure"):
                self.reserve(db)
        finally:
            db.con.execute = original
        peer = self.store()
        self.assertEqual(peer.status()["call_budget"]["count"], 0)
        first = db.claim_research(["q:one", "q:two"], interval_seconds=60, daily_limit=1, day_start=0, now=100)
        db.finish_research(first["question_key"], False, token=first["token"], now=101)
        self.assertIsNone(peer.claim_research(["q:one", "q:two"], interval_seconds=60, daily_limit=1, day_start=0, now=200))
        second = peer.claim_research(["q:one", "q:two"], interval_seconds=60, daily_limit=1, day_start=86400, now=86500)
        self.assertEqual(second["question_key"], "q:two")
        self.assertEqual(db.status()["research"]["daily_count"], 1)

    def test_nine_ledger_migration_preserves_dedup_tasks_and_research(self):
        backup = self.path / "backup"
        backup.mkdir()
        for name, cls in (("living_world", Store), ("brain_book", BrainBook),
                          ("mind", MindStore), ("cognition", CognitiveStore),
                          ("experience_loop", ExperienceLoop), ("proactive_feedback", ProactiveFeedback),
                          ("self_state", SelfState), ("growth", GrowthStore)):
            db = cls(backup / (name+".sqlite3"))
            db.close()
        tools = ToolStore(backup / "tool_memory.sqlite3")
        observation = tools.put_observation("pixiv", "illust:1", "绘画资料", "private:12345678", now=100)
        call = self.reserve(tools)
        tools.complete_call(call["id"], "uncertain", now=101)
        tools.bind_task("pixiv", "1"*32, "private:12345678", "12345678", "archive_work", now=100)
        research = tools.claim_research(["q:one"], interval_seconds=600, daily_limit=2, day_start=0, now=100)
        tools.next_local_question(["q:one", "q:two"])
        tools.close()
        rows, missing = snapshot(backup)
        self.assertEqual(missing, [])
        counts = apply(rows, self.settings)
        self.assertEqual(counts["tool_calls"], 1)
        target = self.store()
        self.assertEqual(source_fingerprint(target.resolve([observation["id"]])[0]), source_fingerprint(observation))
        self.assertFalse(self.reserve(target, now=102)["allowed"])
        self.assertEqual(target.task("pixiv", "1"*32)["subject_id"], "12345678")
        target.finish_research("q:one", True, token=research["token"], now=102)
        self.assertEqual(target.next_local_question(["q:one", "q:two"]), "q:two")
        self.assertEqual(target.status()["research"]["daily_count"], 1)
        self.assertEqual(target.con.execute("SELECT COUNT(*) FROM self_identity").fetchone()[0], 0)
        with self.assertRaisesRegex(ValueError, "目标表非空"):
            apply(rows, self.settings)


if __name__ == "__main__":
    unittest.main()
