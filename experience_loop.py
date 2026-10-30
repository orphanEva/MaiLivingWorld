"""主人私聊主动联系的最小经验闭环。

这里记录的是可验证的事件元数据，不是新的用户画像权威库：
主动尝试 -> Planner/Host结果 -> 同会话主人回复 -> 明确反馈 -> 有界策略调整。
不会保存私聊正文、昵称或媒体；不会把“沉默”直接算作负反馈。
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

OUTCOMES = {
    "submitted", "reply_planned", "planner_stop", "send_confirmed",
    "send_failed", "rejected", "interrupted", "unknown",
}
KINDS = {"explore", "history"}
EXPLICIT = {"positive", "negative", "opt_out"}


def feedback_signal(text: str) -> str:
    """只接受短而明确的主人直接评价，不做情绪猜测和自由文本指令解析。"""
    if not isinstance(text, str) or not text.strip() or len(text) > 90:
        return "reply_only"
    cleaned = re.sub(r"[\s，,。.!！?？～~]+", "", text.strip()).lower()
    if re.fullmatch(r"(?:以后)?(?:请)?(?:别|不要|不用)(?:再)?主动(?:找我|联系我|给我发消息|发消息给我)(?:了|啦)?", cleaned):
        return "opt_out"
    if cleaned in {
        "这个话题没兴趣", "我对这个话题没兴趣", "我对这个没兴趣",
        "不想聊这个话题", "别再提这个话题", "别给我发这种资讯",
        "这种资讯我没兴趣", "这个分享不太合适",
    }:
        return "negative"
    if cleaned in {
        "这个话题很有意思", "这条消息很有用", "这个分享很有用",
        "谢谢你分享", "谢谢提醒", "你的分享很有帮助", "这个话题我喜欢",
    }:
        return "positive"
    return "reply_only"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ExperienceLoop:
    """独立 SQLite 经验账本；仅存 QQ 主人私聊中主动发送的关联记录。"""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(path), timeout=10)
        self.con.execute("PRAGMA busy_timeout=10000")
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.executescript("""
        CREATE TABLE IF NOT EXISTS experience_actions (
            event_id INTEGER PRIMARY KEY,
            owner_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('explore','history')),
            topic_digest TEXT NOT NULL,
            created_at REAL NOT NULL,
            sent_at REAL,
            outcome TEXT NOT NULL DEFAULT 'submitted',
            feedback TEXT NOT NULL DEFAULT 'unrated',
            feedback_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_experience_owner_time
            ON experience_actions(owner_id,session_id,sent_at DESC);
        CREATE TABLE IF NOT EXISTS experience_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            evidence_digest TEXT NOT NULL,
            signal TEXT NOT NULL,
            created_at REAL NOT NULL,
            UNIQUE(event_id,evidence_digest)
        );
        CREATE TABLE IF NOT EXISTS experience_policy (
            owner_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            avoid_until REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(owner_id,kind)
        );
        CREATE TABLE IF NOT EXISTS experience_opt_out (
            owner_id TEXT PRIMARY KEY,
            paused_at REAL NOT NULL
        );
        """)
        self.con.commit()

    def close(self) -> None:
        self.con.close()

    def begin(self, event_id: int, owner_id: str, session_id: str,
              kind: str, topic: str, *, now: float | None = None) -> bool:
        """一个真实 ProactiveFeedback event 对应一个目的明确的尝试。"""
        if not isinstance(event_id, int) or event_id <= 0 or not owner_id or not session_id or kind not in KINDS:
            return False
        timestamp = time.time() if now is None else now
        with self.con:
            cur = self.con.execute("""INSERT OR IGNORE INTO experience_actions
                 (event_id,owner_id,session_id,kind,topic_digest,created_at)
                 VALUES (?,?,?,?,?,?)""",
                 (event_id,owner_id,session_id,kind,digest(topic),timestamp))
            return bool(cur.rowcount)

    def mark(self, event_id: int, outcome: str, *, now: float | None = None) -> bool:
        """只接受可信状态迁移，不能把 stop 或失败当成真实发送。"""
        if outcome not in OUTCOMES - {"submitted"}:
            return False
        timestamp = time.time() if now is None else now
        with self.con:
            row = self.con.execute(
                "SELECT outcome FROM experience_actions WHERE event_id=?", (event_id,)
            ).fetchone()
            if row is None:
                return False
            old = row[0]
            allowed = (
                (old == "submitted" and outcome in OUTCOMES - {"submitted", "send_confirmed", "send_failed"})
                or (old == "reply_planned" and outcome in {"send_confirmed", "send_failed", "interrupted", "unknown"})
            )
            if not allowed:
                return False
            self.con.execute("""UPDATE experience_actions
              SET outcome=?,sent_at=CASE WHEN ?='send_confirmed' THEN ? ELSE sent_at END
              WHERE event_id=?""", (outcome, outcome, timestamp, event_id))
            return True

    def observe(self, owner_id: str, session_id: str, message_id: str, text: str, *,
                now: float | None = None, feedback_hours: int = 24,
                active: bool = False, negative_pause_hours: int = 12) -> str | None:
        """只将发出之后24h内的绑定主人私聊消息关联到上一次确认发送。"""
        if not owner_id or not session_id or not message_id or not isinstance(text, str) or not text.strip():
            return None
        timestamp = time.time() if now is None else now
        signal = feedback_signal(text)
        # 绑定主人要求停止主动联系，不需要先发生一次刚刚成功的主动发送。
        # 但入站身份与真实消息 ID 必须由上层严格核验。
        if signal == "opt_out":
            with self.con:
                self.con.execute("INSERT OR REPLACE INTO experience_opt_out(owner_id,paused_at) VALUES (?,?)",
                                 (owner_id, timestamp))
        row = self.con.execute("""SELECT event_id,kind,feedback FROM experience_actions
            WHERE owner_id=? AND session_id=? AND outcome='send_confirmed'
              AND sent_at<=? AND sent_at>=?
            ORDER BY sent_at DESC, event_id DESC LIMIT 1""",
            (owner_id, session_id, timestamp, timestamp - max(1, min(feedback_hours, 168))*3600)).fetchone()
        if row is None:
            return "opt_out" if signal == "opt_out" else None
        event_id, kind, previous = row
        identity = digest(session_id + "\0" + message_id)
        with self.con:
            cur = self.con.execute("""INSERT OR IGNORE INTO experience_evidence
              (event_id,evidence_digest,signal,created_at) VALUES (?,?,?,?)""",
              (event_id, identity, signal, timestamp))
            if not cur.rowcount:
                return None
            # 第一次普通回复不等于好评；之后收到明确反馈仍可升级为有根据的评价。
            updated = signal if signal in EXPLICIT else (previous if previous != "unrated" else "reply_only")
            if previous in EXPLICIT and signal in EXPLICIT and previous != signal:
                updated = "mixed"  # 矛盾评价不能直接用来优化策略
            self.con.execute("""UPDATE experience_actions SET feedback=?,feedback_at=?
                WHERE event_id=?""", (updated, timestamp, event_id))
            if updated == "opt_out":
                # 直接拒绝主动联系，无论 mode=observe/active 都不得再主动找主人。
                self.con.execute("INSERT OR REPLACE INTO experience_opt_out(owner_id,paused_at) VALUES (?,?)",
                                 (owner_id, timestamp))
            elif active and updated == signal:
                if signal == "negative":
                    self.con.execute("""INSERT INTO experience_policy(owner_id,kind,avoid_until)
                       VALUES (?,?,?) ON CONFLICT(owner_id,kind)
                       DO UPDATE SET avoid_until=MAX(avoid_until,excluded.avoid_until)""",
                       (owner_id, kind, timestamp + min(max(negative_pause_hours, 1), 72) * 3600))
                elif signal == "positive":
                    self.con.execute("""INSERT INTO experience_policy(owner_id,kind,avoid_until)
                        VALUES (?,?,0) ON CONFLICT(owner_id,kind) DO UPDATE SET avoid_until=0""",
                        (owner_id,kind))
        return updated

    def allow(self, owner_id: str, kind: str, *, now: float | None = None,
              active: bool = False) -> tuple[bool, str]:
        """策略仅抑制后续主动联系，不可能提高允许发送的频率。"""
        if not owner_id or kind not in KINDS:
            return False, "invalid_target"
        if self.con.execute("SELECT 1 FROM experience_opt_out WHERE owner_id=?", (owner_id,)).fetchone():
            return False, "owner_opted_out"
        if active:
            row = self.con.execute("SELECT avoid_until FROM experience_policy WHERE owner_id=? AND kind=?",
                                   (owner_id,kind)).fetchone()
            if row and float(row[0]) > (time.time() if now is None else now):
                return False, "explicit_negative_cooldown"
        return True, "ok"

    def latest_sent_event(self, owner_id: str, session_id: str, *, now: float | None = None,
                          feedback_hours: int = 24) -> int | None:
        """复用反馈模块同样的时间窗口，仅暴露内部关联的行动ID。"""
        stamp=time.time() if now is None else now
        row=self.con.execute("""SELECT event_id FROM experience_actions
            WHERE owner_id=? AND session_id=? AND outcome='send_confirmed'
              AND sent_at<=? AND sent_at>=?
            ORDER BY sent_at DESC,event_id DESC LIMIT 1""",
            (owner_id,session_id,stamp,stamp-max(1,min(feedback_hours,168))*3600)).fetchone()
        return int(row[0]) if row else None

    def resume(self, owner_id: str) -> bool:
        """需要上层管理员显式确认；QQ 文本不能自动解除 opt-out。"""
        if not owner_id:
            return False
        with self.con:
            cur = self.con.execute("DELETE FROM experience_opt_out WHERE owner_id=?", (owner_id,))
            return bool(cur.rowcount)

    def status(self, owner_id: str, *, limit: int = 10) -> dict[str, Any]:
        if not owner_id:
            return {"ready": False}
        items = self.con.execute("""SELECT event_id,kind,created_at,outcome,feedback,feedback_at
            FROM experience_actions WHERE owner_id=? ORDER BY event_id DESC LIMIT ?""",
            (owner_id,max(1,min(limit,30)))).fetchall()
        policy = dict(self.con.execute(
            "SELECT kind,avoid_until FROM experience_policy WHERE owner_id=?", (owner_id,)).fetchall())
        paused = self.con.execute("SELECT 1 FROM experience_opt_out WHERE owner_id=?", (owner_id,)).fetchone()
        return {"ready": True, "paused": bool(paused), "avoid_until": policy,
                "recent": [{"event_id": r[0], "kind": r[1], "created_at": r[2],
                            "outcome":r[3], "feedback":r[4], "feedback_at":r[5]} for r in items]}
