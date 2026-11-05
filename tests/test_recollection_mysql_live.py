"""Explicitly enabled disposable MySQL checks for corrections and delivery receipts."""
from __future__ import annotations

import concurrent.futures
import os
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from MaiLivingWorld.growth_store import source_fingerprint
from MaiLivingWorld.recollection_store import RecollectionStore
from test_recollection_store import FragmentCorrectionChecks, OWNER, OTHER, message, proposal, target


@unittest.skipUnless(os.getenv("MLW_LIVE_MYSQL") == "1", "Only with ephemeral MySQL CI")
class RecollectionMySQLLiveTests(FragmentCorrectionChecks, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.settings = {"host": os.environ["MLW_MYSQL_HOST"], "port": int(os.getenv("MLW_MYSQL_PORT", "3306")),
                         "user": os.environ["MLW_MYSQL_USER"], "password": os.environ["MLW_MYSQL_PASSWORD"],
                         "database": os.environ["MLW_MYSQL_DATABASE"], "table_prefix": "rcl_"+uuid.uuid4().hex[:10]+"_"}
        self.stores = []

    def tearDown(self):
        for db in self.stores:
            db.close()
        import pymysql
        connection = pymysql.connect(**{key: value for key, value in self.settings.items() if key != "table_prefix"},
                                     charset="utf8mb4", autocommit=True)
        try:
            with connection.cursor() as cursor:
                for table in ("recollection_deliveries", "recollection_revisions", "recollection_inputs", "recollection_control"):
                    cursor.execute("DROP TABLE IF EXISTS `"+self.settings["table_prefix"]+table+"`")
        finally:
            connection.close()
            self.temp.cleanup()

    def store(self):
        db = RecollectionStore(self.path/"unused.sqlite3", mysql_settings=self.settings)
        self.stores.append(db)
        return db

    def _fragment_store(self):
        if not hasattr(self, "fragment_db"):
            self.fragment_db = self.store()
        return self.fragment_db

    def complete(self, db, job, quote="我现在住在北京", now=102):
        raw = target()
        return db.complete(job["id"], job["token"], proposal(quote=quote), owner_id=OWNER,
                           expected_revision=job["base_revision"], targets={raw["id"]: raw}, now=now)

    def test_empty_constructor_and_fresh_reader_observe_replacement_and_retraction(self):
        writer, reader = self.store(), self.store()
        self.assertEqual(reader.state(OWNER)["revision"], 0)
        self.assertEqual(reader.con.fetch_readonly("SELECT COUNT(*) AS n FROM recollection_control")[0]["n"], 0)
        writer.enqueue(message(), owner_id=OWNER, now=100)
        self.complete(writer, writer.claim(OWNER, now=101))
        first = reader.active(OWNER)[0]
        writer.enqueue(message("new", "我现在住在南京"), owner_id=OWNER, now=200)
        self.complete(writer, writer.claim(OWNER, now=201), quote="我现在住在南京", now=202)
        second = reader.active(OWNER)[0]
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(second["target_digest"], source_fingerprint(target()))
        writer.retract(message("retract", "/记忆 撤回更正"), owner_id=OWNER, revision_id=second["id"], expected_revision=1, now=203)
        self.assertEqual(reader.active(OWNER), [])
        self.assertEqual(reader.active(OTHER), [])
        self.assertEqual(reader.con.fetch_readonly("SELECT status FROM recollection_revisions WHERE id=?", (first["id"],))[0]["status"], "superseded")

    def test_first_and_existing_owner_budget_is_atomic(self):
        self.store()
        barrier = threading.Barrier(3)
        def worker(index):
            db = RecollectionStore(self.path/"worker.sqlite3", mysql_settings=self.settings)
            try:
                results = []
                for day in (0, 86400, 172800):
                    barrier.wait(timeout=10)
                    results.append(db.reserve_cycle(OWNER, day_start=day, daily_limit=1, interval_seconds=0, now=day+100))
                return results
            finally:
                db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            rows = list(pool.map(worker, range(3)))
        self.assertEqual([sum(row[i] for row in rows) for i in range(3)], [1, 1, 1])

    def test_concurrent_message_and_delivery_receipts_apply_once(self):
        self.store()
        barrier = threading.Barrier(3)
        def worker(index):
            db = RecollectionStore(self.path/"worker.sqlite3", mysql_settings=self.settings)
            try:
                barrier.wait(timeout=10)
                ident = db.enqueue(message(), owner_id=OWNER, now=100)
                barrier.wait(timeout=10)
                delivered = db.record_delivery(owner_id=OWNER, scope="private:"+OWNER, receipt_id="verified-send",
                    topic_refs=[{"id": target()["id"], "digest": source_fingerprint(target())}], text_digest="a"*64, now=101)
                return ident, delivered
            finally:
                db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            rows = list(pool.map(worker, range(3)))
        self.assertEqual(len({row[0] for row in rows}), 1)
        self.assertEqual(sum(row[1] for row in rows), 1)
        db = self.store()
        self.assertEqual(db.status(OWNER, now=102)["queued_inputs"], 1)
        self.assertEqual(len(db.recent_deliveries(OWNER, now=102)), 1)

    def test_expired_lease_and_retraction_revoke_stale_writer(self):
        writer, peer = self.store(), self.store()
        writer.enqueue(message(), owner_id=OWNER, now=100)
        first = writer.claim(OWNER, now=101, lease_seconds=1)
        second = peer.claim(OWNER, now=102)
        with self.assertRaisesRegex(ValueError, "lease_conflict"):
            self.complete(writer, first, now=103)
        self.complete(peer, second, now=103)
        revision = writer.active(OWNER)[0]
        writer.enqueue(message("later", "我现在住在南京"), owner_id=OWNER, now=104)
        third = writer.claim(OWNER, now=105)
        peer.retract(message("retract", "/记忆 撤回更正"), owner_id=OWNER, revision_id=revision["id"], expected_revision=1, now=106)
        with self.assertRaisesRegex(ValueError, "lease_conflict"):
            self.complete(writer, third, quote="我现在住在南京", now=107)
        self.assertEqual(writer.active(OWNER), [])

    def test_revision_insert_failure_rolls_back_supersession_and_clearing_input(self):
        writer, peer = self.store(), self.store()
        writer.enqueue(message(), owner_id=OWNER, now=100)
        self.complete(writer, writer.claim(OWNER, now=101))
        first = peer.active(OWNER)[0]
        writer.enqueue(message("new", "我现在住在南京"), owner_id=OWNER, now=200)
        job = writer.claim(OWNER, now=201)
        execute = writer.con.execute
        def fail(sql, parameters=()):
            if sql.startswith("INSERT INTO recollection_revisions"):
                raise RuntimeError("synthetic_insert_failure")
            return execute(sql, parameters)
        writer.con.execute = fail
        try:
            with self.assertRaisesRegex(RuntimeError, "synthetic_insert_failure"):
                self.complete(writer, job, quote="我现在住在南京", now=202)
        finally:
            writer.con.execute = execute
        self.assertEqual(peer.active(OWNER), [first])
        self.assertEqual(peer.state(OWNER)["revision"], 1)
        self.complete(writer, job, quote="我现在住在南京", now=203)
        self.assertEqual(peer.active(OWNER)[0]["quote"], "我现在住在南京")


if __name__ == "__main__":
    unittest.main()
