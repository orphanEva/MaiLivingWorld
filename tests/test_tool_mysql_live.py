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
from MaiLivingWorld.tool_store import ToolStore, observation_id
from MaiLivingWorld.unified_memory import collect_memories, resolve_memories
from MaiLivingWorld.scripts.migrate_all_sqlite_to_mysql import ALL_TABLES, apply, snapshot

DIGEST = hashlib.sha256(b"test-only-arguments").hexdigest()


class ChatMediaLedgerChecks:
    """Run the same public ledger operations against SQLite and the CI MySQL service."""

    def media_call(self, db, key, *, kind="image", scope="group:20000001", sender="10000001",
                  now=100, day=0, maximum=300):
        digest = hashlib.sha256(key.encode()).hexdigest()
        return db.reserve_call("chat_media", kind, digest, key, scope, sender, day, maximum, now=now)

    def restart(self, db):
        db.close()
        self.stores.remove(db)
        return self.store()

    def test_chat_media_and_mcp_daily_budgets_are_independent_after_restart(self):
        db = self.store()
        first = self.media_call(db, "media-day-0", maximum=1)
        self.assertTrue(first["allowed"])
        db.complete_call(first["id"], "failed", now=101)
        self.assertEqual(db.status()["call_budget"]["count"], 0)

        def mcp(provider, key, day=0):
            return db.reserve_call(provider, "read", DIGEST, key, "private:10000001", "10000001",
                                   day, 1, now=day + 105)

        first_mcp = mcp("pixiv", "mcp-day-0")
        self.assertTrue(first_mcp["allowed"])
        db.complete_call(first_mcp["id"], now=108)
        db = self.restart(db)
        self.assertEqual(self.media_call(db, "media-day-0", maximum=1),
                         {"id": first["id"], "status": "failed", "allowed": False})
        self.assertEqual(self.media_call(db, "another-media", maximum=1)["status"], "budget_exhausted")
        self.assertEqual(mcp("cap", "another-mcp")["status"], "budget_exhausted")
        self.assertEqual(db.status()["calls"], {"reserved": 0, "succeeded": 1, "failed": 0, "uncertain": 0})
        self.assertEqual(db.status()["call_budget"], {"count": 1, "day_start": 0})

        self.assertTrue(mcp("cap", "mcp-day-1", day=86400)["allowed"])
        # The MCP clock has advanced. Yesterday's second media slot is still available.
        self.assertTrue(self.media_call(db, "late-media", maximum=2, now=120)["allowed"])
        self.assertTrue(self.media_call(db, "media-day-1", day=86400, now=86520, maximum=1)["allowed"])
        self.assertEqual(self.media_call(db, "another-late-media", maximum=3)["status"], "budget_exhausted")
        self.assertEqual(db.status()["call_budget"], {"count": 1, "day_start": 86400})

    def test_recent_media_uses_chat_kind_and_time_and_keeps_failed_reads(self):
        db = self.store()
        group, other, private = "group:20000001", "group:20000002", "private:10000001"
        expected = {}
        fixtures = [
            ("old", "image", group, "succeeded", 99),
            ("image1", "image", group, "succeeded", 100),
            ("video", "video", group, "succeeded", 820),
            ("link", "bilibili", group, "succeeded", 850),
            ("image2", "image", group, "failed", 900),
            ("other", "image", other, "succeeded", 930),
            ("private", "image", private, "succeeded", 940),
            ("future", "image", group, "succeeded", 1001),
        ]
        for key, kind, scope, status, stamp in fixtures:
            sender = "10000002" if key == "image2" else "10000001"
            call = self.media_call(db, key, kind=kind, scope=scope, sender=sender, now=stamp)
            self.assertTrue(call["allowed"])
            db.complete_call(call["id"], status, now=stamp)
            expected[key] = {"args_digest": hashlib.sha256(key.encode()).hexdigest(), "status": status}
        # Persist only successful sightings, just as the intake hook does.
        for key, scope in (("image1", group), ("private", private)):
            db.put_observation("chat_media", expected[key]["args_digest"], "图片画面：屋檐上有一只猫。",
                               scope, subject_id="10000001" if scope == private else "", now=950)
        # A different source with the same tool name is never a media candidate.
        self.assertTrue(db.reserve_call("cap", "image", DIGEST, "cap-image", group, "10000001",
                                        0, 1, now=970)["allowed"])
        db = self.restart(db)
        self.assertEqual(db.recent_chat_media(group, ["image"], now=1000),
                         [expected["image2"], expected["image1"]])
        self.assertEqual(db.recent_chat_media(group, ["video", "bilibili"], now=1000),
                         [expected["link"], expected["video"]])
        self.assertEqual(db.recent_chat_media(other, ["image"], now=1000), [expected["other"]])
        self.assertEqual(db.recent_chat_media(private, ["image"], now=1000), [expected["private"]])
        self.assertEqual(db.recent_chat_media("private:10000002", ["image"], now=1000), [])
        self.assertEqual(db.recent_chat_media(group, ["image"], now=2000), [])
        for scope, subject in ((group, ""), (private, "10000001")):
            recent = db.recent_chat_media(scope, ["image"], now=1000)
            records = db.resolve([observation_id("chat_media", row["args_digest"]) for row in recent])
            self.assertEqual([(r["scope"], r["subject_id"], r["evidence_status"]) for r in records],
                             [(scope, subject, "visual_observation")])

    def test_withdrawal_fences_another_writer_and_costs_no_read(self):
        reader, writer = self.store(), self.store()
        key, message_key = (hashlib.sha256(s.encode()).hexdigest() for s in ("picture", "message"))
        scope, author = "group:20000001", "10000001"
        ticket = writer.reserve_call("chat_media", "image", key, key, scope, author, 0, 2,
                                     now=80, media_message_key=message_key)
        self.assertTrue(ticket["allowed"])
        reader.put_observation("chat_media", key, "图中的胡杨树", scope, media_message_key=message_key)
        self.assertTrue(reader.retract_chat_media(message_key, [key], scope, author, now=81))
        self.assertFalse(reader.retract_chat_media(message_key, [key], scope, author, now=82))
        self.assertIsNone(writer.put_observation("chat_media", key, "迟到的图像结果", scope, media_message_key=message_key))
        writer.complete_call(ticket["id"], "failed", now=83)
        self.assertFalse(writer.resolve([observation_id("chat_media", key)]))
        self.assertFalse(writer.reserve_call("chat_media", "image", key, key, scope, author, 0, 2,
                                            now=84, media_message_key=message_key)["allowed"])
        self.assertTrue(self.media_call(writer, "next", maximum=2)["allowed"])

    def test_recent_media_filters_before_limiting_across_a_full_page(self):
        group = "group:20000001"
        db = self.store()
        for stamp in (200, 201, 202):
            call = self.media_call(db, str(stamp), scope=group, now=stamp)
            self.assertTrue(call["allowed"])
            db.complete_call(call["id"], now=stamp)
        # More than a normal 200-row memory scan, all newer and in a different chat.
        for stamp in range(300, 501):
            self.assertTrue(self.media_call(db, str(stamp), scope="group:20000002", now=stamp)["allowed"])
        db = self.restart(db)
        self.assertEqual(db.recent_chat_media(group, ["image"], now=501), [
            {"args_digest": hashlib.sha256(key).hexdigest(), "status": "succeeded"}
            for key in (b"202", b"201")])

    def test_skipped_media_survives_restart_without_spending_a_slot_or_duplicating_a_retry(self):
        group = "group:20000001"
        db = self.store()
        first = self.media_call(db, "first", now=100, maximum=1)
        db.complete_call(first["id"], now=101)
        self.assertFalse(self.media_call(db, "second", now=110, maximum=1)["allowed"])
        self.assertFalse(self.media_call(db, "second", now=111, maximum=0)["allowed"])
        db = self.restart(db)
        rows = db.recent_chat_media(group, ["image"], now=112)
        self.assertEqual([r["status"] for r in rows], ["skipped", "succeeded"])
        self.assertEqual(rows[0]["args_digest"], hashlib.sha256(b"second").hexdigest())
        self.assertEqual(db.recent_chat_media(group, ["video"], now=112), [])
        self.assertEqual(db.recent_chat_media("private:10000001", ["image"], now=112), [])
        second = self.media_call(db, "second", now=120, maximum=2)
        self.assertTrue(second["allowed"])
        db.complete_call(second["id"], now=121)
        self.assertEqual([r["status"] for r in db.recent_chat_media(group, ["image"], now=122)],
                         ["succeeded", "succeeded"])
        self.assertEqual(self.media_call(db, "second", now=123, maximum=0)["status"], "succeeded")
        self.assertEqual(db.status()["call_budget"]["count"], 0)

    def test_multiple_omissions_share_one_unresolvable_marker(self):
        db = self.store()
        group = "group:20000001"
        for n in range(6):
            self.assertFalse(self.media_call(db, str(n), now=150+n, maximum=0)["allowed"])
        db = self.restart(db)
        skipped, = db.recent_chat_media(group, ["image"], now=156)
        self.assertEqual(skipped["status"], "skipped")
        self.assertNotIn(skipped["args_digest"], [hashlib.sha256(str(n).encode()).hexdigest() for n in range(6)])
        allowed = self.media_call(db, "5", now=157, maximum=1)
        self.assertTrue(allowed["allowed"])
        db.complete_call(allowed["id"], now=158)
        self.assertEqual(db.recent_chat_media(group, ["image"], now=160)[1], skipped)
        self.assertEqual(db.recent_chat_media(group, ["image"], now=1056)[0]["status"], "succeeded")


