"""为已有独立 SQLite 账本提供 PyMySQL 接口适配，不依赖本地 SQLite 运行。

仅转换本仓库固定 SQL 模板；所有运行值仍使用驱动参数绑定。
连接不自动重试写事务，MySQL 异常会向上抛出，不静默回退。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .sql_logging import SQLAudit


TABLES = (
    "brain_runs", "brain_events", "brain_event_evidence", "brain_facts",
    "brain_fact_evidence", "brain_goals", "brain_goal_events",
    "brain_decisions", "brain_cycles", "brain_fact_revisions",
    "mind_runs", "mind_notes", "mind_evidence", "mind_reviews", "mind_snapshots",
    "cognitive_cycles", "cognitive_sources", "cognitive_goals",
    "cognitive_evidence", "cognitive_events",
    "experience_actions", "experience_evidence", "experience_policy",
    "experience_opt_out", "proactive_targets", "proactive_events",
)
LONG_TEXT = {
    "text", "summary", "payload", "url", "reason", "basis", "reflection",
    "observation", "uncertainty", "note", "evidence_ids", "question",
    "behavior_goal", "title", "criterion", "value", "source", "topic",
}
_TABLE_MATCH = re.compile(r"\b(" + "|".join(TABLES) + r")\b")
_UPSERT = re.compile(
    r"\bON\s+CONFLICT\s*\([^)]*\)\s+DO\s+UPDATE\s+SET\s+",
    re.IGNORECASE | re.DOTALL,
)
_EXCLUDED = re.compile(r"\bexcluded\.(\w+)", re.IGNORECASE)
_MAX_TWO = re.compile(r"\bMAX\s*\(\s*(\w+)\s*,", re.IGNORECASE)


class CompatRow(tuple):
    """保持 SQLite Row 的序列兼映射行为，支持 json.dumps 和 dict(row)。"""
    def __new__(cls, data: dict[str, Any]):
        obj = super().__new__(cls, data.values())
        obj._names = tuple(data.keys())
        return obj

    def __getitem__(self, key):
        if isinstance(key, str):
            return super().__getitem__(self._names.index(key))
        return super().__getitem__(key)

    def keys(self):
        return self._names



class Result:
    def __init__(self, cursor=None, rows=None):
        self.cursor = cursor
        self._rows = None if rows is None else list(rows)
        self.rowcount = cursor.rowcount if cursor is not None else 0
        self.lastrowid = cursor.lastrowid if cursor is not None else None

    def fetchall(self):
        if self._rows is not None:
            rows, self._rows = self._rows, []
            return rows
        return [CompatRow(row) for row in self.cursor.fetchall()]

    def fetchone(self):
        if self._rows is not None:
            return self._rows.pop(0) if self._rows else None
        row = self.cursor.fetchone()
        return CompatRow(row) if row is not None else None

    def __iter__(self):
        while True:
            row = self.fetchone()
            if row is None:
                break
            yield row

    def close(self):
        if self.cursor is not None:
            self.cursor.close()


def mysql_create_table(sql: str) -> str:
    """将限定范围内的 SQLite CREATE TABLE 定义变成 MySQL 8.4 InnoDB DDL。"""
    lines = []
    def replace_text(match):
        column = match.group(1).lower()
        # 索引/主键一律短字符串，较长的私聊摘要等用 TEXT。
        rest = line[match.end():].split(",", 1)[0]
        if column in LONG_TEXT:
            typ = ("VARCHAR(1024)" if re.search(r"\bDEFAULT\b", rest, re.I)
                   else "LONGTEXT" if column == "payload" else "TEXT")
        else:
            typ = "VARCHAR(191)"
        return match.group(1) + " " + typ

    for line in sql.strip().splitlines():
        line = re.sub(r"\b(\w+)\s+TEXT\b", replace_text, line, flags=re.I)
        line = re.sub(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
                      "BIGINT PRIMARY KEY AUTO_INCREMENT", line, flags=re.I)
        line = re.sub(r"\bINTEGER\s+PRIMARY\s+KEY\b", "BIGINT PRIMARY KEY", line, flags=re.I)
        line = re.sub(r"\bINTEGER\b", "BIGINT", line, flags=re.I)
        line = re.sub(r"\bREAL\b", "DOUBLE", line, flags=re.I)
        lines.append(line)
    return "\n".join(lines) + " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin"


class MySQLCompatConnection:
    """提供 SQLite 账本所依赖的 execute/transaction/Row 接口。"""

    def __init__(self, *, host: str, port: int, user: str, password: str,
                 database: str, table_prefix: str = "mlw_", logger=None,
                 sql_debug: bool = False, connect_timeout: int = 5):
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,20}", table_prefix):
            raise ValueError("非法的 MySQL 表名前缀")
        import pymysql
        self.prefix = table_prefix
        self.con = pymysql.connect(
            host=host, port=int(port), user=user, password=password,
            database=database, charset="utf8mb4", autocommit=False,
            connect_timeout=int(connect_timeout), read_timeout=10, write_timeout=10,
            cursorclass=pymysql.cursors.DictCursor,
        )
        self.audit = SQLAudit(logger=logger, enabled=sql_debug, backend="mysql")
        self.row_factory = None
        self._in_block = False

    def _tables(self, sql: str) -> str:
        return _TABLE_MATCH.sub(lambda m: self.prefix + m.group(0), sql)

    def _sql(self, sql: str) -> str:
        query = self._tables(sql)
        # MySQL 8.x 的 SIGNAL 是保留关键字；SQLite 允许无引号的同名列。
        query = re.sub(r"(?<![`\w])signal(?![`\w])", "`signal`", query, flags=re.I)
        query = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b",
                       "INSERT IGNORE INTO", query, flags=re.I)
        query = re.sub(r"\bINSERT\s+OR\s+REPLACE\s+INTO\b",
                       "REPLACE INTO", query, flags=re.I)
        query = _UPSERT.sub("ON DUPLICATE KEY UPDATE ", query)
        query = _EXCLUDED.sub(lambda m: "VALUES("+m.group(1)+")", query)
        query = _MAX_TWO.sub(r"GREATEST(\1,", query)
        query = re.sub(r"\blast_insert_rowid\s*\(\s*\)", "LAST_INSERT_ID()", query, flags=re.I)
        query = query.replace("?", "%s")
        # MySQL rejects LIMIT directly inside an IN subquery.
        limited = re.compile(
            r"\bNOT\s+IN\s*\(\s*(SELECT\s+.*?\bLIMIT\s+(?:%s|\d+))\s*\)",
            flags=re.I | re.S,
        )
        return limited.sub(
            lambda m: "NOT IN (SELECT * FROM (" + m.group(1) + ") AS mlw_recent)",
            query,
        )

    def _raw(self, sql: str, parameters=()):
        cursor = self.con.cursor()
        try:
            query = self._sql(sql)
            self.audit.run(query, parameters, lambda: cursor.execute(query, parameters))
            return Result(cursor)
        except BaseException:
            cursor.close()
            raise

    def execute(self, sql: str, parameters=()):
        pragma = re.fullmatch(r"\s*PRAGMA\s+table_info\((\w+)\)\s*", sql, re.I)
        if pragma:
            table = pragma.group(1)
            if table not in TABLES:
                raise ValueError("非法的 PRAGMA 表名")
            cursor = self.con.cursor()
            try:
                cursor.execute("""SELECT COLUMN_NAME AS name FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s
                    ORDER BY ORDINAL_POSITION""", (self.prefix+table,))
                names = [row["name"] for row in cursor.fetchall()]
            finally:
                cursor.close()
            return Result(rows=[CompatRow({"cid":i, "name":name}) for i,name in enumerate(names)])
        if re.match(r"\s*PRAGMA\s+(?:journal_mode|busy_timeout|foreign_keys)\b",sql,re.I):
            return Result(rows=[])
        return self._raw(sql, parameters)

    def executescript(self, script: str):
        for stmt in script.split(";"):
            stmt = stmt.strip()
            if not stmt:
                continue
            if re.match(r"^CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\b",stmt,re.I):
                stmt = mysql_create_table(stmt)
            elif re.match(r"^CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\b",stmt,re.I):
                self._create_index(stmt)
                continue
            else:
                raise ValueError("MySQL 初始化出现不允许的 SQL 结构")
            self._raw(stmt).close()
        self.con.commit()

    def _create_index(self, sql: str):
        hit = re.match(r"CREATE\s+(UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+(\w+)"
                       r"\s+ON\s+(\w+)\s*\(",sql,re.I)
        if not hit:
            raise ValueError("无法解析 MySQL 索引定义")
        index, table = hit.group(2), hit.group(3)
        if table not in TABLES or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",index):
            raise ValueError("非法索引名称")
        cursor = self.con.cursor()
        try:
            cursor.execute("""SELECT 1 FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND INDEX_NAME=%s
                LIMIT 1""", (self.prefix+table,index))
            exists = cursor.fetchone() is not None
        finally:
            cursor.close()
        if not exists:
            create = re.sub(r"\bIF\s+NOT\s+EXISTS\b","",sql, count=1, flags=re.I)
            self._raw(create).close()

    def __enter__(self):
        self._in_block = True
        return self

    def __exit__(self, typ, value, tb):
        self._in_block = False
        if typ is None:
            self.con.commit()
        else:
            self.con.rollback()
        return False

    def commit(self):
        self.con.commit()

    def rollback(self):
        self.con.rollback()

    def close(self):
        self.con.close()

    @property
    def in_transaction(self):
        return self._in_block
