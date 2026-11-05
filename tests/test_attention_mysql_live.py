"""Attention ledger checks for the explicitly enabled disposable MySQL CI."""
from __future__ import annotations

import concurrent.futures
import os
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from MaiLivingWorld.attention_store import AttentionStore
from test_attention_store import OWNER, OTHER, READING, RESEARCH, message


@unittest.skipUnless(os.getenv("MLW_LIVE_MYSQL") == "1", "Only with ephemeral MySQL CI")
class AttentionMySQLLiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.settings = {"host": os.environ["MLW_MYSQL_HOST"], "port": int(os.getenv("MLW_MYSQL_PORT", "3306")),
                         "user": os.environ["MLW_MYSQL_USER"], "password": os.environ["MLW_MYSQL_PASSWORD"],
                         "database": os.environ["MLW_MYSQL_DATABASE"], "table_prefix": "att_"+uuid.uuid4().hex[:10]+"_"}
        self.stores = []

    def tearDown(self):
        for db in self.stores:
            db.close()
        import pymysql
        connection = pymysql.connect(**{key: value for key, value in self.settings.items() if key != "table_prefix"},
                                     charset="utf8mb4", autocommit=True)
        try:
            with connection.cursor() as cursor:
                for table in ("attention_events", "attention_receipts", "attention_control"):
                    cursor.execute("DROP TABLE IF EXISTS `"+self.settings["table_prefix"]+table+"`")
        finally:
            connection.close()
            self.temp.cleanup()

    def store(self):
        db = AttentionStore(self.path/"unused.sqlite3", mysql_settings=self.settings)
        self.stores.append(db)
        return db

    def test_empty_constructor_peer_capture_and_mode_reads_are_fresh(self):
        writer, reader = self.store(), self.store()
        self.assertEqual(reader.state(OWNER)["revision"], 0)
        self.assertEqual(reader.con.fetch_readonly("SELECT COUNT(*) AS n FROM attention_control")[0]["n"], 0)
        self.assertTrue(writer.capture(message(), owner_id=OWNER, now=100))
        self.assertEqual(reader.state(OWNER, now=101)["engaged_until"], 190)
        self.assertTrue(writer.set_mode(OWNER, message=message(), mode="listen", until=300, expected_revision=1, now=101))
        self.assertEqual(reader.state(OWNER, now=102)["conversation"]["effective_mode"], "listen")
        self.assertFalse(reader.state(OWNER, now=300)["conversation"]["active"])
        self.assertEqual(reader.state(OTHER)["revision"], 0)

    def test_concurrent_first_and_existing_owner_budget_has_one_winner(self):
        self.store()
        barrier = threading.Barrier(3)
        def worker(index):
            db = AttentionStore(self.path/"worker.sqlite3", mysql_settings=self.settings)
            try:
                result = []
                for day in (0, 86400, 172800):
                    barrier.wait(timeout=10)
                    lease = db.claim(OWNER, candidates=[READING], day_start=day,
                                     daily_limit=1, interval_seconds=0, now=day+100)
                    if lease:
                        db.finish(OWNER, lease["token"], outcome="progress", now=day+101)
                    result.append(bool(lease))
                return result
            finally:
                db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            rows = list(pool.map(worker, range(3)))
        self.assertEqual([sum(row[i] for row in rows) for i in range(3)], [1, 1, 1])

    def test_simultaneous_receipts_do_not_extend_engagement_or_double_count(self):
        self.store()
        barrier = threading.Barrier(3)
        def worker(index):
            db = AttentionStore(self.path/"worker.sqlite3", mysql_settings=self.settings)
            try:
                barrier.wait(timeout=10)
                return db.capture(message(), owner_id=OWNER, now=100)
            finally:
                db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            rows = list(pool.map(worker, range(3)))
        self.assertEqual(sum(rows), 1)
        reader = self.store()
        self.assertFalse(reader.capture(message(), owner_id=OWNER, now=1000))
        self.assertEqual((reader.state(OWNER)["revision"], reader.state(OWNER)["engaged_until"]), (1, 190))
        self.assertEqual(len(reader.events(OWNER)), 1)

    def test_peer_capture_invalidates_lease_and_expired_worker_cannot_commit(self):
        writer, peer = self.store(), self.store()
        options = dict(candidates=[READING, RESEARCH], day_start=0, daily_limit=10, interval_seconds=0)
        first = writer.claim(OWNER, now=100, **options)
        self.assertTrue(peer.valid(OWNER, first["token"], now=101))
        peer.capture(message(), owner_id=OWNER, engagement_seconds=1, now=102)
        self.assertFalse(writer.valid(OWNER, first["token"], now=103))
        self.assertFalse(writer.finish(OWNER, first["token"], outcome="progress", now=103))
        self.assertEqual(peer.state(OWNER)["last_outcome"], "interrupted")
        second = writer.claim(OWNER, now=104, lease_seconds=1, **options)
        third = peer.claim(OWNER, now=105, **options)
        self.assertFalse(writer.finish(OWNER, second["token"], outcome="progress", now=106))
        self.assertTrue(writer.valid(OWNER, third["token"], now=106))

    def test_audit_failure_rolls_back_budget_and_focus_then_restart_keeps_fairness(self):
        writer, reader = self.store(), self.store()
        options = dict(candidates=[READING, RESEARCH], day_start=0, daily_limit=10, interval_seconds=0)
        original = writer._event
        def fail(*args):
            raise RuntimeError("synthetic_audit_failure")
        writer._event = fail
        try:
            with self.assertRaisesRegex(RuntimeError, "synthetic_audit_failure"):
                writer.claim(OWNER, now=100, **options)
        finally:
            writer._event = original
        self.assertEqual((reader.state(OWNER)["cycle_count"], reader.state(OWNER)["focus"]), (0, None))
        for now in (100, 110):
            lease = writer.claim(OWNER, now=now, **options)
            self.assertEqual(lease["focus"], READING)
            writer.finish(OWNER, lease["token"], outcome="progress", now=now+1)
        restored = self.store()
        self.assertEqual(restored.claim(OWNER, now=120, **options)["focus"], RESEARCH)
        self.assertEqual(restored.state(OWNER)["cycle_count"], 3)


if __name__ == "__main__":
    unittest.main()
