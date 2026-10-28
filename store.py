"""持久化、去重与主动联系配额。所有发送尝试均先落盘。"""
from __future__ import annotations
import json
import sqlite3
import time
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(path), timeout=10)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA busy_timeout=5000")
        self.con.executescript("""
        CREATE TABLE IF NOT EXISTS stories (
            key TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            summary TEXT NOT NULL,
            published_at REAL,
            first_seen REAL NOT NULL,
            memory_saved INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target TEXT NOT NULL,
            created_at REAL NOT NULL,
            story_key TEXT,
            kind TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_attempt_target_time ON attempts(target, created_at);
        CREATE TABLE IF NOT EXISTS story_attempts (
            target TEXT NOT NULL,
            story_key TEXT NOT NULL,
            tried_at REAL NOT NULL,
            PRIMARY KEY(target, story_key)
        );
        """)
        self.con.commit()

    def close(self):
        self.con.close()

    def save_stories(self, stories: list[Any], max_rows: int = 700) -> int:
        now = time.time()
        added = 0
        with self.con:
            for story in stories:
                cur = self.con.execute("""INSERT OR IGNORE INTO stories
                    (key,source,title,url,summary,published_at,first_seen) VALUES(?,?,?,?,?,?,?)""",
                    (story.key, story.source, story.title, story.url, story.summary, story.published_at, now))
                added += cur.rowcount
            self.con.execute("""DELETE FROM stories WHERE key IN
                  (SELECT key FROM stories ORDER BY first_seen DESC LIMIT -1 OFFSET ?)""", (max_rows,))
        return added

    def candidates(self, target: str, now: float, max_age_hours: float, limit: int = 12) -> list[dict[str, Any]]:
        cutoff = now - max_age_hours * 3600
        self.con.row_factory = sqlite3.Row
        rows = self.con.execute("""SELECT s.* FROM stories s
          LEFT JOIN story_attempts a ON s.key=a.story_key AND a.target=?
          WHERE a.story_key IS NULL AND s.first_seen>=?
          AND (s.published_at IS NULL OR (s.published_at>=? AND s.published_at<=?))
          ORDER BY s.first_seen DESC LIMIT ?""",
          (target, cutoff, cutoff, now+21600, limit)).fetchall()
        return [dict(x) for x in rows]

    def may_contact(self, target: str, now: float, day_start: float, min_gap_seconds: float, max_daily: int) -> tuple[bool,str]:
        row=self.con.execute("SELECT MAX(created_at), COUNT(CASE WHEN created_at>=? THEN 1 END) FROM attempts WHERE target=?",(day_start,target)).fetchone()
        last,count = row
        if count >= max_daily:
            return False, "daily_limit"
        if last and now-float(last)<min_gap_seconds:
            return False, "cooldown"
        return True,"ok"

    def record_attempt(self, target: str, now: float, kind: str, story_key: str | None) -> None:
        with self.con:
            self.con.execute("INSERT INTO attempts(target,created_at,story_key,kind) VALUES(?,?,?,?)",(target,now,story_key,kind))
            if story_key:
                self.con.execute("INSERT OR IGNORE INTO story_attempts(target,story_key,tried_at) VALUES(?,?,?)",(target,story_key,now))

    def mark_memory_saved(self,key: str) -> None:
        with self.con:
            self.con.execute("UPDATE stories SET memory_saved=1 WHERE key=?",(key,))

    def statistics(self) -> dict[str,int]:
        return {
            "stories": self.con.execute("SELECT COUNT(*) FROM stories").fetchone()[0],
            "wake_attempts": self.con.execute("SELECT COUNT(*) FROM attempts").fetchone()[0],
            "memory_saved": self.con.execute("SELECT COUNT(*) FROM stories WHERE memory_saved=1").fetchone()[0],
        }

    def recent(self, limit: int = 15) -> list[dict[str,Any]]:
        self.con.row_factory=sqlite3.Row
        return [dict(x) for x in self.con.execute("SELECT key,source,title,url,first_seen,memory_saved FROM stories ORDER BY first_seen DESC LIMIT ?",(limit,))]
