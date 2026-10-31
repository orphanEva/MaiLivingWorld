"""校验所有 MySQL 表与列中文注释；独立临时 MySQL 8.4，无需用户正式数据。"""
from __future__ import annotations

import os
import re
import unittest
from pathlib import Path


ROOT=Path(__file__).resolve().parents[1]
DDL=(ROOT/"sql/mysql.sql").read_text(encoding="utf-8")
UPGRADE=(ROOT/"sql/mysql_comments_existing.sql").read_text(encoding="utf-8")
CREATE=re.compile(r"CREATE TABLE IF NOT EXISTS \`(mlw_[a-z_]+)\` \([\s\S]*?\)\s+ENGINE=InnoDB[\s\S]*?;",re.I)
ALTER=re.compile(r"ALTER TABLE \`(mlw_[a-z_]+)\`[\s\S]*?;",re.I)
COLUMN_COMMENT=re.compile(r"\bCOMMENT\s+'(?:''|[^'])*'|\bCOMMENT='(?:''|[^'])*'",re.I)


class SchemaCommentTests(unittest.TestCase):
    def test_29_tables_have_comment_and_matching_update_statements(self):
        creates=CREATE.findall(DDL)
        alters=ALTER.findall(UPGRADE)
        self.assertEqual(len(creates),29)
        self.assertEqual(len(set(creates)),29)
        self.assertEqual(len(alters),29)
        self.assertEqual(set(creates),set(alters))
        for match in CREATE.finditer(DDL):
            statement=match.group(0)
            self.assertRegex(statement,r"COLLATE=\w+ COMMENT='[^']+';")
            self.assertGreaterEqual(statement.count(" COMMENT '"),2)
        self.assertNotRegex(UPGRADE,r"\b(?:DROP TABLE|TRUNCATE|DELETE FROM|INSERT INTO)\b")

    @unittest.skipUnless(os.environ.get("MLW_LIVE_MYSQL")=="1",
                         "require temporary MySQL service")
    def test_live_add_comments_to_preexisting_29_tables(self):
        import pymysql
        connection=pymysql.connect(
            host=os.environ["MLW_MYSQL_HOST"],
            port=int(os.environ.get("MLW_MYSQL_PORT","3306")),
            user=os.environ["MLW_MYSQL_USER"],
            password=os.environ["MLW_MYSQL_PASSWORD"],
            database=os.environ["MLW_MYSQL_DATABASE"],
            charset="utf8mb4",autocommit=True,
            cursorclass=pymysql.cursors.DictCursor,
        )
        # 先模拟已经执行过的无注释 DDL，再用 ALTER 给既有表补全中文注释。
        try:
            with connection.cursor() as cur:
                for match in CREATE.finditer(DDL):
                    sql=match.group(0)
                    sql=COLUMN_COMMENT.sub("",sql)
                    sql=sql.replace("mlw_","commentqa_")
                    cur.execute(sql)
                cur.execute("""SELECT COUNT(*) AS n FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME LIKE 'commentqa\\_%'
                    AND COLUMN_COMMENT!=''""")
                self.assertEqual(cur.fetchone()["n"],0)
                for match in ALTER.finditer(UPGRADE):
                    cur.execute(match.group(0).replace("mlw_","commentqa_"))
                cur.execute("""SELECT TABLE_NAME,COLUMN_NAME,COLUMN_COMMENT
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME LIKE 'commentqa\\_%'""")
                all_columns=cur.fetchall()
                self.assertEqual(len(all_columns),183)
                self.assertTrue(all(row["COLUMN_COMMENT"] for row in all_columns))
                cur.execute("""SELECT TABLE_COMMENT FROM information_schema.TABLES
                    WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME LIKE 'commentqa\\_%'""")
                tables=cur.fetchall()
                self.assertEqual(len(tables),29)
                self.assertTrue(all(row["TABLE_COMMENT"] for row in tables))
                # 再执行一次：可以重复运行，不应破坏现有注释。
                for match in ALTER.finditer(UPGRADE):
                    cur.execute(match.group(0).replace("mlw_","commentqa_"))
        finally:
            connection.close()