class ChatMediaSQLiteTests(ChatMediaLedgerChecks, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "media.sqlite3"
        self.stores = []

    def store(self):
        db = ToolStore(self.path)
        self.stores.append(db)
        return db

    def tearDown(self):
        for db in self.stores:
            db.close()
        self.temp.cleanup()


@unittest.skipUnless(os.getenv("MLW_LIVE_MYSQL") == "1", "Only with ephemeral MySQL CI")
class ToolMySQLLiveTests(ChatMediaLedgerChecks, unittest.TestCase):
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

    def competing_call_budgets(self, *, existing_control):
        db = self.store()
        if existing_control:
            db.put_observation("pixiv", "illust:seed", "Seed the control row", "public", now=1)
        # Each connection remains open across days. Every round must see the
        # preceding winner's commit, while a new day allows exactly one call.
        barrier = threading.Barrier(3)
        rounds = 6

        def worker(index):
            contender = ToolStore(self.path / "worker.sqlite3", mysql_settings=self.settings)
            try:
                results = []
                for round_index in range(rounds):
                    barrier.wait(timeout=10)
                    day = round_index * 86400
                    results.append(self.reserve(
                        contender, "message-"+str(round_index)+"-"+str(index),
                        maximum=1, now=day+100, day=day))
                return results
            finally:
                contender.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(worker, range(3)))
        for round_index in range(rounds):
            with self.subTest(existing_control=existing_control, round=round_index):
                rows = [worker_results[round_index] for worker_results in results]
                self.assertEqual(sum(row["allowed"] for row in rows), 1)
                self.assertEqual(sum(row["status"] == "budget_exhausted" for row in rows), 2)
        day = (rounds-1) * 86400
        winner = next(worker_results[-1] for worker_results in results if worker_results[-1]["allowed"])
        db.complete_call(winner["id"], "uncertain", now=day+101)
        self.assertFalse(self.reserve(db, "new-message", maximum=1, now=day+102, day=day)["allowed"])
        self.assertTrue(self.reserve(db, "tomorrow", maximum=1, now=day+86500, day=day+86400)["allowed"])
        self.assertEqual(db.status()["call_budget"]["count"], 1)

    def test_global_call_budget_is_atomic_for_competing_connections(self):
        self.competing_call_budgets(existing_control=False)

    def test_global_call_budget_is_atomic_with_existing_control_row(self):
        self.competing_call_budgets(existing_control=True)

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
