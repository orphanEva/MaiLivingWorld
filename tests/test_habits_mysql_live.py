"""Real MySQL contracts, enabled only for the disposable CI database."""
from __future__ import annotations

import concurrent.futures
import os
from pathlib import Path
import tempfile
import threading
import unittest
import uuid

from MaiLivingWorld.habits_store import HabitsStore
from test_habits_store import HabitLifecycleChecks, OWNER, message, proposal


@unittest.skipUnless(os.getenv("MLW_LIVE_MYSQL") == "1", "Only with ephemeral MySQL CI")
class HabitsMySQLLiveTests(HabitLifecycleChecks, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"unused.sqlite3"
        self.settings = {"host": os.environ["MLW_MYSQL_HOST"], "port": int(os.getenv("MLW_MYSQL_PORT", "3306")),
                         "user": os.environ["MLW_MYSQL_USER"], "password": os.environ["MLW_MYSQL_PASSWORD"],
                         "database": os.environ["MLW_MYSQL_DATABASE"], "table_prefix": "hab_"+uuid.uuid4().hex[:10]+"_"}
        self.stores = []

    def tearDown(self):
        for db in self.stores:
            db.close()
        import pymysql
        connection = pymysql.connect(**{key: value for key, value in self.settings.items() if key != "table_prefix"},
                                     charset="utf8mb4", autocommit=True)
        try:
            with connection.cursor() as cursor:
                for table in ("habits_audit", "habits_preferences", "habits_inputs", "habits_control"):
                    cursor.execute("DROP TABLE IF EXISTS `"+self.settings["table_prefix"]+table+"`")
        finally:
            connection.close()
            self.temp.cleanup()

    def connection(self):
        db = HabitsStore(self.path, mysql_settings=self.settings)
        self.stores.append(db)
        return db

    def store(self):
        if not hasattr(self, "db"):
            self.db = self.connection()
        return self.db

    def test_new_and_existing_controller_have_one_atomic_budget_winner(self):
        self.store()
        barrier = threading.Barrier(3)
        def worker(index):
            db = HabitsStore(self.path, mysql_settings=self.settings)
            try:
                wins = []
                for day in (0, 86400):
                    barrier.wait(timeout=10)
                    wins.append(db.reserve_cycle(OWNER, day_start=day, interval_seconds=0, daily_limit=1, now=day+100))
                return wins
            finally:
                db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            rows = list(pool.map(worker, range(3)))
        self.assertEqual([sum(row[i] for row in rows) for i in range(2)], [1, 1])

    def test_competing_same_command_has_one_receipt(self):
        self.store()
        barrier = threading.Barrier(3)
        def worker(index):
            db = HabitsStore(self.path, mysql_settings=self.settings)
            try:
                barrier.wait(timeout=10)
                return db.command(message("same", "/习惯 设置 默认 长度 简短"), owner_id=OWNER,
                                  action="set", situation="general", dimension="length", value="short", now=100)
            finally:
                db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(worker, range(3)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.store().state(OWNER)["revision"], 1)

    def test_reader_observes_fresh_preferences_and_expired_lease_reclaimed(self):
        writer, reader = self.store(), self.connection()
        self.assertEqual(reader.preferences(OWNER), [])
        self.assertEqual(reader._read("SELECT COUNT(*) AS n FROM habits_control")[0]["n"], 0)
        writer.enqueue(message(body="以后回复请简短一些。"), owner_id=OWNER, now=100)
        old = writer.claim(OWNER, now=101, lease_seconds=1)
        fresh = reader.claim(OWNER, now=102)
        with self.assertRaisesRegex(ValueError, "lease_conflict"):
            writer.complete(old["id"], old["token"], proposal(old["text"], evidence="explicit"),
                            owner_id=OWNER, expected_revision=0, now=103)
        reader.complete(fresh["id"], fresh["token"], proposal(fresh["text"], evidence="explicit"),
                        owner_id=OWNER, expected_revision=0, now=103)
        self.assertEqual(writer.preferences(OWNER)[0]["value"], "short")
        self.command(action="clear", now=104)
        self.assertEqual(reader.preferences(OWNER, include_inactive=False), [])


if __name__ == "__main__":
    unittest.main()
