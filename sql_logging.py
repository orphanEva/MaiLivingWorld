"""可开关的 SQL 诊断：保留 SQL 模板，只输出参数类型，不泄露 QQ 内容、密钥或 URL。

MyBatis 风格：Preparing / Parameters / Updates / Cost。默认关闭。
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from typing import Any


# 屏蔽 SQL 中内嵌的单引号字符串；参数值一律不输出。
_LITERAL = re.compile(r"'(?:''|\\'|[^'])*'")
_SPACE = re.compile(r"\s+")


def safe_template(sql: str) -> str:
    return _SPACE.sub(" ", _LITERAL.sub("'***'", str(sql))).strip()[:800]


def parameter_types(params: Any) -> str:
    if params is None:
        return "[]"
    if isinstance(params, dict):
        values = list(params.values())
    elif isinstance(params, (list, tuple)):
        values = list(params)
    else:
        return "[batch or unsupported]"
    # 不打印值、具体长度和对象 repr，防止手机号、QQ ID、聊天正文泄露。
    result = []
    for item in values[:20]:
        if item is None:
            result.append("null")
        elif isinstance(item, bool):
            result.append("bool(***)")
        elif isinstance(item, int):
            result.append("int(***)")
        elif isinstance(item, float):
            result.append("float(***)")
        elif isinstance(item, (bytes, bytearray)):
            result.append("bytes(***)")
        elif isinstance(item, str):
            result.append("str(***)")
        else:
            result.append(type(item).__name__ + "(***)")
    if len(values) > 20:
        result.append("...")
    return "[" + ", ".join(result) + "]"


class SQLAudit:
    def __init__(self, *, logger: Any = None, enabled: bool = False, backend: str = "sqlite"):
        self.logger = logger or logging.getLogger("MaiLivingWorld.SQL")
        self.enabled = bool(enabled)
        self.backend = backend

    def run(self, sql: str, params: Any, operation) -> Any:
        if not self.enabled:
            return operation()
        self.logger.info("[MaiLivingWorld][SQL][%s] ==> Preparing: %s",
                         self.backend, safe_template(sql))
        self.logger.info("[MaiLivingWorld][SQL][%s] ==> Parameters: %s",
                         self.backend, parameter_types(params))
        start = time.perf_counter()
        try:
            result = operation()
        except Exception as exc:
            self.logger.warning("[MaiLivingWorld][SQL][%s] <== Failed: %s (%.2f ms)",
                                self.backend, type(exc).__name__,
                                (time.perf_counter()-start)*1000)
            raise
        rows = result if isinstance(result, int) else getattr(result, "rowcount", None)
        if isinstance(rows, int) and rows >= 0:
            verb = "Total" if str(sql).lstrip().split(None, 1)[0].upper() in (
                "SELECT", "SHOW", "EXPLAIN", "WITH"
            ) else "Updates"
            self.logger.info("[MaiLivingWorld][SQL][%s] <== %s: %d (%.2f ms)",
                             self.backend, verb, rows, (time.perf_counter()-start)*1000)
        else:
            self.logger.info("[MaiLivingWorld][SQL][%s] <== Executed (%.2f ms)",
                             self.backend, (time.perf_counter()-start)*1000)
        return result


class SQLSQLiteConnection(sqlite3.Connection):
    """sqlite3.Connection 的窄封装，不改变游标、事务与 row_factory 行为。"""
    _sql_audit: SQLAudit | None = None

    def execute(self, sql, parameters=(), /):
        audit = self._sql_audit
        call = lambda: sqlite3.Connection.execute(self, sql, parameters)
        return audit.run(sql, parameters, call) if audit else call()

    def executemany(self, sql, seq_of_parameters, /):
        audit = self._sql_audit
        # executemany 参数可能是生成器，不能消费它们来打印。
        call = lambda: sqlite3.Connection.executemany(self, sql, seq_of_parameters)
        return audit.run(sql, None, call) if audit else call()

    def executescript(self, sql_script, /):
        audit = self._sql_audit
        call = lambda: sqlite3.Connection.executescript(self, sql_script)
        return audit.run(sql_script, None, call) if audit else call()


def connect_sqlite(path: str, *, timeout: float = 10,
                   logger: Any = None, sql_debug: bool = False) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=timeout, factory=SQLSQLiteConnection)
    con._sql_audit = SQLAudit(logger=logger, enabled=sql_debug, backend="sqlite")
    return con


class SQLMySQLCursor:
    """不依赖具体 PyMySQL 版本：只包装 cursor.execute，不接触连接事务。"""
    def __init__(self, cursor: Any, audit: SQLAudit):
        self._cursor = cursor
        self._audit = audit

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self._cursor.__exit__(exc_type, exc_value, traceback)

    def __getattr__(self, name: str):
        return getattr(self._cursor, name)

    def execute(self, sql, args=None):
        return self._audit.run(sql, args, lambda: self._cursor.execute(sql, args))

    def executemany(self, sql, args):
        return self._audit.run(sql, None, lambda: self._cursor.executemany(sql, args))
