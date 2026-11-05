"""Disposable MySQL checks; explicitly enabled only by CI/operator."""
import concurrent.futures
import os
from pathlib import Path
import tempfile
import threading
import unittest
import uuid

from MaiLivingWorld.followthrough_store import FollowthroughStore
from test_followthrough_store import FollowthroughChecks, OWNER


@unittest.skipUnless(os.getenv("MLW_LIVE_MYSQL") == "1", "Only with ephemeral MySQL CI")
class FollowthroughMySQLLiveTests(FollowthroughChecks, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"unused.sqlite3"
        self.settings = {"host": os.environ["MLW_MYSQL_HOST"], "port": int(os.getenv("MLW_MYSQL_PORT", "3306")),
                         "user": os.environ["MLW_MYSQL_USER"], "password": os.environ["MLW_MYSQL_PASSWORD"],
                         "database": os.environ["MLW_MYSQL_DATABASE"], "table_prefix": "flw_"+uuid.uuid4().hex[:10]+"_"}
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.close()
        import pymysql
        connection = pymysql.connect(**{key: value for key, value in self.settings.items() if key != "table_prefix"}, charset="utf8mb4", autocommit=True)
        try:
            with connection.cursor() as cursor:
                for table in ("followthrough_receipts", "followthrough_items", "followthrough_sources", "followthrough_control"):
                    cursor.execute("DROP TABLE IF EXISTS `"+self.settings["table_prefix"]+table+"`")
        finally:
            connection.close()
            self.temp.cleanup()

    def store(self):
        store = FollowthroughStore(self.path, mysql_settings=self.settings)
        self.stores.append(store)
        return store

    def test_concurrent_poll_budget_and_delivery_only_apply_once(self):
        db = self.store()
        row = self.draft(db)
        barrier = threading.Barrier(3)
        def activate(index):
            worker = FollowthroughStore(self.path, mysql_settings=self.settings)
            try:
                barrier.wait(timeout=10)
                return worker.record_delivery(OWNER, row["sid"], row["promise_text"], "d"*64,
                    item_id=row["id"], expected_revision=row["revision"], now=101)
            finally:
                worker.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            self.assertEqual(sum(pool.map(activate, range(3))), 1)
        def claim(index):
            worker = FollowthroughStore(self.path, mysql_settings=self.settings)
            try:
                barrier.wait(timeout=10)
                return worker.claim_poll(OWNER, day_start=0, daily_limit=1, now=102)
            finally:
                worker.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            self.assertEqual(sum(row is not None for row in pool.map(claim, range(3))), 1)
