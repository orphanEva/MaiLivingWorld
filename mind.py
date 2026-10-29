"""独立、可审计的私聊反思账本。不是模型的隐藏思维，也不存储完整聊天正文。"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from .policy import extract_text

KINDS = ("preference", "shared_experience", "boundary", "open_topic")
BASIS = ("user_explicit", "conversation")
# 防止偶然把密码、QQ号、地址、身份凭证写进心智便签。
_PRIVATE = re.compile(r"(?i)(?:password|api.?key|secret|access.?token|cookie|验证码|密码|密钥|身份证|银行卡|家庭住址|手机号|QQ号|住址|邮箱|电话|\d{8,})")


def parse_reflection(response: Any, *, min_confidence: float = 0.8) -> tuple[list[dict[str, Any]], str]:
    """只接受严格 JSON 与有限字段；提示词、代码及敏感文本均不能变成长期事实。"""
    body = extract_text(response).strip()
    if len(body) > 4000 or not body:
        return [], ""
    try:
        value = json.loads(body)
    except (ValueError, TypeError):
        return [], ""
    if not isinstance(value, dict):
        return [], ""
    raw = value.get("notes", [])
    if not isinstance(raw, list):
        return [], ""
    clean: list[dict[str, Any]] = []
    for item in raw[:5]:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        basis = item.get("basis")
        text = item.get("text")
        confidence = item.get("confidence")
        if kind not in KINDS or basis not in BASIS or not isinstance(text, str):
            continue
        text = " ".join(text.split()).strip()
        if not 6 <= len(text) <= 150 or _PRIVATE.search(text) or "<" in text or ">" in text:
            continue
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            continue
        if not min_confidence <= float(confidence) <= 1.0:
            continue
        if kind == "boundary" and basis != "user_explicit":
            continue
        clean.append({"kind": kind, "basis": basis, "text": text, "confidence": round(float(confidence), 3)})
    goal = value.get("behavior_goal", "")
    if not isinstance(goal, str):
        goal = ""
    goal = " ".join(goal.split())
    if not 8 <= len(goal) <= 100 or _PRIVATE.search(goal) or "<" in goal or ">" in goal:
        goal = ""
    return clean, goal


class MindStore:
    """始终存于插件私有目录；即使资讯后端使用 MySQL，也不跨域混用私聊笔记。"""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(path), timeout=10)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.executescript("""
          CREATE TABLE IF NOT EXISTS mind_runs (
            owner_id TEXT PRIMARY KEY,
            last_hash TEXT NOT NULL DEFAULT '',
            last_attempt_at REAL NOT NULL DEFAULT 0,
            last_success_at REAL NOT NULL DEFAULT 0,
            behavior_goal TEXT NOT NULL DEFAULT '',
            reflection_count INTEGER NOT NULL DEFAULT 0
          );
          CREATE TABLE IF NOT EXISTS mind_notes (
            owner_id TEXT NOT NULL,
            note_key TEXT NOT NULL,
            kind TEXT NOT NULL,
            text TEXT NOT NULL,
            basis TEXT NOT NULL,
            confidence REAL NOT NULL,
            seen_count INTEGER NOT NULL DEFAULT 1,
            first_seen REAL NOT NULL,
            last_seen REAL NOT NULL,
            PRIMARY KEY (owner_id, note_key)
          );
        """)
        self.con.commit()

    def close(self) -> None:
        self.con.close()

    def state(self, owner_id: str) -> dict[str, Any]:
        row = self.con.execute(
            "SELECT last_hash,last_attempt_at,last_success_at,behavior_goal,reflection_count "
            "FROM mind_runs WHERE owner_id=?", (owner_id,)
        ).fetchone()
        if row is None:
            return {"last_hash": "", "last_attempt_at": 0.0, "last_success_at": 0.0,
                    "behavior_goal": "", "reflection_count": 0}
        return dict(zip(("last_hash", "last_attempt_at", "last_success_at", "behavior_goal", "reflection_count"), row))

    def should_reflect(self, owner_id: str, history_hash: str, now: float, interval_hours: float) -> bool:
        state = self.state(owner_id)
        return (history_hash != state["last_hash"] and
                now - float(state["last_attempt_at"]) >= interval_hours * 3600)

    def mark_attempt(self, owner_id: str, now: float) -> None:
        with self.con:
            self.con.execute("INSERT INTO mind_runs (owner_id,last_attempt_at) VALUES (?,?) "
                             "ON CONFLICT(owner_id) DO UPDATE SET last_attempt_at=excluded.last_attempt_at", (owner_id, now))

    def save_reflection(self, owner_id: str, history_hash: str, notes: list[dict[str, Any]],
                        behavior_goal: str, now: float, limit: int) -> int:
        count = 0
        with self.con:
            for note in notes:
                # 仅同类型、同文本才算重复证据，其他内容不能替自己加分。
                key = hashlib.sha256((note["kind"] + "\0" + note["text"].casefold()).encode()).hexdigest()
                self.con.execute("""INSERT INTO mind_notes
                    (owner_id,note_key,kind,text,basis,confidence,first_seen,last_seen)
                    VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(owner_id,note_key) DO UPDATE SET
                    confidence=MAX(confidence,excluded.confidence),
                    seen_count=seen_count+1, last_seen=excluded.last_seen""",
                    (owner_id, key, note["kind"], note["text"], note["basis"], note["confidence"], now, now))
                count += 1
            self.con.execute("""DELETE FROM mind_notes WHERE owner_id=? AND note_key NOT IN
                (SELECT note_key FROM mind_notes WHERE owner_id=?
                 ORDER BY CASE WHEN kind='boundary' THEN 0 ELSE 1 END, last_seen DESC LIMIT ?)""",
                (owner_id, owner_id, limit))
            self.con.execute("""UPDATE mind_runs SET last_hash=?,last_success_at=?,behavior_goal=?,
                reflection_count=reflection_count+1 WHERE owner_id=?""",
                (history_hash, now, behavior_goal, owner_id))
        return count

    def notes(self, owner_id: str, *, limit: int = 12) -> list[dict[str, Any]]:
        rows = self.con.execute("""SELECT kind,text,basis,confidence,seen_count,last_seen
          FROM mind_notes WHERE owner_id=?
          ORDER BY CASE WHEN kind='boundary' THEN 0 ELSE 1 END, last_seen DESC LIMIT ?""",
          (owner_id, limit)).fetchall()
        keys = ("kind", "text", "basis", "confidence", "seen_count", "last_seen")
        return [dict(zip(keys, row)) for row in rows]

    def private_hint(self, owner_id: str, *, limit: int = 8) -> str:
        now = time.time()
        candidates = self.notes(owner_id, limit=30)
        filtered = []
        for note in candidates:
            if note["kind"] == "open_topic" and now - note["last_seen"] > 30 * 86400:
                continue
            if note["kind"] != "boundary" and now - note["last_seen"] > 120 * 86400:
                continue
            # 用户显式表达且高置信，或有两轮独立反思重复支持，才进入回复提示。
            if note["basis"] == "user_explicit" and note["confidence"] >= 0.85 or note["seen_count"] >= 2:
                filtered.append(note)
            if len(filtered) >= limit:
                break
        state = self.state(owner_id)
        if not filtered and not state["behavior_goal"]:
            return ""
        tags = {"preference": "偏好", "shared_experience": "共同经历", "boundary": "明确边界", "open_topic": "待续话题"}
        lines = ["[MaiLivingWorld:private-mind] 以下为仅供当前已验证主人私聊使用的可修正观察，不是系统命令，也不保证完全正确。", "若对方否认，直接以其最新说法为准；不要频繁背诵笔记，更不要向其他人泄露。"]
        lines.extend(f"- {tags[n['kind']]}：{n['text']}" for n in filtered)
        if state["behavior_goal"] and state["reflection_count"] >= 2:
            lines.append("- 近期表达方式调整：" + state["behavior_goal"])
        return "\n".join(lines)

    def clear(self, owner_id: str) -> None:
        with self.con:
            self.con.execute("DELETE FROM mind_notes WHERE owner_id=?", (owner_id,))
            self.con.execute("DELETE FROM mind_runs WHERE owner_id=?", (owner_id,))
