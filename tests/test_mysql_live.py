"""需要显式 MLW_LIVE_MYSQL=1 才连接真实 MySQL；普通单元测试安全跳过。"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from MaiLivingWorld.explore import Story
from MaiLivingWorld.mysql_store import MySQLStore
from MaiLivingWorld.store import Store
from MaiLivingWorld.scripts.migrate_sqlite_to_mysql import migrate, source_snapshot


@unittest.skipUnless(os.environ.get("MLW_LIVE_MYSQL") == "1",
                     "live MySQL test requires temporary CI database")
class MySQLLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_kwargs = {
            "host": os.environ["MLW_MYSQL_HOST"],
            "port": int(os.environ.get("MLW_MYSQL_PORT", "3306")),
            "user": os.environ["MLW_MYSQL_USER"],
            "password": os.environ["MLW_MYSQL_PASSWORD"],
            "database": os.environ["MLW_MYSQL_DATABASE"],
        }

    def test_live_insert_dedup_attempts_mark_and_query(self):
        db=MySQLStore(**self.db_kwargs,table_prefix="itest_")
        try:
            now=time.time()
            story=Story("live-key-1","B站热门","测试标题","https://example.com/a","测试简介",now)
            self.assertEqual(db.save_stories([story,story]),1)
            self.assertEqual(len(db.candidates("qq:private:12345678",now,24)),1)
            self.assertTrue(db.may_contact("qq:private:12345678",now,now-86400,3600,2)[0])
            db.record_attempt("qq:private:12345678",now,"explore",story.key)
            self.assertEqual(db.candidates("qq:private:12345678",now,24),[])
            db.mark_memory_saved(story.key)
            self.assertEqual(db.statistics(),{"stories":1,"wake_attempts":1,"memory_saved":1})
        finally:
            db.close()

    def test_live_sqlite_migration_is_atomic_and_rejects_nonempty_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"living_world.sqlite3"
            src=Store(path)
            try:
                now=time.time()
                story=Story("migrate-key-1","B站热门","迁移测试","https://example.com/b","内容",now)
                src.save_stories([story])
                src.record_attempt("qq:private:12345678",now,"explore",story.key)
                src.mark_memory_saved(story.key)
            finally:
                src.close()
            rows=source_snapshot(path)
            db=MySQLStore(**self.db_kwargs,table_prefix="imigrate_")
            try:
                self.assertEqual(migrate(rows,db),
                                 {"stories":1,"attempts":1,"story_attempts":1})
                self.assertEqual(db.statistics(),
                                 {"stories":1,"wake_attempts":1,"memory_saved":1})
                with self.assertRaisesRegex(ValueError,"非空"):
                    migrate(rows,db)
            finally:
                db.close()
