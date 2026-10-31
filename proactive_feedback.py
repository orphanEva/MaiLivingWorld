"""主动聊天回执与退避：分别统计投递请求、Planner 决策及确认发送。

不读取私人聊天文本；本地 SQLite 只保存目标会话 ID 和事件状态。
"""
from __future__ import annotations

import sqlite3

from .sql_logging import connect_sqlite
import time
from pathlib import Path
from typing import Any

TERMINAL = {"planner_stop", "send_confirmed", "send_failed", "rejected", "interrupted", "unknown"}


def decision_from_items(output_items: Any) -> str:
    """只使用 MaiBot 正式的 Context FunctionCallItem 结构判断，不猜测文本。"""
    if not isinstance(output_items, list):
        return ""
    names = []
    for item in output_items:
        if not isinstance(item, dict) or item.get("item_type") != "FunctionCallItem":
            continue
        call = item.get("tool_call")
        if isinstance(call, dict):
            name = call.get("func_name")
            if isinstance(name, str):
                names.append(name.lower().strip())
    if "reply" in names:
        return "reply_planned"
    if "stop" in names:
        return "planner_stop"
    return ""


class ProactiveFeedback:
    """跨重启保留限流和结果；不会把未确认的任务统计成真实消息。"""

    def __init__(self, path: Path, *, logger=None, sql_debug: bool = False):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = connect_sqlite(str(path), timeout=10, logger=logger, sql_debug=sql_debug)
        self.con.execute("PRAGMA busy_timeout=10000")
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.executescript("""
        CREATE TABLE IF NOT EXISTS proactive_targets (
          target TEXT PRIMARY KEY,
          stop_streak INTEGER NOT NULL DEFAULT 0,
          backoff_until REAL NOT NULL DEFAULT 0,
          last_sent_at REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS proactive_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          target TEXT NOT NULL,
          session_id TEXT NOT NULL,
          created_at REAL NOT NULL,
          updated_at REAL NOT NULL,
          outcome TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_proactive_events_time
          ON proactive_events(target,created_at,outcome);
        """)
        self.con.commit()

    def close(self) -> None:
        self.con.close()

    def check(self, target: str, now: float, day_start: float,
              min_sent_gap_seconds: float, max_daily_sends: int) -> tuple[bool, str]:
        row = self.con.execute(
            "SELECT backoff_until,last_sent_at FROM proactive_targets WHERE target=?", (target,)
        ).fetchone()
        if row:
            until, last_sent = row
            if float(until) > now:
                return False, f"planner_stop_backoff_until={int(until)}"
            if last_sent and now - float(last_sent) < min_sent_gap_seconds:
                return False, "confirmed_send_cooldown"
        sent = self.con.execute("""SELECT COUNT(*) FROM proactive_events
          WHERE target=? AND created_at>=? AND outcome='send_confirmed'""",
          (target, day_start)).fetchone()[0]
        if sent >= max_daily_sends:
            return False, "confirmed_send_daily_limit"
        return True, "ok"

    def begin(self, target: str, session_id: str, now: float) -> int:
        with self.con:
            cur = self.con.execute("""INSERT INTO proactive_events
                (target,session_id,created_at,updated_at,outcome)
                VALUES (?,?,?,?,?)""", (target, session_id, now, now, "submitted"))
            return int(cur.lastrowid)

    def outcome(self, event_id: int) -> str:
        row = self.con.execute("SELECT outcome FROM proactive_events WHERE id=?", (event_id,)).fetchone()
        return str(row[0]) if row else ""

    def finish(self, event_id: int, outcome: str, *, now: float | None = None,
               base_backoff_minutes: int = 45, max_backoff_minutes: int = 240) -> bool:
        if outcome not in TERMINAL | {"reply_planned"}:
            return False
        timestamp = time.time() if now is None else now
        with self.con:
            row = self.con.execute(
                "SELECT target,outcome FROM proactive_events WHERE id=?", (event_id,)
            ).fetchone()
            if row is None:
                return False
            target, old = row
            if old in TERMINAL:
                return False
            if old == "reply_planned" and outcome not in {"send_confirmed", "send_failed", "interrupted", "unknown"}:
                return False
            self.con.execute("UPDATE proactive_events SET outcome=?,updated_at=? WHERE id=?",
                             (outcome, timestamp, event_id))
            self.con.execute("INSERT OR IGNORE INTO proactive_targets(target) VALUES(?)", (target,))
            if outcome == "planner_stop":
                prev = self.con.execute("SELECT stop_streak FROM proactive_targets WHERE target=?", (target,)).fetchone()[0]
                streak = min(32, int(prev) + 1)
                delay_min = min(max_backoff_minutes, base_backoff_minutes * (2 ** min(12, streak - 1)))
                self.con.execute("UPDATE proactive_targets SET stop_streak=?,backoff_until=? WHERE target=?",
                                 (streak, timestamp + delay_min * 60, target))
            elif outcome == "send_confirmed":
                self.con.execute("UPDATE proactive_targets SET stop_streak=0,backoff_until=0,last_sent_at=? WHERE target=?",
                                 (timestamp, target))
            return True

    def expire_unfinished(self, *, now: float | None = None, timeout_seconds: int = 240) -> int:
        t = time.time() if now is None else now
        with self.con:
            cur = self.con.execute("""UPDATE proactive_events SET outcome='unknown',updated_at=?
                WHERE outcome IN ('submitted','reply_planned') AND created_at<?""",
                (t, t - timeout_seconds))
            return cur.rowcount

    def status(self, target: str, *, day_start: float) -> dict[str, Any]:
        row = self.con.execute("SELECT stop_streak,backoff_until,last_sent_at FROM proactive_targets WHERE target=?",
                               (target,)).fetchone()
        counts = dict(self.con.execute("SELECT outcome,COUNT(*) FROM proactive_events WHERE target=? AND created_at>=? GROUP BY outcome",
                                       (target, day_start)).fetchall())
        return {"target": target, "stop_streak": int(row[0]) if row else 0,
                "backoff_until": float(row[1]) if row else 0,
                "last_sent_at": float(row[2]) if row else 0,
                "today": counts}
