"""MySQL 后端的无网络模拟验证。需要真实 MySQL 环境才能做端到端验收。"""
from __future__ import annotations

import copy
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from MaiLivingWorld.explore import Story
from MaiLivingWorld.mysql_store import MySQLStore


class Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.fetched = None
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=None):
        q = " ".join(query.lower().split())
        self.conn.queries.append((q, params))
        self.rowcount = 0
        if q.startswith("create table"):
            return 0
        if q.startswith("insert ignore into `mlw_stories`"):
            key = params[0]
            if key not in self.conn.stories:
                self.conn.stories[key] = dict(zip(
                    ("key", "source", "title", "url", "summary", "published_at", "first_seen"), params
                ), memory_saved=0)
                self.rowcount = 1
            return self.rowcount
        if q.startswith("select count(*) as n from `mlw_stories`"):
            records = self.conn.stories.values()
            self.fetched = {"n": sum(v["memory_saved"] == 1 for v in records) if "where memory_saved=1" in q else len(self.conn.stories)}
        elif q.startswith("delete from `mlw_stories`"):
            for k in sorted(self.conn.stories, key=lambda k: (self.conn.stories[k]["first_seen"], k))[:params[0]]:
                self.conn.stories.pop(k)
        elif q.startswith("select s.* from `mlw_stories`"):
            target, cutoff, _, latest, limit = params
            self.fetched = sorted((dict(s) for s in self.conn.stories.values()
                                   if (target, s["key"]) not in self.conn.story_attempts
                                   and s["first_seen"] >= cutoff
                                   and (s["published_at"] is None or cutoff <= s["published_at"] <= latest)),
                                  key=lambda s: s["first_seen"], reverse=True)[:limit]
        elif q.startswith("select max(created_at) as last_at"):
            day_start, target = params
            target_attempts = [a for a in self.conn.attempts if a["target"] == target]
            self.fetched = {"last_at": max((a["created_at"] for a in target_attempts), default=None),
                            "day_count": sum(a["created_at"] >= day_start for a in target_attempts)}
        elif q.startswith("insert into `mlw_attempts`"):
            self.conn.attempts.append(dict(zip(("target", "created_at", "story_key", "kind"), params)))
            self.rowcount = 1
        elif q.startswith("insert ignore into `mlw_story_attempts`"):
            target, key, when = params
            self.conn.story_attempts.setdefault((target, key), when)
            self.rowcount = 1
        elif q.startswith("update `mlw_stories` set memory_saved=1"):
            if params[0] in self.conn.stories:
                self.conn.stories[params[0]]["memory_saved"] = 1
        elif q.startswith("select count(*) as n from `mlw_attempts`"):
            self.fetched = {"n": len(self.conn.attempts)}
        elif q.startswith("select `key`,source,title,url,first_seen,memory_saved from `mlw_stories`"):
            limit = params[0]
            self.fetched = [{k:s[k] for k in ("key","source","title","url","first_seen","memory_saved")}
                            for s in sorted(self.conn.stories.values(), key=lambda s: s["first_seen"], reverse=True)[:limit]]
        else:
            raise AssertionError(f"Unexpected SQL: {query}")
        return self.rowcount

    def fetchone(self):
        return self.fetched

    def fetchall(self):
        return self.fetched


class FakeConnection:
    def __init__(self):
        self.stories = {}
        self.attempts = []
        self.story_attempts = {}
        self.queries = []
        self.commits = 0
        self.begins = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return Cursor(self)

    def ping(self, reconnect):
        assert reconnect

    def begin(self):
        self.begins += 1

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class MySQLStoreTests(unittest.TestCase):
    def test_mysql_dedup_and_quota(self):
        conn = FakeConnection()
        fake = types.ModuleType("pymysql")
        fake.cursors = types.SimpleNamespace(DictCursor=object)
        fake.connect = lambda **kwargs: conn
        with patch.dict(sys.modules, {"pymysql": fake}):
            db = MySQLStore("mysql", 3306, "bot", "secret", "test", "mlw_")
            now = time.time()
            story = Story("abc", "B站热门", "视频", "https://www.bilibili.com/", "简介", now)
            self.assertEqual(db.save_stories([story, story]), 1)
            self.assertEqual(len(db.candidates("qq:private:123", now, 24)), 1)
            self.assertEqual(db.may_contact("qq:private:123", now, now-3600, 3600, 2), (True, "ok"))
            db.record_attempt("qq:private:123", now, "explore", "abc")
            self.assertEqual(db.candidates("qq:private:123", now, 24), [])
            self.assertEqual(len(db.candidates("qq:private:456", now, 24)), 1)
            self.assertEqual(db.may_contact("qq:private:123", now+1, now-3600, 3600, 2), (False, "cooldown"))
            db.record_attempt("qq:private:123", now+3601, "history", None)
            self.assertEqual(db.may_contact("qq:private:123", now+7200, now-3600, 3600, 2), (False, "daily_limit"))
            db.mark_memory_saved("abc")
            self.assertEqual(db.statistics(), {"stories": 1, "wake_attempts": 2, "memory_saved": 1})
            self.assertEqual(db.recent(1)[0]["title"], "视频")
            self.assertEqual(conn.begins, 4)
            self.assertGreaterEqual(conn.commits, 4)
            self.assertTrue(any("primary key (`target`, `story_key`)" in q for q,_ in conn.queries))
            db.close()
            self.assertTrue(conn.closed)

    def test_reject_invalid_identifier(self):
        with self.assertRaises(ValueError):
            MySQLStore("mysql", 3306, "bot", "pass", "db", "bad;DROP TABLE users")

    def test_missing_mysql_credentials(self):
        with self.assertRaisesRegex(ValueError, "必须填写"):
            MySQLStore("", 3306, "bot", "pass", "db")


if __name__ == "__main__":
    unittest.main()
