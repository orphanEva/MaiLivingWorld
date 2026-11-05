"""Disposable MySQL variants and concurrent delivery arbitration."""
import concurrent.futures
import os
from pathlib import Path
import tempfile
import threading
import unittest
import uuid

from MaiLivingWorld.shared_reading_store import SharedReadingStore
from test_shared_reading_store import SharedReadingChecks, OWNER, RID


@unittest.skipUnless(os.getenv("MLW_LIVE_MYSQL") == "1", "Only with ephemeral MySQL CI")
class SharedReadingMySQLLiveTests(SharedReadingChecks, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"unused.sqlite3"
        self.settings = {"host": os.environ["MLW_MYSQL_HOST"], "port": int(os.getenv("MLW_MYSQL_PORT", "3306")),
                         "user": os.environ["MLW_MYSQL_USER"], "password": os.environ["MLW_MYSQL_PASSWORD"],
                         "database": os.environ["MLW_MYSQL_DATABASE"], "table_prefix": "shr_"+uuid.uuid4().hex[:10]+"_"}
        self.stores = []

    def store(self):
        value = SharedReadingStore(self.path, mysql_settings=self.settings)
        self.stores.append(value)
        return value

    def tearDown(self):
        for store in self.stores:
            store.close()
        import pymysql
        with pymysql.connect(**{k: v for k, v in self.settings.items() if k != "table_prefix"}, charset="utf8mb4", autocommit=True) as db:
            with db.cursor() as cursor:
                for table in ("shared_reading_receipts", "shared_reading_sessions", "shared_reading_control"):
                    cursor.execute("DROP TABLE IF EXISTS `"+self.settings["table_prefix"]+table+"`")
        self.temp.cleanup()

    def test_concurrent_delivery_is_applied_once(self):
        row = self.prepared(self.store())
        barrier = threading.Barrier(3)
        def deliver(index):
            worker = SharedReadingStore(self.path, mysql_settings=self.settings)
            try:
                barrier.wait(timeout=10)
                return worker.record_delivery(OWNER, "private-one", row["draft"]["text"], "e"*64,
                    reading_id=RID, expected_revision=row["revision"], now=103)
            finally:
                worker.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            self.assertEqual(sum(pool.map(deliver, range(3))), 1)
