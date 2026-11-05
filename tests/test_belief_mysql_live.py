"""The opinion revision transaction contract against disposable MySQL 8."""
from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from pathlib import Path

from MaiLivingWorld.growth_store import GrowthStore
from MaiLivingWorld.tests.test_belief_store import BeliefChecks


@unittest.skipUnless(os.getenv("MLW_LIVE_MYSQL") == "1", "Only with ephemeral MySQL CI")
class BeliefMySQLLiveTests(BeliefChecks, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"mysql-placeholder.sqlite3"
        self.settings = {"host": os.environ["MLW_MYSQL_HOST"],
                         "port": int(os.getenv("MLW_MYSQL_PORT", "3306")),
                         "user": os.environ["MLW_MYSQL_USER"], "password": os.environ["MLW_MYSQL_PASSWORD"],
                         "database": os.environ["MLW_MYSQL_DATABASE"],
                         "table_prefix": "bvqa_"+uuid.uuid4().hex[:10]+"_"}
        self.db = self.open_store()

    def open_store(self):
        return GrowthStore(self.path, mysql_settings=self.settings)

    def tearDown(self):
        self.db.close()
        import pymysql
        connection = pymysql.connect(**{k: v for k, v in self.settings.items() if k != "table_prefix"},
                                     charset="utf8mb4", autocommit=True)
        try:
            with connection.cursor() as cursor:
                for name in ("growth_consumed", "growth_history", "growth_jobs", "growth_state"):
                    cursor.execute("DROP TABLE IF EXISTS `"+self.settings["table_prefix"]+name+"`")
        finally:
            connection.close()
            self.temp.cleanup()
