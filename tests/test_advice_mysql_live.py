"""Disposable MySQL variants and concurrent delivery/feedback arbitration."""
import concurrent.futures
import os
from pathlib import Path
import tempfile
import threading
import unittest
import uuid

from MaiLivingWorld.advice_store import AdviceStore
from test_advice_store import AdviceChecks, OWNER, message


@unittest.skipUnless(os.getenv("MLW_LIVE_MYSQL") == "1", "Only with ephemeral MySQL CI")
class AdviceMySQLLiveTests(AdviceChecks, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"unused.sqlite3"
        self.settings = {"host": os.environ["MLW_MYSQL_HOST"], "port": int(os.getenv("MLW_MYSQL_PORT", "3306")),
                         "user": os.environ["MLW_MYSQL_USER"], "password": os.environ["MLW_MYSQL_PASSWORD"],
                         "database": os.environ["MLW_MYSQL_DATABASE"], "table_prefix": "adv_"+uuid.uuid4().hex[:10]+"_"}
        self.stores = []

    def store(self):
        value = AdviceStore(self.path, mysql_settings=self.settings)
        self.stores.append(value)
        return value

    def tearDown(self):
        for store in self.stores:
            store.close()
        import pymysql
        with pymysql.connect(**{k: v for k, v in self.settings.items() if k != "table_prefix"}, charset="utf8mb4", autocommit=True) as db:
            with db.cursor() as cursor:
                for table in ("advice_receipts", "advice_inputs", "advice_items", "advice_control"):
                    cursor.execute("DROP TABLE IF EXISTS `"+self.settings["table_prefix"]+table+"`")
        self.temp.cleanup()

    def test_concurrent_delivery_and_claim_each_have_one_winner(self):
        db = self.store()
        row = self.prepared(db)
        barrier = threading.Barrier(3)
        def send(index):
            worker = AdviceStore(self.path, mysql_settings=self.settings)
            try:
                barrier.wait(timeout=10)
                return worker.record_delivery(OWNER, row["id"], row["revision"], "private-one", row["delivery_text"], "c"*64, now=101)
            finally:
                worker.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            self.assertEqual(sum(pool.map(send, range(3))), 1)
        active = db.get(OWNER, row["id"])
        db.enqueue(message("你说的“先列一张小清单”我没尝试"), OWNER, row["id"], active["revision"], now=102)
        barrier = threading.Barrier(3)
        def claim(index):
            worker = AdviceStore(self.path, mysql_settings=self.settings)
            try:
                barrier.wait(timeout=10)
                return bool(worker.claim(OWNER, day_start=0, interval_seconds=60, daily_limit=1, now=103))
            finally:
                worker.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            self.assertEqual(sum(pool.map(claim, range(3))), 1)
        self.assertEqual(db.state(OWNER)["cycle_count"], 1)
