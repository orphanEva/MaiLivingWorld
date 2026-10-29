"""有限自主认知：公开证据驱动的问题跟踪、观察和复盘。

不是意识/思维链；不会访问私人聊天、发消息、调用外部工具或修改人格。
模型只提交 JSON 候选，程序校验来源、预算、状态和权限。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from .policy import extract_text

ACTIONS = {"wait_for_update", "compare_public_sources", "ask_when_relevant"}
DECISIONS = {"resolved", "discarded", "reopen"}
INVALID = re.compile(r"(?i)(?:password|api.?key|access.?token|cookie|验证码|密码|密钥|身份证|银行卡|家庭住址|手机号|qq号|\d{8,})")


def _clean(value: Any, limit: int, min_length: int = 4) -> str:
    if not isinstance(value, str):
        return ""
    value = " ".join(value.split()).strip()
    if not min_length <= len(value) <= limit or INVALID.search(value) or any(x in value for x in ("<", ">", "{", "}", "```")):
        return ""
    return value


def parse_cognition(result: Any, sources: list[dict[str, Any]], existing: list[dict[str, Any]],
                    *, new_limit: int = 2) -> tuple[list[dict[str, str]], list[dict[str, str]]] | None:
    """严格匹配来自 Host 已保存的公开素材 key 和现存问题 id；无法解析时不写入。"""
    raw = extract_text(result).strip()
    if not raw or len(raw) > 6500:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    keys = {str(x["key"]) for x in sources if isinstance(x, dict) and x.get("key")}
    ids = {str(x["goal_id"]) for x in existing if isinstance(x, dict) and x.get("goal_id") and x.get("status") == "open"}
    if not isinstance(data.get("new", []), list) or not isinstance(data.get("updates", []), list):
        return None
    added: list[dict[str, str]] = []
    seen_sources = set()
    for item in data.get("new", [])[:new_limit]:
        if not isinstance(item, dict):
            continue
        source_key = item.get("source_key")
        if not isinstance(source_key, str) or source_key not in keys or source_key in seen_sources:
            continue
        topic = _clean(item.get("topic"), 36, 2)
        question = _clean(item.get("question"), 120, 6)
        observation = _clean(item.get("observation"), 180, 8)
        uncertainty = _clean(item.get("uncertainty"), 150, 6)
        next_action = item.get("next_action")
        if not (topic and question and observation and uncertainty and next_action in ACTIONS):
            continue
        seen_sources.add(source_key)
        added.append({"source_key": source_key, "topic": topic, "question": question,
                      "observation": observation, "uncertainty": uncertainty, "next_action": next_action})
    updates: list[dict[str, str]] = []
    seen_updates = set()
    for item in data.get("updates", [])[:3]:
        if not isinstance(item, dict):
            continue
        gid, source_key = item.get("goal_id"), item.get("source_key")
        if not isinstance(gid, str) or gid not in ids or not isinstance(source_key, str) or source_key not in keys:
            continue
        if (gid, source_key) in seen_updates or source_key in seen_sources:
            continue
        observation = _clean(item.get("observation"), 180, 8)
        uncertainty = _clean(item.get("uncertainty"), 150, 6)
        next_action = item.get("next_action")
        if not observation or not uncertainty or next_action not in ACTIONS:
            continue
        seen_updates.add((gid, source_key))
        updates.append({"goal_id": gid, "source_key": source_key, "observation": observation,
                        "uncertainty": uncertainty, "next_action": next_action})
    return added, updates


class CognitiveStore:
    """独立 SQLite，只保存公开信息的元数据、来源链接、候选问题和审核事件。"""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(path), timeout=10)
        self.con.execute("PRAGMA busy_timeout=10000")
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.executescript("""
          CREATE TABLE IF NOT EXISTS cognitive_cycles (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              attempted_at REAL NOT NULL, status TEXT NOT NULL,
              input_count INTEGER NOT NULL DEFAULT 0, added INTEGER NOT NULL DEFAULT 0,
              updated INTEGER NOT NULL DEFAULT 0
          );
          CREATE TABLE IF NOT EXISTS cognitive_sources (
              source_key TEXT PRIMARY KEY, seen_at REAL NOT NULL
          );
          CREATE TABLE IF NOT EXISTS cognitive_goals (
              goal_id TEXT PRIMARY KEY, topic TEXT NOT NULL, question TEXT NOT NULL,
              observation TEXT NOT NULL, uncertainty TEXT NOT NULL,
              next_action TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
              created_at REAL NOT NULL, updated_at REAL NOT NULL,
              revision INTEGER NOT NULL DEFAULT 1
          );
          CREATE TABLE IF NOT EXISTS cognitive_evidence (
              goal_id TEXT NOT NULL, source_key TEXT NOT NULL, source TEXT NOT NULL,
              title TEXT NOT NULL, url TEXT NOT NULL, observed_at REAL NOT NULL,
              PRIMARY KEY(goal_id,source_key)
          );
          CREATE TABLE IF NOT EXISTS cognitive_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              goal_id TEXT NOT NULL, kind TEXT NOT NULL,
              note TEXT NOT NULL, source_key TEXT,
              created_at REAL NOT NULL
          );
        """)
        self.con.commit()

    def close(self):
        self.con.close()

    def already_seen(self, source_key: str) -> bool:
        return self.con.execute("SELECT 1 FROM cognitive_sources WHERE source_key=?",
                                (source_key,)).fetchone() is not None

    def pending_sources(self, sources: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        selected = []
        for row in sources:
            if not isinstance(row, dict) or not isinstance(row.get("key"), str):
                continue
            if self.already_seen(row["key"]):
                continue
            selected.append(row)
            if len(selected) >= limit:
                break
        return selected

    def goals(self, *, limit: int = 12, status: str | None = None) -> list[dict[str, Any]]:
        self.con.row_factory = sqlite3.Row
        if status:
            rows = self.con.execute("""SELECT * FROM cognitive_goals WHERE status=?
                                      ORDER BY updated_at DESC LIMIT ?""", (status, limit)).fetchall()
        else:
            rows = self.con.execute("SELECT * FROM cognitive_goals ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(x) for x in rows]

    def sources_for(self, goal_id: str) -> list[dict[str, Any]]:
        self.con.row_factory = sqlite3.Row
        rows = self.con.execute("""SELECT source_key,source,title,url,observed_at
                                   FROM cognitive_evidence WHERE goal_id=? ORDER BY observed_at DESC LIMIT 10""",
                                (goal_id,)).fetchall()
        return [dict(x) for x in rows]

    def events(self, goal_id: str, limit: int = 20) -> list[dict[str, Any]]:
        self.con.row_factory = sqlite3.Row
        rows = self.con.execute("""SELECT kind,note,source_key,created_at FROM cognitive_events
                                   WHERE goal_id=? ORDER BY id DESC LIMIT ?""",(goal_id,limit)).fetchall()
        return [dict(x) for x in rows]

    def eligible(self, now: float, day_start: float, interval_hours: int, daily_limit: int) -> tuple[bool,str]:
        last = self.con.execute("SELECT MAX(attempted_at) FROM cognitive_cycles").fetchone()[0]
        if last and now - float(last) < interval_hours * 3600:
            return False, "interval"
        count = self.con.execute("SELECT COUNT(*) FROM cognitive_cycles WHERE attempted_at>=?",
                                 (day_start,)).fetchone()[0]
        if count >= daily_limit:
            return False, "daily_limit"
        return True, "ok"

    def begin(self, now: float, count: int) -> int:
        with self.con:
            cur = self.con.execute("INSERT INTO cognitive_cycles(attempted_at,status,input_count) VALUES(?,?,?)",
                                   (now,"started",count))
            return int(cur.lastrowid)

    def failed(self, cycle_id: int):
        with self.con:
            self.con.execute("UPDATE cognitive_cycles SET status='failed' WHERE id=? AND status='started'",(cycle_id,))

    def _evidence(self, gid: str, source: dict[str, Any], now: float):
        self.con.execute("""INSERT OR IGNORE INTO cognitive_evidence
           (goal_id,source_key,source,title,url,observed_at) VALUES (?,?,?,?,?,?)""",
           (gid,source["key"],str(source.get("source",""))[:80],
            str(source.get("title",""))[:250],str(source.get("url",""))[:800],now))

    def commit_cycle(self, cycle_id: int, sources: list[dict[str, Any]],
                     new: list[dict[str,str]], updates: list[dict[str,str]], max_open: int,
                     now: float) -> dict[str,int]:
        lookup={x["key"]:x for x in sources}
        created,updated=0,0
        with self.con:
            for s in sources:
                self.con.execute("INSERT OR IGNORE INTO cognitive_sources VALUES (?,?)",(s["key"],now))
            open_count=self.con.execute("SELECT COUNT(*) FROM cognitive_goals WHERE status='open'").fetchone()[0]
            for goal in new:
                source=lookup.get(goal["source_key"])
                if not source or open_count >= max_open:
                    continue
                # 同一来源只能创建一个问题；同一个周期也不会添加重复问题。
                gid="C"+hashlib.sha256(goal["source_key"].encode()).hexdigest()[:22]
                cur=self.con.execute("""INSERT OR IGNORE INTO cognitive_goals
                (goal_id,topic,question,observation,uncertainty,next_action,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?)""",(gid,goal["topic"],goal["question"],goal["observation"],
                    goal["uncertainty"],goal["next_action"],now,now))
                if not cur.rowcount:
                    continue
                self._evidence(gid,source,now)
                self.con.execute("INSERT INTO cognitive_events(goal_id,kind,note,source_key,created_at) VALUES (?,?,?,?,?)",
                                 (gid,"created","根据公开标题简介产生待验证问题",source["key"],now))
                open_count+=1
                created+=1
            for update in updates:
                gid=update["goal_id"]
                source=lookup.get(update["source_key"])
                if not source or not self.con.execute("SELECT 1 FROM cognitive_goals WHERE goal_id=? AND status='open'",(gid,)).fetchone():
                    continue
                # 新证据只能更新存在的问题，且同一来源不能重复计算。
                cur=self.con.execute("""INSERT OR IGNORE INTO cognitive_evidence
                    (goal_id,source_key,source,title,url,observed_at) VALUES(?,?,?,?,?,?)""",
                    (gid,source["key"],str(source.get("source",""))[:80],
                     str(source.get("title",""))[:250],str(source.get("url",""))[:800],now))
                if not cur.rowcount:
                    continue
                self.con.execute("""UPDATE cognitive_goals SET observation=?,uncertainty=?,
                    next_action=?,updated_at=?,revision=revision+1 WHERE goal_id=? AND status='open'""",
                    (update["observation"],update["uncertainty"],update["next_action"],now,gid))
                self.con.execute("INSERT INTO cognitive_events(goal_id,kind,note,source_key,created_at) VALUES(?,?,?,?,?)",
                                 (gid,"new_public_evidence","新的公开素材带来观察，仍未人工核实",source["key"],now))
                updated+=1
            self.con.execute("UPDATE cognitive_cycles SET status='completed',added=?,updated=? WHERE id=?",
                             (created,updated,cycle_id))
        return {"added":created,"updated":updated}

    def decide(self, goal_id: str, decision: str, *, now: float | None = None) -> bool:
        """仅供显式管理员操作；不由模型调用，不执行 next_action。"""
        if decision not in DECISIONS or not re.fullmatch(r"C[0-9a-f]{22}",str(goal_id)):
            return False
        target="open" if decision=="reopen" else decision
        timestamp=time.time() if now is None else now
        with self.con:
            row=self.con.execute("SELECT status FROM cognitive_goals WHERE goal_id=?",(goal_id,)).fetchone()
            if not row:
                return False
            old=row[0]
            if old==target or (old!="open" and target!="open") or (old=="open" and target=="open"):
                return False
            self.con.execute("UPDATE cognitive_goals SET status=?,updated_at=?,revision=revision+1 WHERE goal_id=?",
                             (target,timestamp,goal_id))
            self.con.execute("INSERT INTO cognitive_events(goal_id,kind,note,source_key,created_at) VALUES(?,?,?,?,?)",
                             (goal_id,"review_"+decision,"通过管理员确认更新问题状态",None,timestamp))
        return True

    def summary(self) -> dict[str, Any]:
        counts=dict(self.con.execute("SELECT status,COUNT(*) FROM cognitive_goals GROUP BY status").fetchall())
        latest=self.con.execute("SELECT attempted_at,status FROM cognitive_cycles ORDER BY id DESC LIMIT 1").fetchone()
        return {"goals":counts,"sources_seen":self.con.execute("SELECT COUNT(*) FROM cognitive_sources").fetchone()[0],
                "cycle_count":self.con.execute("SELECT COUNT(*) FROM cognitive_cycles").fetchone()[0],
                "last_cycle":{"timestamp":latest[0],"status":latest[1]} if latest else None}

    def interest_hint(self, *, limit: int = 3) -> str:
        # 只读取公开来源生成的主题；不含个人信息，不生成任何动作。
        topics=[]
        for goal in self.goals(limit=20,status="open"):
            topic=goal["topic"]
            if topic not in topics:
                topics.append(topic)
            if len(topics)>=limit:
                break
        return "、".join(topics)
