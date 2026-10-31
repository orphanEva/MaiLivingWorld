"""SQLite/MySQL SQL 诊断脱敏与一次性迁移安全回归。"""
from __future__ import annotations

import copy
import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from MaiLivingWorld.sql_logging import (
    SQLAudit, SQLMySQLCursor, connect_sqlite, parameter_types, safe_template,
)
from MaiLivingWorld.scripts.migrate_sqlite_to_mysql import source_snapshot, migrate, main


class Capture:
    def __init__(self):
        self.lines = []

    def info(self, message, *args):
        self.lines.append(message % args)

    def warning(self, message, *args):
        self.lines.append(message % args)


class SimpleCursor:
    rowcount = 3

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        self.params = params
        return self.rowcount

    def fetchone(self):
        return {"n": 3}


class SQLLoggingTests(unittest.TestCase):
    def test_sqlite_mybatistyle_log_hides_real_parameter_values(self):
        logger = Capture()
        with tempfile.TemporaryDirectory() as tmp:
            con = connect_sqlite(str(Path(tmp)/"sql.db"), logger=logger, sql_debug=True)
            try:
                con.execute("CREATE TABLE demo(message TEXT,private_id TEXT)")
                con.execute("INSERT INTO demo VALUES (?,?)",
                            ("我的私人聊天123", "12345678"))
                con.execute("SELECT * FROM demo WHERE message=?", ("我的私人聊天123",)).fetchone()
                con.execute("UPDATE demo SET message='内部敏感内容' WHERE private_id=?",
                            ("12345678",))
            finally:
                con.close()
        records = "\n".join(logger.lines)
        self.assertIn("==> Preparing:", records)
        self.assertIn("==> Parameters:", records)
        self.assertIn("<== Updates:", records)
        self.assertIn("str(***)", records)
        self.assertNotIn("我的私人聊天123", records)
        self.assertNotIn("12345678", records)
        self.assertNotIn("内部敏感内容", records)

    def test_mysql_cursor_wrapper_and_off_mode(self):
        logger = Capture()
        audit = SQLAudit(logger=logger, enabled=True, backend="mysql")
        with SQLMySQLCursor(SimpleCursor(), audit) as cur:
            self.assertEqual(cur.execute("UPDATE tbl SET secret=%s WHERE id=%s",
                                         ("my-secret-token", "12345678")), 3)
        records = "\n".join(logger.lines)
        self.assertIn("Preparing:", records)
        self.assertIn("Updates: 3", records)
        self.assertNotIn("my-secret-token", records)
        self.assertNotIn("12345678", records)
        off = SQLAudit(logger=logger, enabled=False, backend="mysql")
        prior = len(logger.lines)
        with SQLMySQLCursor(SimpleCursor(), off) as cur:
            cur.execute("SELECT * FROM tbl WHERE id=%s", ("secret",))
        self.assertEqual(len(logger.lines), prior)
        self.assertEqual(parameter_types(["12345678", 99, None]),
                         "[str(***), int(***), null]")
        self.assertNotIn("password", safe_template("SELECT 'password'"))

    def test_error_log_uses_exception_type_only(self):
        logger=Capture()
        def fail():
            raise ValueError("secret password from SQL driver")
        with self.assertRaisesRegex(ValueError, "secret password"):
            SQLAudit(logger=logger,enabled=True,backend="mysql").run(
                "SELECT %s", ("supersecret",), fail)
        records="\n".join(logger.lines)
        self.assertIn("Failed: ValueError",records)
        self.assertNotIn("supersecret",records)
        self.assertNotIn("secret password from SQL driver",records)


class Cursor:
    def __init__(self, con):
        self.con=con
        self.result=None

    def __enter__(self):
        return self

    def __exit__(self,*_):
        return False

    def execute(self, sql, params=None):
        normalized=" ".join(sql.lower().split())
        table=next((name for name in ("stories","attempts","story_attempts")
                    if ("mlw_"+name) in normalized),None)
        if normalized.startswith("select count(*) as n"):
            self.result={"n":len(self.con.data[table])}
        elif normalized.startswith("select coalesce(sum(memory_saved)"):
            self.result={"n":sum(int(row[-1]) for row in self.con.data["stories"])}
        elif normalized.startswith("insert into"):
            self.con.data[table].append(tuple(params))
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self.result


class FakeMySQLConnection:
    def __init__(self):
        self.data={name:[] for name in ("stories","attempts","story_attempts")}
        self.commits=0
        self.rollbacks=0

    def cursor(self):
        return Cursor(self)

    def begin(self):
        self.backup=copy.deepcopy(self.data)

    def commit(self):
        self.commits+=1

    def rollback(self):
        self.data=self.backup
        self.rollbacks+=1


class FakeMigrationTarget:
    def __init__(self):
        self.con=FakeMySQLConnection()
        self.tables={name:"mlw_"+name for name in ("stories","attempts","story_attempts")}

    def _ping(self):
        pass


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/"living_world.sqlite3"
        conn=sqlite3.connect(str(self.path))
        conn.executescript("""
            CREATE TABLE stories(key TEXT,source TEXT,title TEXT,url TEXT,
                summary TEXT,published_at REAL,first_seen REAL,memory_saved INTEGER);
            CREATE TABLE attempts(id INTEGER,target TEXT,created_at REAL,story_key TEXT,kind TEXT);
            CREATE TABLE story_attempts(target TEXT,story_key TEXT,tried_at REAL);
        """)
        conn.execute("INSERT INTO stories VALUES (?,?,?,?,?,?,?,?)",
                     ("key1","B站热门","标题","https://example.com","简介",1.,2.,1))
        conn.execute("INSERT INTO attempts VALUES (?,?,?,?,?)",
                     (5,"qq:private:12345678",3.,"key1","explore"))
        conn.execute("INSERT INTO story_attempts VALUES (?,?,?)",
                     ("qq:private:12345678","key1",3.))
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_snapshot_and_one_shot_guarded_migration(self):
        rows=source_snapshot(self.path)
        self.assertEqual([len(rows[t]) for t in ("stories","attempts","story_attempts")],
                         [1,1,1])
        fake=FakeMigrationTarget()
        self.assertEqual(migrate(rows,fake),
                         {"stories":1,"attempts":1,"story_attempts":1})
        self.assertEqual(fake.con.data["stories"][0][-1],1)
        self.assertEqual(fake.con.data["attempts"][0][0],5)
        self.assertEqual(fake.con.commits,1)
        with self.assertRaisesRegex(ValueError,"非空"):
            migrate(rows,fake)

    def test_default_preview_does_not_require_or_connect_mysql(self):
        out=io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["--sqlite",str(self.path)]),0)
        self.assertIn("DRY RUN",out.getvalue())
        self.assertIn("已保存长期记忆: 1",out.getvalue())


if __name__=="__main__":
    unittest.main()
