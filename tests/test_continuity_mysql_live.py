"""Explicitly enabled disposable MySQL CI checks; no production credentials."""
from __future__ import annotations

import concurrent.futures
import os
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from MaiLivingWorld.continuity_store import ContinuityStore
from test_continuity_store import OWNER, OTHER, message, proposal


@unittest.skipUnless(os.getenv("MLW_LIVE_MYSQL") == "1", "Only with ephemeral MySQL CI")
class ContinuityMySQLLiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.settings = {"host": os.environ["MLW_MYSQL_HOST"], "port": int(os.getenv("MLW_MYSQL_PORT", "3306")),
                         "user": os.environ["MLW_MYSQL_USER"], "password": os.environ["MLW_MYSQL_PASSWORD"],
                         "database": os.environ["MLW_MYSQL_DATABASE"], "table_prefix": "cty_"+uuid.uuid4().hex[:10]+"_"}
        self.stores = []

    def tearDown(self):
        for db in self.stores:
            db.close()
        import pymysql
        connection = pymysql.connect(**{k: v for k, v in self.settings.items() if k != "table_prefix"},
                                     charset="utf8mb4", autocommit=True)
        try:
            with connection.cursor() as cursor:
                for table in ("continuity_audit", "continuity_threads", "continuity_inputs", "continuity_control"):
                    cursor.execute("DROP TABLE IF EXISTS `"+self.settings["table_prefix"]+table+"`")
        finally:
            connection.close()
            self.temp.cleanup()

    def store(self):
        db = ContinuityStore(self.path/"unused.sqlite3", mysql_settings=self.settings)
        self.stores.append(db)
        return db

    def test_empty_constructor_and_peer_reads_see_new_pause_then_expiry(self):
        writer, reader = self.store(), self.store()
        self.assertEqual(reader.state(OWNER)["revision"], 0)
        self.assertEqual(reader.con.fetch_readonly("SELECT COUNT(*) AS n FROM continuity_control")[0]["n"], 0)
        writer.command(message(), owner_id=OWNER, action="contact_pause", value="busy", until=300, now=100)
        self.assertTrue(reader.state(OWNER, 200)["contact"]["active"])
        self.assertFalse(reader.state(OWNER, 300)["contact"]["active"])
        self.assertEqual(reader.state(OTHER, 200)["revision"], 0)

    def test_first_and_existing_owner_row_has_exactly_one_budget_winner(self):
        self.store()
        barrier = threading.Barrier(3)
        def worker(index):
            db = ContinuityStore(self.path/"worker.sqlite3", mysql_settings=self.settings)
            try:
                result = []
                for day in (0, 86400, 172800):
                    barrier.wait(timeout=10)
                    result.append(db.reserve_cycle(OWNER, day_start=day, interval_seconds=0, daily_limit=1, now=day+100))
                return result
            finally:
                db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            rows = list(pool.map(worker, range(3)))
        self.assertEqual([sum(row[i] for row in rows) for i in range(3)], [1, 1, 1])

    def test_concurrent_same_receipt_and_batch_claim_are_atomic(self):
        self.store()
        barrier = threading.Barrier(3)
        def worker(index):
            db = ContinuityStore(self.path/"worker.sqlite3", mysql_settings=self.settings)
            try:
                barrier.wait(timeout=10)
                ident = db.enqueue(message(), owner_id=OWNER, now=100)
                barrier.wait(timeout=10)
                return ident, db.claim(OWNER, now=101)
            finally:
                db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            rows = list(pool.map(worker, range(3)))
        self.assertEqual(len({row[0] for row in rows}), 1)
        self.assertEqual(sum(row[1] is not None for row in rows), 1)
        self.assertEqual(self.store().status(OWNER)["inputs"], {"running": 1})

    def test_expired_lease_and_boundary_cas_prevent_stale_commit(self):
        writer, peer = self.store(), self.store()
        ident = writer.enqueue(message(), owner_id=OWNER, now=100)
        first = writer.claim(OWNER, now=101, lease_seconds=2)
        second = peer.claim(OWNER, now=103)
        with self.assertRaisesRegex(ValueError, "lease_conflict"):
            writer.complete(first["id"], first["token"], proposal(ident), owner_id=OWNER, expected_revision=0, now=104)
        writer.command(message("listen"), owner_id=OWNER, action="support", value="listen", until=200, now=104)
        with self.assertRaisesRegex(ValueError, "revision_conflict"):
            peer.complete(second["id"], second["token"], proposal(ident), owner_id=OWNER, expected_revision=0, now=105)
        self.assertEqual(peer.threads(OWNER), [])

    def test_atomic_audit_and_forget_visible_without_reader_transaction_reset(self):
        writer, reader = self.store(), self.store()
        ident = writer.enqueue(message(), owner_id=OWNER, now=100)
        batch = writer.claim(OWNER, now=101)
        execute = writer.con.execute
        def fail_audit(sql, parameters=()):
            if sql.startswith("INSERT INTO continuity_audit"):
                raise RuntimeError("synthetic-audit-failure")
            return execute(sql, parameters)
        writer.con.execute = fail_audit
        try:
            with self.assertRaisesRegex(RuntimeError, "synthetic-audit-failure"):
                writer.complete(batch["id"], batch["token"], proposal(ident), owner_id=OWNER, expected_revision=0, now=102)
        finally:
            writer.con.execute = execute
        self.assertEqual(reader.threads(OWNER), [])
        self.assertEqual(reader.state(OWNER)["revision"], 0)
        writer.complete(batch["id"], batch["token"], proposal(ident), owner_id=OWNER, expected_revision=0, now=103)
        row = reader.threads(OWNER)[0]
        writer.forget_thread(message("forget"), owner_id=OWNER, thread_id=row["id"], expected_revision=1, now=104)
        self.assertEqual(reader.threads(OWNER, status="all"), [])
        self.assertIsNone(reader.thread(row["id"], owner_id=OWNER))


if __name__ == "__main__":
    unittest.main()
