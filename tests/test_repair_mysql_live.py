"""Repair transactions against an isolated temporary MySQL table namespace."""
import os
from pathlib import Path
import tempfile
import unittest
import uuid

from MaiLivingWorld.repair_store import RepairStore
from MaiLivingWorld.tests.test_repair_store import RepairChecks


@unittest.skipUnless(os.getenv("MLW_LIVE_MYSQL") == "1", "Only with ephemeral MySQL CI")
class RepairMySQLLiveTests(RepairChecks, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"unused.sqlite3"
        self.settings = {"host": os.environ["MLW_MYSQL_HOST"], "port": int(os.getenv("MLW_MYSQL_PORT", "3306")),
            "user": os.environ["MLW_MYSQL_USER"], "password": os.environ["MLW_MYSQL_PASSWORD"],
            "database": os.environ["MLW_MYSQL_DATABASE"], "table_prefix": "repair_"+uuid.uuid4().hex[:10]+"_"}
        self.stores = []

    def store(self):
        value = RepairStore(self.path, mysql_settings=self.settings)
        self.stores.append(value)
        return value

    def tearDown(self):
        for store in self.stores:
            store.close()
        import pymysql
        with pymysql.connect(**{k: v for k, v in self.settings.items() if k != "table_prefix"}, charset="utf8mb4", autocommit=True) as db:
            with db.cursor() as cursor:
                for table in ("repair_receipts", "repair_inputs", "repair_items", "repair_control"):
                    cursor.execute("DROP TABLE IF EXISTS `"+self.settings["table_prefix"]+table+"`")
        self.temp.cleanup()
