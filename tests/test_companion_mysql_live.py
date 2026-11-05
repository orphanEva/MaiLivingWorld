"""Explicitly enabled disposable-MySQL checks for the companion ledger."""
from __future__ import annotations

import concurrent.futures
import os
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from MaiLivingWorld.companion_store import CompanionStore
from MaiLivingWorld.scripts.migrate_all_sqlite_to_mysql import ALL_TABLES, apply, snapshot
from test_companion_store import HASH, OWNER, legacy_backup, ref


@unittest.skipUnless(os.getenv("MLW_LIVE_MYSQL") == "1", "Only with ephemeral MySQL CI")
class CompanionMySQLLiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.settings = {"host": os.environ["MLW_MYSQL_HOST"], "port": int(os.getenv("MLW_MYSQL_PORT", "3306")),
                         "user": os.environ["MLW_MYSQL_USER"], "password": os.environ["MLW_MYSQL_PASSWORD"],
                         "database": os.environ["MLW_MYSQL_DATABASE"], "table_prefix": "cqa_"+uuid.uuid4().hex[:10]+"_"}
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.close()
        import pymysql
        connection = pymysql.connect(**{k: v for k, v in self.settings.items() if k != "table_prefix"},
                                     charset="utf8mb4", autocommit=True)
        try:
            with connection.cursor() as cursor:
                for table in reversed(ALL_TABLES):
                    cursor.execute("DROP TABLE IF EXISTS `"+self.settings["table_prefix"]+table+"`")
        finally:
            connection.close()
            self.temp.cleanup()

    def store(self):
        db = CompanionStore(self.path/"unused.sqlite3", mysql_settings=self.settings)
        self.stores.append(db)
        return db

    def open_reading(self, db):
        return db.open_reading("pixiv", "novel", "1", "v1", owner_id=OWNER, scope="private:"+OWNER,
                               total_units=6, source_refs=[ref()], title="测试作品", now=100)

    def test_cross_connection_reading_notes_pause_and_empty_constructor(self):
        writer, reader = self.store(), self.store()
        self.assertEqual(reader.status()["units"], 0)
        self.assertEqual(reader.con.fetch_readonly("SELECT COUNT(*) AS n FROM companion_control")[0]["n"], 0)
        row = self.open_reading(writer)
        self.assertEqual(reader.reading(row["id"]), row)
        lease = writer.claim_reading(row["id"], owner_id=OWNER, now=101)
        after = writer.complete_unit(row["id"], lease["token"], owner_id=OWNER, expected_revision=lease["revision"],
                                     version="v1", index=0, unit_id="first", source_refs=[ref()], excerpt="甲\n乙",
                                     note={"summary": "已读片段笔记", "quotes": ["甲"]}, note_status="reviewed", now=102)
        self.assertEqual(reader.reading(row["id"])["cursor"], 3)
        self.assertEqual(reader.units(row["id"])[0]["excerpt"], "甲\n乙")
        self.assertEqual(len(reader.notes(row["id"], current_refs=[ref()])), 1)
        writer.check_reading(row["id"], owner_id=OWNER, version="v2", current_refs=[ref()], now=103)
        self.assertEqual(reader.reading(row["id"])["status"], "paused")
        self.assertEqual(reader.notes(row["id"], current_refs=[ref()]), [])
        self.assertEqual(after["status"], "active")

    def test_reading_budget_first_and_existing_control_compete_without_retries(self):
        self.store()
        barrier = threading.Barrier(3)
        def worker(_):
            db = CompanionStore(self.path/"worker.sqlite3", mysql_settings=self.settings)
            try:
                results = []
                for day in (0, 86400, 172800, 259200):
                    barrier.wait(timeout=10)
                    results.append(db.reserve_reading_cycle(owner_id=OWNER, day_start=day,
                                                            interval_seconds=0, daily_limit=1, now=day+100))
                return results
            finally:
                db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(worker, range(3)))
        self.assertEqual([sum(values[day] for values in results) for day in range(4)], [1, 1, 1, 1])

    def test_plan_revision_and_followup_are_atomic_between_connections(self):
        first, second = self.store(), self.store()
        plan = first.propose_plan(owner_id=OWNER, scope="private:"+OWNER, title="共同计划", message_id="propose",
                                    summary_hash=HASH, due_at=150.5, now=100)
        active = first.command_plan(plan["id"], owner_id=OWNER, action="accept", expected_revision=1,
                                    message_id="accept", summary_hash=HASH, now=101)
        with self.assertRaisesRegex(ValueError, "revision_conflict"):
            second.command_plan(plan["id"], owner_id=OWNER, action="cancel", expected_revision=1,
                                message_id="cancel", summary_hash=HASH, now=102)
        self.assertEqual(second.plan(plan["id"]), active)
        self.assertEqual(second.plans(owner_id=OWNER, status="active", due_before=150, oldest=True), [])
        self.assertEqual(second.plans(owner_id=OWNER, status="active", due_before=151, oldest=True)[0]["id"], plan["id"])
        self.assertTrue(first.claim_plan_followup(plan["id"], owner_id=OWNER, expected_revision=2, now=200))
        self.assertFalse(second.claim_plan_followup(plan["id"], owner_id=OWNER, expected_revision=2, now=200))
        self.assertEqual(len(second.history(plan["id"])), 2)

    def test_reading_unit_failure_rolls_back_progress(self):
        db = self.store()
        row = self.open_reading(db)
        lease = db.claim_reading(row["id"], owner_id=OWNER, now=101)
        execute = db.con.execute
        def fail_save(sql, parameters=()):
            if sql.startswith("UPDATE companion_readings SET status="):
                raise RuntimeError("synthetic-update-failure")
            return execute(sql, parameters)
        db.con.execute = fail_save
        try:
            with self.assertRaisesRegex(RuntimeError, "synthetic-update-failure"):
                db.complete_unit(row["id"], lease["token"], owner_id=OWNER, expected_revision=lease["revision"],
                                 version="v1", index=0, unit_id="first", source_refs=[ref()], excerpt="甲乙丙", now=102)
        finally:
            db.con.execute = execute
        peer = self.store()
        self.assertEqual(peer.units(row["id"]), [])
        self.assertEqual(peer.reading(row["id"])["cursor"], 0)

    def test_ten_ledger_migration_preserves_progress_commands_and_budgets(self):
        backup = self.path/"backup"
        backup.mkdir()
        legacy_backup(backup)
        source = CompanionStore(backup/"companion.sqlite3")
        try:
            row = self.open_reading(source)
            lease = source.claim_reading(row["id"], owner_id=OWNER, now=101)
            source.complete_unit(row["id"], lease["token"], owner_id=OWNER, expected_revision=lease["revision"],
                                 version="v1", index=0, unit_id="first", source_refs=[ref()], excerpt="甲乙丙", now=102)
            plan = source.propose_plan(owner_id=OWNER, scope="private:"+OWNER, title="共同计划", message_id="propose",
                                       summary_hash=HASH, now=100)
            source.command_plan(plan["id"], owner_id=OWNER, action="accept", expected_revision=1,
                                message_id="accept", summary_hash=HASH, now=101)
            source.claim_plan_followup(plan["id"], owner_id=OWNER, expected_revision=2, now=200)
            source.reserve_reading_cycle(owner_id=OWNER, day_start=0, interval_seconds=60, daily_limit=1, now=200)
        finally:
            source.close()
        rows, missing = snapshot(backup)
        self.assertEqual(missing, [])
        counts = apply(rows, self.settings)
        self.assertEqual(counts["companion_units"], 1)
        target = self.store()
        self.assertEqual(target.reading(row["id"])["cursor"], 3)
        self.assertEqual(target.plan(plan["id"])["followup_count"], 1)
        replay = target.command_plan(plan["id"], owner_id=OWNER, action="accept", expected_revision=1,
                                      message_id="accept", summary_hash=HASH, now=300)
        self.assertEqual(replay["status"], "active")
        self.assertFalse(target.reserve_reading_cycle(owner_id=OWNER, day_start=0, interval_seconds=0, daily_limit=1, now=300))
        self.assertEqual(target.con.fetch_readonly("SELECT COUNT(*) AS n FROM self_identity")[0]["n"], 0)
        with self.assertRaisesRegex(ValueError, "目标表非空"):
            apply(rows, self.settings)


if __name__ == "__main__":
    unittest.main()
