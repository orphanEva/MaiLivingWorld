"""Explicitly isolated MySQL topic-receipt concurrency checks."""
import concurrent.futures
import os
from pathlib import Path
import tempfile
import threading
import unittest
import uuid

from MaiLivingWorld.mcp_runtime import digest
from MaiLivingWorld.reference_store import ReferenceStore
from test_reference_store import ReferenceChecks, OWNER, reference


@unittest.skipUnless(os.getenv("MLW_LIVE_MYSQL") == "1", "Only with ephemeral MySQL CI")
class ReferenceMySQLLiveTests(ReferenceChecks, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"unused.sqlite3"
        self.settings = {"host": os.environ["MLW_MYSQL_HOST"], "port": int(os.getenv("MLW_MYSQL_PORT", "3306")),
            "user": os.environ["MLW_MYSQL_USER"], "password": os.environ["MLW_MYSQL_PASSWORD"],
            "database": os.environ["MLW_MYSQL_DATABASE"], "table_prefix": "ref_"+uuid.uuid4().hex[:10]+"_"}
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.close()
        import pymysql
        connection = pymysql.connect(**{key: value for key, value in self.settings.items() if key != "table_prefix"}, charset="utf8mb4", autocommit=True)
        try:
            with connection.cursor() as cursor:
                for table in ("reference_receipts", "reference_control"):
                    cursor.execute("DROP TABLE IF EXISTS `"+self.settings["table_prefix"]+table+"`")
        finally:
            connection.close()
            self.temp.cleanup()

    def store(self):
        db = ReferenceStore(self.path, mysql_settings=self.settings)
        self.stores.append(db)
        return db

    def race(self, action):
        barrier = threading.Barrier(3)
        def invoke(index):
            db = ReferenceStore(self.path, mysql_settings=self.settings)
            try:
                barrier.wait(timeout=10)
                return action(db, index)
            finally:
                db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            return list(pool.map(invoke, range(3)))

    def test_concurrent_delivery_has_one_winner(self):
        db = self.store()
        db.capture(OWNER, digest("input"), digest("question"), now=100)
        applied = self.race(lambda worker, index: worker.delivered(OWNER, digest(["send", index]), digest("text"),
            [reference()], expected_revision=1, input_receipt=digest("input"), ttl_seconds=3600, now=101))
        self.assertEqual(sum(applied), 1)
        self.assertEqual(db.state(OWNER, now=102)["revision"], 2)

    def test_same_input_replay_is_atomic(self):
        db = self.store()
        applied = self.race(lambda worker, index: worker.capture(OWNER, digest("same"), digest("question"), now=100))
        self.assertEqual(sum(applied), 1)
        self.assertEqual(db.state(OWNER)["revision"], 1)
