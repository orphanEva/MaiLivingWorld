"""Run the same atomic reply-reserve contract on disposable MySQL connections."""
import os
from pathlib import Path
import tempfile
import unittest
import uuid

from MaiLivingWorld.self_state import SelfState
from test_reply_budget import ReplyBudgetContract


@unittest.skipUnless(os.getenv("MLW_LIVE_MYSQL") == "1", "Only with ephemeral MySQL CI")
class ReplyBudgetMySQLLiveTests(ReplyBudgetContract, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"unused.sqlite3"
        self.settings = {"host": os.environ["MLW_MYSQL_HOST"], "port": int(os.getenv("MLW_MYSQL_PORT", "3306")),
            "user": os.environ["MLW_MYSQL_USER"], "password": os.environ["MLW_MYSQL_PASSWORD"],
            "database": os.environ["MLW_MYSQL_DATABASE"], "table_prefix": "rb_"+uuid.uuid4().hex[:10]+"_"}
        self.db = self.new_store()

    def new_store(self):
        return SelfState(self.path, mysql_settings=self.settings)

    def tearDown(self):
        self.db.close()
        import pymysql
        connection = pymysql.connect(**{key: value for key, value in self.settings.items() if key != "table_prefix"},
                                     charset="utf8mb4", autocommit=True)
        try:
            with connection.cursor() as cursor:
                for table in ("self_owner_history", "self_identity"):
                    cursor.execute("DROP TABLE IF EXISTS `"+self.settings["table_prefix"]+table+"`")
        finally:
            connection.close()
            self.temp.cleanup()


if __name__ == "__main__":
    unittest.main()
