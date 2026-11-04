"""Durable trusted tool observations, call budgets, task ownership and research.

Only trusted tool adapters may write observations. Model summaries, credentials,
raw call arguments and hidden reasoning do not belong in this ledger.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
import unicodedata
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .mysql_compat import MySQLCompatConnection
from .sql_logging import connect_sqlite

_CONTROL = "__tool_control__"
_LOCAL = "__tool_local__"
_RANK = {"public": 0, "personal": 1, "unknown": 2, "confidential": 3}
_SECRET = re.compile(r"保密|秘密|不要(?:公开|告诉|转告|透露)|do not share|confidential", re.I)


def _now(value=None):
    value = time.time() if value is None else value
    if type(value) not in (int, float):
        raise ValueError("invalid_time")
    try:
        if not math.isfinite(value) or not 0 <= value <= 1e12:
            raise ValueError("invalid_time")
    except OverflowError:
        raise ValueError("invalid_time") from None
    return float(value)


def _text(value, maximum, *, empty=False, code="invalid_text"):
    if not isinstance(value, str):
        raise ValueError(code)
    value = unicodedata.normalize("NFKC", value).strip()
    if (not value and not empty) or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError(code)
    return value


def _name(value, maximum=80):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,"+str(maximum-1)+r"}", value):
        raise ValueError("invalid_name")
    return value


def _task_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", value):
        raise ValueError("invalid_task_id")
    return value


def _digest(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _integer(value, minimum=0, maximum=1000000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("invalid_limit")
    return value


def _day(value, now):
    value = _now(value)
    if not value <= now < value + 86400:
        raise ValueError("invalid_day")
    return value


def _url(value):
    value = _text(value, 1000, empty=True, code="invalid_url")
    if not value:
        return ""
    try:
        parts = urlsplit(value)
        port = parts.port
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment or "?" in value or "#" in value or "\\" in value
                or (port is not None and port < 1)):
            raise ValueError("invalid_url")
    except ValueError:
        raise ValueError("invalid_url") from None
    return value


def observation_record(row):
    """One canonical mapper shared by direct and unified-memory reads."""
    if not row or int(row["active"]) != 1:
        return None
    return {"id": row["id"], "source": "tool", "kind": "observation",
            "scope": row["scope"], "subject_id": row["subject_id"], "text": row["text"],
            "timestamp": float(row["timestamp"]), "sensitivity": row["sensitivity"],
            "evidence_status": "tool_observation", "origin_key": row["origin_key"],
            "url": row["url"], "source_name": row["provider"]}


class ToolStore:
    def __init__(self, path: Path, *, logger=None, sql_debug=False, mysql_settings=None):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = (MySQLCompatConnection(**mysql_settings, logger=logger, sql_debug=sql_debug)
                    if mysql_settings is not None else
                    connect_sqlite(str(path), timeout=10, logger=logger, sql_debug=sql_debug))
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA busy_timeout=10000")
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.executescript("""
        CREATE TABLE IF NOT EXISTS tool_observations (
          id TEXT PRIMARY KEY, provider TEXT NOT NULL, resource_key TEXT NOT NULL,
          text TEXT NOT NULL, scope TEXT NOT NULL, subject_id TEXT NOT NULL,
          sensitivity TEXT NOT NULL, url TEXT NOT NULL, origin_key TEXT NOT NULL,
          timestamp REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tool_observations_active ON tool_observations(active,timestamp);
        CREATE TABLE IF NOT EXISTS tool_calls (
          id TEXT PRIMARY KEY, provider TEXT NOT NULL, tool TEXT NOT NULL,
          args_digest TEXT NOT NULL, context_digest TEXT NOT NULL, status TEXT NOT NULL,
          scope TEXT NOT NULL, subject_id TEXT NOT NULL, budget_day REAL NOT NULL,
          payload TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tool_calls_day ON tool_calls(budget_day,created);
        CREATE TABLE IF NOT EXISTS tool_tasks (
          id TEXT PRIMARY KEY, provider TEXT NOT NULL, task_id TEXT NOT NULL,
          scope TEXT NOT NULL, subject_id TEXT NOT NULL, operation TEXT NOT NULL,
          created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tool_research (
          question_key TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'idle',
          attempts INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
          last_attempt REAL NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
          last_finish REAL NOT NULL DEFAULT 0, lease_token TEXT NOT NULL DEFAULT '',
          lease_until REAL NOT NULL DEFAULT 0, interval_seconds INTEGER NOT NULL DEFAULT 0,
          budget_day REAL NOT NULL DEFAULT 0, daily_count INTEGER NOT NULL DEFAULT 0,
          updated REAL NOT NULL DEFAULT 0
        );
        """)
        self.con.commit()

    def close(self):
        self.con.close()

    def _lock(self):
        # Lazy initialization keeps a freshly constructed migration target empty.
        # INSERT itself serializes competing creators; UPDATE serializes later writes.
        self.con.execute("INSERT OR IGNORE INTO tool_research(question_key) VALUES (?)", (_CONTROL,))
        self.con.execute("UPDATE tool_research SET updated=updated WHERE question_key=?", (_CONTROL,))

    def _read(self, sql, parameters=()):
        if isinstance(self.con, MySQLCompatConnection):
            return [dict(row) for row in self.con.fetch_readonly(sql, parameters)]
        cursor = self.con.execute(sql, parameters)
        try:
            return [dict(row) for row in cursor.fetchall()]
        finally:
            cursor.close()

    def put_observation(self, provider, resource_key, text, scope, subject_id="",
                        sensitivity="personal", url="", origin_key="", now=None):
        provider = _name(provider, 40)
        resource_key = _text(resource_key, 191, code="invalid_resource")
        text = _text(text, 600)
        scope = _text(scope, 100, code="invalid_scope")
        subject_id = _text(subject_id, 100, empty=True)
        if not isinstance(sensitivity, str) or sensitivity not in _RANK:
            raise ValueError("invalid_sensitivity")
        url = _url(url)
        origin_key = _text(origin_key, 240, empty=True)
        origin_key = "tool:" + provider + ":" + _digest(origin_key or resource_key)
        timestamp = _now(now)
        identity = "tool:observation:" + _digest(provider, resource_key)
        if sensitivity == "public" and not (scope == "public" or scope.startswith("public:")):
            sensitivity = "personal" if scope.startswith(("private:", "group:")) else "unknown"
        if _SECRET.search(text):
            sensitivity = "confidential"
        with self.con:
            self._lock()
            old = self.con.execute("SELECT * FROM tool_observations WHERE id=?", (identity,)).fetchone()
            if old is not None:
                sensitivity = max((old["sensitivity"], sensitivity), key=_RANK.get)
                old_public = old["scope"] == "public" or old["scope"].startswith("public:")
                new_public = scope == "public" or scope.startswith("public:")
                if old["scope"] == "global:tool":
                    scope, subject_id = "global:tool", ""
                    sensitivity = max((sensitivity, "unknown"), key=_RANK.get)
                elif old["scope"] != scope:
                    if new_public and not old_public:
                        scope, subject_id = old["scope"], old["subject_id"]
                    elif not old_public and not new_public:
                        scope, subject_id = "global:tool", ""
                        sensitivity = max((sensitivity, "unknown"), key=_RANK.get)
                    elif old_public and new_public:
                        scope = old["scope"]
                if scope == old["scope"]:
                    if old["subject_id"] and subject_id and old["subject_id"] != subject_id:
                        subject_id = ""
                        sensitivity = max((sensitivity, "unknown"), key=_RANK.get)
                    elif not subject_id:
                        subject_id = old["subject_id"]
                changes = {"text": text, "scope": scope, "subject_id": subject_id,
                           "sensitivity": sensitivity, "url": url, "origin_key": origin_key, "active": 1}
                if all(old[key] == value for key, value in changes.items()):
                    return observation_record(old)
                timestamp = max(timestamp, float(old["timestamp"]) + .000001)
                self.con.execute("UPDATE tool_observations SET text=?,scope=?,subject_id=?,sensitivity=?,"
                                 "url=?,origin_key=?,timestamp=?,active=1 WHERE id=?",
                                 (text, scope, subject_id, sensitivity, url, origin_key, timestamp, identity))
            else:
                self.con.execute("INSERT INTO tool_observations(id,provider,resource_key,text,scope,subject_id,"
                                 "sensitivity,url,origin_key,timestamp,created) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                 (identity, provider, resource_key, text, scope, subject_id, sensitivity,
                                  url, origin_key, timestamp, timestamp))
            return observation_record(self.con.execute("SELECT * FROM tool_observations WHERE id=?", (identity,)).fetchone())

    def retract_observation(self, provider, resource_key, now=None):
        identity = "tool:observation:" + _digest(_name(provider, 40), _text(resource_key, 191, code="invalid_resource"))
        timestamp = _now(now)
        with self.con:
            self._lock()
            old = self.con.execute("SELECT timestamp,active FROM tool_observations WHERE id=?", (identity,)).fetchone()
            if old is None or not old["active"]:
                return False
            self.con.execute("UPDATE tool_observations SET active=0,timestamp=? WHERE id=?",
                             (max(timestamp, float(old["timestamp"])+.000001), identity))
            return True

    def retract_origin(self, provider, origin_key, now=None):
        """Withdraw every current observation of one trusted original resource."""
        provider = _name(provider, 40)
        origin_key = _text(origin_key, 240)
        origin_key = "tool:" + provider + ":" + _digest(origin_key)
        timestamp = _now(now)
        with self.con:
            self._lock()
            result = self.con.execute(
                "UPDATE tool_observations SET active=0,"
                "timestamp=CASE WHEN timestamp>=? THEN timestamp+0.001 ELSE ? END "
                "WHERE provider=? AND origin_key=? AND active=1",
                (timestamp, timestamp, provider, origin_key))
            return int(result.rowcount)

    def observations(self, limit=200, now=None):
        _integer(limit, 1, 1000)
        _now(now)
        return [observation_record(row) for row in self._read(
            "SELECT * FROM tool_observations WHERE active=1 ORDER BY timestamp DESC,id LIMIT ?", (limit,))]

    def resolve(self, ids, now=None):
        _now(now)
        if (not isinstance(ids, (list, tuple)) or len(ids) > 128
                or any(not isinstance(i, str) or not re.fullmatch(r"tool:observation:[0-9a-f]{64}", i) for i in ids)):
            raise ValueError("invalid_ids")
        if not ids:
            return []
        rows = self._read("SELECT * FROM tool_observations WHERE active=1 AND id IN ("+
                          ",".join("?" for _ in ids)+")", tuple(ids))
        lookup = {row["id"]: observation_record(row) for row in rows}
        return [lookup[i] for i in dict.fromkeys(ids) if i in lookup]

    def reserve_call(self, provider, tool, args_digest, request_key, scope, subject_id,
                     day_start, max_daily, now=None):
        provider, tool = _name(provider, 40), _name(tool)
        if not isinstance(args_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", args_digest):
            raise ValueError("invalid_digest")
        request_key = _text(request_key, 512, code="invalid_request_key")
        scope, subject_id = _text(scope, 100), _text(subject_id, 100, empty=True)
        timestamp = _now(now)
        day_start = _day(day_start, timestamp)
        max_daily = _integer(max_daily)
        context_digest = _digest(request_key, scope, subject_id)
        identity = "tool:call:"+_digest(provider, tool, args_digest, context_digest)
        with self.con:
            self._lock()
            old = self.con.execute("SELECT status FROM tool_calls WHERE id=?", (identity,)).fetchone()
            if old is not None:
                return {"id": identity, "status": old["status"], "allowed": False}
            count = self.con.execute("SELECT COUNT(*) FROM tool_calls WHERE budget_day=?", (day_start,)).fetchone()[0]
            latest = self.con.execute("SELECT MAX(budget_day) FROM tool_calls").fetchone()[0]
            if count >= max_daily or (latest is not None and day_start < latest):
                return {"id": "", "status": "budget_exhausted", "allowed": False}
            self.con.execute("INSERT INTO tool_calls(id,provider,tool,args_digest,context_digest,status,scope,"
                             "subject_id,budget_day,payload,created,updated) VALUES (?,?,?,?,?,'reserved',?,?,?,'{}',?,?)",
                             (identity, provider, tool, args_digest, context_digest, scope, subject_id,
                              day_start, timestamp, timestamp))
            return {"id": identity, "status": "reserved", "allowed": True}

    def complete_call(self, id, status="succeeded", result_summary="", now=None):
        if not isinstance(id, str) or not re.fullmatch(r"tool:call:[0-9a-f]{64}", id):
            raise ValueError("invalid_call")
        if not isinstance(status, str) or status not in {"succeeded", "failed", "uncertain"}:
            raise ValueError("invalid_call_status")
        summary = _text(result_summary, 240, empty=True)
        timestamp = _now(now)
        with self.con:
            self._lock()
            old = self.con.execute("SELECT status FROM tool_calls WHERE id=?", (id,)).fetchone()
            if old is None:
                raise ValueError("call_missing")
            if old["status"] != "reserved":
                if old["status"] != status:
                    raise ValueError("call_already_completed")
                return {"id": id, "status": status}
            # Summaries are adapter-generated status text, never model output or arguments.
            payload = json.dumps({"summary": summary}, ensure_ascii=False, separators=(",", ":"))
            self.con.execute("UPDATE tool_calls SET status=?,payload=?,updated=? WHERE id=?",
                             (status, payload, timestamp, id))
            return {"id": id, "status": status}

    def bind_task(self, provider, task_id, scope, subject_id, operation, now=None):
        provider, task_id = _name(provider, 40), _task_id(task_id)
        scope, subject_id = _text(scope, 100), _text(subject_id, 100, empty=True)
        operation, timestamp = _name(operation), _now(now)
        identity = _digest(provider, task_id)
        with self.con:
            self._lock()
            old = self.con.execute("SELECT * FROM tool_tasks WHERE id=?", (identity,)).fetchone()
            if old is not None:
                if (old["scope"], old["subject_id"], old["operation"]) != (scope, subject_id, operation):
                    raise ValueError("task_binding_conflict")
                return None
            self.con.execute("INSERT INTO tool_tasks(id,provider,task_id,scope,subject_id,operation,created,updated) "
                             "VALUES (?,?,?,?,?,?,?,?)", (identity, provider, task_id, scope, subject_id, operation, timestamp, timestamp))

    def task(self, provider, task_id):
        identity = _digest(_name(provider, 40), _task_id(task_id))
        rows = self._read("SELECT provider,task_id,scope,subject_id,operation,created,updated FROM tool_tasks WHERE id=?", (identity,))
        return rows[0] if rows else None

    def claim_research(self, question_keys, *, interval_seconds, daily_limit, day_start, now=None):
        timestamp = _now(now)
        day_start = _day(day_start, timestamp)
        interval_seconds = _integer(interval_seconds, 1, 31536000)
        daily_limit = _integer(daily_limit)
        if not isinstance(question_keys, (list, tuple)) or len(question_keys) > 128:
            raise ValueError("invalid_questions")
        keys = sorted({_text(key, 160, code="invalid_question") for key in question_keys})
        if {_CONTROL, _LOCAL}.intersection(keys):
            raise ValueError("invalid_question")
        if not keys or not daily_limit:
            return None
        with self.con:
            self._lock()
            control = self.con.execute("SELECT * FROM tool_research WHERE question_key=?", (_CONTROL,)).fetchone()
            if (control["budget_day"] > day_start or
                    (control["budget_day"] == day_start and control["daily_count"] >= daily_limit) or
                    (control["attempts"] > 0 and control["last_attempt"] + interval_seconds > timestamp)):
                return None
            for key in keys:
                self.con.execute("INSERT OR IGNORE INTO tool_research(question_key) VALUES (?)", (key,))
            rows = self.con.execute("SELECT * FROM tool_research WHERE question_key IN ("+
                                    ",".join("?" for _ in keys)+") AND next_attempt<=? "
                                    "AND (status<>'running' OR lease_until<=?) "
                                    "ORDER BY last_attempt,attempts,question_key LIMIT 1", (*keys, timestamp, timestamp)).fetchall()
            if not rows:
                return None
            row, token = rows[0], uuid.uuid4().hex
            self.con.execute("UPDATE tool_research SET status='running',attempts=attempts+1,last_attempt=?,"
                             "next_attempt=?,lease_token=?,lease_until=?,interval_seconds=?,updated=? WHERE question_key=?",
                             (timestamp, timestamp+interval_seconds, token, timestamp+max(600, min(interval_seconds, 3600)),
                              interval_seconds, timestamp, row["question_key"]))
            self.con.execute("UPDATE tool_research SET attempts=attempts+1,last_attempt=?,"
                             "daily_count=CASE WHEN budget_day=? THEN daily_count+1 ELSE 1 END,budget_day=?,updated=? WHERE question_key=?",
                             (timestamp, day_start, day_start, timestamp, _CONTROL))
            return {"question_key": row["question_key"], "token": token, "attempted": int(row["attempts"])+1}

    def finish_research(self, key, success, now=None, *, token=None):
        key = _text(key, 160, code="invalid_question")
        if key in {_CONTROL, _LOCAL} or type(success) is not bool:
            raise ValueError("invalid_research_result")
        if token is not None and (not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token)):
            raise ValueError("invalid_token")
        timestamp = _now(now)
        with self.con:
            self._lock()
            row = self.con.execute("SELECT * FROM tool_research WHERE question_key=?", (key,)).fetchone()
            if (row is None or row["status"] != "running" or (token is not None and row["lease_token"] != token)
                    or row["lease_until"] <= timestamp):
                raise ValueError("research_lease_lost")
            failures = 0 if success else min(20, int(row["failures"])+1)
            delay = row["interval_seconds"] if success else min(7*86400, max(60, row["interval_seconds"])*2**min(10, failures-1))
            self.con.execute("UPDATE tool_research SET status=?,failures=?,last_finish=?,next_attempt=?,"
                             "lease_token='',lease_until=0,updated=? WHERE question_key=?",
                             ("succeeded" if success else "failed", failures, timestamp,
                              max(row["next_attempt"], timestamp+delay), timestamp, key))

    def next_local_question(self, question_keys):
        """Rotate local retrieval without consuming network or research budgets."""
        if not isinstance(question_keys, (list, tuple)) or len(question_keys) > 128:
            raise ValueError("invalid_questions")
        keys = sorted({_text(key, 160, code="invalid_question") for key in question_keys})
        if {_CONTROL, _LOCAL}.intersection(keys):
            raise ValueError("invalid_question")
        if not keys:
            return None
        with self.con:
            self._lock()
            row = self.con.execute("SELECT lease_token FROM tool_research WHERE question_key=?", (_LOCAL,)).fetchone()
            previous = row["lease_token"] if row is not None else ""
            chosen = keys[(keys.index(previous)+1) % len(keys)] if previous in keys else keys[0]
            self.con.execute("INSERT OR IGNORE INTO tool_research(question_key) VALUES (?)", (_LOCAL,))
            self.con.execute("UPDATE tool_research SET lease_token=?,attempts=attempts+1,updated=? WHERE question_key=?",
                             (chosen, time.time(), _LOCAL))
            return chosen

    def status(self):
        # Dedicated reads are side-effect free even on a new empty database.
        calls = {row["status"]: int(row["n"]) for row in self._read("SELECT status,COUNT(*) AS n FROM tool_calls GROUP BY status")}
        active = {int(row["active"]): int(row["n"]) for row in self._read("SELECT active,COUNT(*) AS n FROM tool_observations GROUP BY active")}
        daily = self._read("SELECT budget_day,COUNT(*) AS n FROM tool_calls GROUP BY budget_day ORDER BY budget_day DESC LIMIT 1")
        control = self._read("SELECT attempts,last_attempt,budget_day,daily_count FROM tool_research WHERE question_key=?", (_CONTROL,))
        return {"observations": {"active": active.get(1, 0), "inactive": active.get(0, 0)},
                "calls": {name: calls.get(name, 0) for name in ("reserved", "succeeded", "failed", "uncertain")},
                "call_budget": {"day_start": daily[0]["budget_day"] if daily else 0,
                                "count": daily[0]["n"] if daily else 0},
                "tasks": self._read("SELECT COUNT(*) AS n FROM tool_tasks")[0]["n"],
                "research": control[0] if control else {"attempts": 0, "last_attempt": 0, "budget_day": 0, "daily_count": 0}}
