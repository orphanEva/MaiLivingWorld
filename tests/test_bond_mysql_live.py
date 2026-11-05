"""Bond ledger checks for the explicitly enabled disposable MySQL CI service."""
from __future__ import annotations

import concurrent.futures
import os
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from MaiLivingWorld.bond_store import BondStore
from test_bond_store import OWNER, OTHER, message


@unittest.skipUnless(os.getenv("MLW_LIVE_MYSQL") == "1", "Only with ephemeral MySQL CI")
class BondMySQLLiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.settings = {"host": os.environ["MLW_MYSQL_HOST"], "port": int(os.getenv("MLW_MYSQL_PORT", "3306")),
                         "user": os.environ["MLW_MYSQL_USER"], "password": os.environ["MLW_MYSQL_PASSWORD"],
                         "database": os.environ["MLW_MYSQL_DATABASE"], "table_prefix": "bqa_"+uuid.uuid4().hex[:10]+"_"}
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.close()
        import pymysql
        connection = pymysql.connect(**{k: v for k, v in self.settings.items() if k != "table_prefix"},
                                     charset="utf8mb4", autocommit=True)
        try:
            with connection.cursor() as cursor:
                for table in ("bond_audit", "bond_moments", "bond_profiles"):
                    cursor.execute("DROP TABLE IF EXISTS `"+self.settings["table_prefix"]+table+"`")
        finally:
            connection.close()
            self.temp.cleanup()

    def store(self):
        db = BondStore(self.path/"unused.sqlite3", mysql_settings=self.settings)
        self.stores.append(db)
        return db

    def test_profiles_remain_empty_until_a_real_command_and_peer_sees_commits(self):
        writer, reader = self.store(), self.store()
        self.assertEqual(reader.profile(OWNER)["revision"], 0)
        self.assertEqual(reader.con.fetch_readonly("SELECT COUNT(*) AS n FROM bond_profiles")[0]["n"], 0)
        row = writer.command(message(), owner_id=OWNER, action="set_address", value="小明", now=100)
        self.assertEqual(reader.profile(OWNER), row)
        stopped = writer.command(message("m2"), owner_id=OWNER, action="set_affection", value="avoid", now=101)
        self.assertEqual(reader.profile(OWNER), stopped)
        self.assertEqual(reader.profile(OTHER)["preferred_address"], "")

    def test_new_and_existing_profile_lock_preserves_concurrent_boundaries(self):
        self.store()
        barrier = threading.Barrier(3)
        def worker(index):
            db = BondStore(self.path/"worker.sqlite3", mysql_settings=self.settings)
            try:
                results = []
                for round_index in range(2):
                    barrier.wait(timeout=10)
                    key = str(round_index)+"-"+str(index)
                    results.append(db.command(message(key), owner_id=OWNER, action="add_boundary",
                                              value="边界"+key, now=100+round_index)["revision"])
                return results
            finally:
                db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(worker, range(3)))
        self.assertEqual(sorted(value for row in results for value in row), list(range(1, 7)))
        db = self.store()
        self.assertEqual(len(db.profile(OWNER)["boundaries"]), 6)
        self.assertEqual(len(db.audit(OWNER)), 6)

    def test_concurrent_same_message_applies_once(self):
        self.store()
        barrier = threading.Barrier(3)
        def worker(_):
            db = BondStore(self.path/"worker.sqlite3", mysql_settings=self.settings)
            try:
                barrier.wait(timeout=10)
                return db.command(message(), owner_id=OWNER, action="set_affection", value="avoid", now=100)
            finally:
                db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            rows = list(pool.map(worker, range(3)))
        self.assertTrue(all(row["revision"] == 1 for row in rows))
        self.assertEqual(len(self.store().audit(OWNER)), 1)

    def test_moment_retirement_is_visible_and_replayed_creation_cannot_restore_it(self):
        writer, reader = self.store(), self.store()
        row = writer.confirm_moment(message(), owner_id=OWNER, summary="一起读完第一章", kind="anniversary",
                                    occurred_on="2024-02-29", now=100)
        self.assertEqual(reader.moments(OWNER), [row])
        self.assertEqual(reader.moments(OTHER), [])
        retired = writer.retire_moment(message("m2"), owner_id=OWNER, moment_id=row["id"], expected_revision=1, now=101)
        self.assertEqual(reader.moments(OWNER), [])
        self.assertEqual(reader.moments(OWNER, status="retired"), [retired])
        replay = writer.confirm_moment(message(), owner_id=OWNER, summary="一起读完第一章", kind="anniversary",
                                       occurred_on="2024-02-29", now=200)
        self.assertEqual(replay["status"], "retired")

    def test_audit_failure_rolls_back_new_profile_and_memory(self):
        writer = self.store()
        execute = writer.con.execute
        def fail_audit(sql, parameters=()):
            if sql.startswith("INSERT INTO bond_audit"):
                raise RuntimeError("synthetic-audit-failure")
            return execute(sql, parameters)
        writer.con.execute = fail_audit
        try:
            with self.assertRaisesRegex(RuntimeError, "synthetic-audit-failure"):
                writer.confirm_moment(message(), owner_id=OWNER, summary="不能部分提交")
        finally:
            writer.con.execute = execute
        reader = self.store()
        self.assertEqual(reader.profile(OWNER)["revision"], 0)
        self.assertEqual(reader.moments(OWNER), [])
        self.assertEqual(reader.audit(OWNER), [])


if __name__ == "__main__":
    unittest.main()
