"""持久的智能体身份与唯一主人绑定；仅接收管理配置，不从聊天内容认主。"""
from __future__ import annotations

import math
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from .mysql_compat import MySQLCompatConnection
from .owner import normalize_qq
from .sql_logging import connect_sqlite


class SelfState:
    """一个数据库命名空间对应一个智能体；构造只建表，身份首次读取时创建。

    惰性创建使迁移程序可以先准备空目标表，再原样导入既有 agent_id。
    主人绑定只允许管理配置调用 configure_owner，不能给模型注册为工具。
    """

    def __init__(self, path: Path, *, logger=None, sql_debug: bool = False,
                 mysql_settings: dict | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._mysql = mysql_settings is not None
        self.con = (MySQLCompatConnection(**mysql_settings, logger=logger, sql_debug=sql_debug)
                    if self._mysql else
                    connect_sqlite(str(path), timeout=10, logger=logger, sql_debug=sql_debug))
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA busy_timeout=10000")
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.executescript("""
        CREATE TABLE IF NOT EXISTS self_identity (
          slot INTEGER PRIMARY KEY CHECK(slot=1),
          agent_id TEXT NOT NULL UNIQUE,
          owner_id TEXT NOT NULL DEFAULT '',
          owner_platform TEXT NOT NULL DEFAULT '',
          revision INTEGER NOT NULL DEFAULT 0,
          budget_day REAL NOT NULL DEFAULT 0,
          thought_count INTEGER NOT NULL DEFAULT 0,
          last_thought REAL NOT NULL DEFAULT 0,
          created REAL NOT NULL,
          updated REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS self_owner_history (
          revision INTEGER PRIMARY KEY,
          agent_id TEXT NOT NULL,
          previous_owner_id TEXT NOT NULL,
          owner_id TEXT NOT NULL,
          owner_platform TEXT NOT NULL,
          changed REAL NOT NULL
        );
        """)
        self.con.commit()

    def close(self) -> None:
        self.con.close()

    def _read_state(self) -> dict[str, Any]:
        row = self.con.execute(
            "SELECT agent_id,owner_id,owner_platform,revision,created,updated,"
            "budget_day,thought_count,last_thought "
            "FROM self_identity WHERE slot=1"
        ).fetchone()
        if row is None:
            raise RuntimeError("智能体身份记录缺失")
        return dict(row)

    def state(self) -> dict[str, Any]:
        """返回数据库实际保留的身份，竞争初始化不会用新 UUID 覆盖旧身份。"""
        now = time.time()
        with self.con:
            self.con.execute(
                "INSERT OR IGNORE INTO self_identity "
                "(slot,agent_id,owner_id,owner_platform,revision,created,updated) "
                "VALUES (1,?,'','',0,?,?)", (str(uuid.uuid4()), now, now)
            )
            return self._read_state()

    def configure_owner(self, owner_id: str, *, now: float | None = None) -> dict[str, Any]:
        """应用显式的主人配置；重复配置幂等，空值/非法值不能抹掉既有绑定。"""
        owner = normalize_qq(owner_id)
        if not owner:
            raise ValueError("主人必须是有效的 QQ 平台用户 ID")
        timestamp = time.time() if now is None else float(now)
        if not math.isfinite(timestamp) or timestamp < 0:
            raise ValueError("主人配置时间必须是有效时间戳")
        self.state()
        with self.con:
            # 先获取写锁再读取版本，避免两个管理配置并发时覆盖或丢失审计。
            if self._mysql:
                self.con.execute("SELECT slot FROM self_identity WHERE slot=1 FOR UPDATE").fetchone()
            else:
                self.con.execute("UPDATE self_identity SET revision=revision WHERE slot=1")
            current = self._read_state()
            if current["owner_id"] == owner and current["owner_platform"] == "qq":
                return current
            revision = int(current["revision"]) + 1
            self.con.execute(
                "UPDATE self_identity SET owner_id=?,owner_platform='qq',revision=?,updated=? WHERE slot=1",
                (owner, revision, timestamp),
            )
            self.con.execute(
                "INSERT INTO self_owner_history "
                "(revision,agent_id,previous_owner_id,owner_id,owner_platform,changed) "
                "VALUES (?,?,?,?,'qq',?)",
                (revision, current["agent_id"], current["owner_id"], owner, timestamp),
            )
            return self._read_state()

    def history(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """管理端审计记录；包含平台 ID，不应拼入模型提示或公开状态输出。"""
        limit = max(1, min(int(limit), 100))
        with self.con:
            rows = self.con.execute(
                "SELECT revision,agent_id,previous_owner_id,owner_id,owner_platform,changed "
                "FROM self_owner_history ORDER BY revision DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(row) for row in rows]

    def reserve_thought(self, *, now: float, day_start: float,
                        min_interval_seconds: int, daily_limit: int,
                        reserve_for_reply: int = 0, purpose: str = "foreground") -> bool:
        """原子预留插件内部模型次数，失败也计数，重启与易主均不重置。

        后台与前台共用总计数；后台只可使用扣除回复预留量后的额度，
        前台可使用完整额度。这不是 Host 最终回复模型的调用限制。
        """
        if (type(reserve_for_reply) is not int or reserve_for_reply < 0
                or not isinstance(purpose, str) or purpose not in {"foreground", "background"}):
            raise ValueError("无效的回复预留预算或调用用途")
        now, day_start = float(now), float(day_start)
        if (not math.isfinite(now) or not math.isfinite(day_start)
                or day_start < 0 or now < day_start
                or min_interval_seconds < 0 or daily_limit < 0):
            raise ValueError("无效的思考预算或时间")
        effective_limit = (max(0, int(daily_limit)-reserve_for_reply)
                           if purpose == "background" else int(daily_limit))
        if effective_limit == 0:
            return False
        self.state()
        with self.con:
            changed = self.con.execute(
                "UPDATE self_identity SET "
                "thought_count=CASE WHEN budget_day=? THEN thought_count+1 ELSE 1 END,"
                "budget_day=?,last_thought=? WHERE slot=1 "
                "AND budget_day<=? AND (thought_count=0 OR last_thought<=?) "
                "AND (budget_day<? OR thought_count<?)",
                (day_start, day_start, now, day_start, now-min_interval_seconds,
                 day_start, effective_limit),
            )
            return changed.rowcount == 1
