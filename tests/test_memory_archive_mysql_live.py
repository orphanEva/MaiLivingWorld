"""Archive window transactions and readonly searches on real isolated MySQL."""
import os
from pathlib import Path
import tempfile
import unittest
import uuid

from MaiLivingWorld.advice_store import AdviceStore
from MaiLivingWorld.repair_store import RepairStore
from test_memory_archive import ArchiveChecks


@unittest.skipUnless(os.getenv("MLW_LIVE_MYSQL") == "1", "Only with ephemeral MySQL CI")
class MemoryArchiveMySQLTests(ArchiveChecks, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.stores = []
        self.settings = {"host": os.environ["MLW_MYSQL_HOST"], "port": int(os.getenv("MLW_MYSQL_PORT", "3306")),
            "user": os.environ["MLW_MYSQL_USER"], "password": os.environ["MLW_MYSQL_PASSWORD"],
            "database": os.environ["MLW_MYSQL_DATABASE"], "table_prefix": "arc_"+uuid.uuid4().hex[:10]+"_"}

    def store(self, kind):
        value = (AdviceStore if kind == "advice" else RepairStore)(Path(self.temp.name)/(kind+".sqlite3"), mysql_settings=self.settings)
        self.stores.append(value)
        return value

    def tearDown(self):
        for store in self.stores:
            store.close()
        import pymysql
        with pymysql.connect(**{k: v for k, v in self.settings.items() if k != "table_prefix"}, charset="utf8mb4", autocommit=True) as db:
            with db.cursor() as cursor:
                for kind in ("repair", "advice"):
                    for suffix in ("receipts", "inputs", "items", "control"):
                        cursor.execute("DROP TABLE IF EXISTS `"+self.settings["table_prefix"]+kind+"_"+suffix+"`")
        self.temp.cleanup()
