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
_SOCIAL = "__tool_social__:"
_RESEARCH_READ = "__tool_research_read__:"
_SOCIAL_HELP = frozenset({"social_setup_required", "social_auth_failed", "social_permission_required",
                         "social_search_key_required", "social_reply_uncertain"})
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


def _social_digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("invalid_social_identity")
    return value


def _social_article_inputs(owner, candidates):
    from .owner import normalize_qq
    from .public_web_search import _public_url
    if not normalize_qq(owner) or normalize_qq(owner) != owner:
        raise ValueError("invalid_social_owner")
    if not isinstance(candidates, dict) or len(candidates) > 10:
        raise ValueError("invalid_social_batch")
    for url, version in candidates.items():
        if not isinstance(url, str) or len(url) > 1000 or _public_url(url) != url:
            raise ValueError("invalid_social_article")
        _social_digest(version)


def _reserved_research_key(key):
    return key in {_CONTROL, _LOCAL} or key.startswith((_SOCIAL, _RESEARCH_READ))


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


def observation_id(provider, resource_key):
    return "tool:observation:" + _digest(_name(provider, 40), _text(resource_key, 191, code="invalid_resource"))


def observation_record(row):
    """One canonical mapper shared by direct and unified-memory reads."""
    if not row or int(row["active"]) != 1:
        return None
    evidence = "tool_observation"
    if row["provider"] in ("news", "reference"):
        evidence = "publisher_excerpt" if row["provider"] == "news" else "encyclopedia_excerpt"
        if row["text"].startswith("订阅摘要"):
            evidence = "feed_summary"
        elif row["text"].startswith("网页节选"):
            evidence = "web_excerpt"
    if row["provider"] == "chat_media":
        evidence = "visual_observation"
        if row["text"].startswith("B站链接资料"):
            evidence = "source_metadata"
        elif row["text"].startswith("B站字幕节选"):
            evidence = "subtitle_excerpt"
    return {"id": row["id"], "source": "tool", "kind": "observation",
            "scope": row["scope"], "subject_id": row["subject_id"], "text": row["text"],
            "timestamp": float(row["timestamp"]), "sensitivity": row["sensitivity"],
            "evidence_status": evidence,
            "origin_key": row["origin_key"],
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
        if isinstance(self.con, MySQLCompatConnection):
            # INSERT IGNORE takes a shared lock on a duplicate InnoDB key. A
            # subsequent UPDATE lets competing callers deadlock upgrading it.
            # The translated upsert acquires an exclusive lock in one statement,
            # before any consistent read establishes this transaction's snapshot.
            self.con.execute(
                "INSERT INTO tool_research(question_key) VALUES (?) "
                "ON CONFLICT(question_key) DO UPDATE SET updated=updated", (_CONTROL,))
            return
        # SQLite serializes competing writers at the initial INSERT.
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
                        sensitivity="personal", url="", origin_key="", now=None, *, media_message_key=None):
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
        identity = observation_id(provider, resource_key)
        if sensitivity == "public" and not (scope == "public" or scope.startswith("public:")):
            sensitivity = "personal" if scope.startswith(("private:", "group:")) else "unknown"
        if _SECRET.search(text):
            sensitivity = "confidential"
        with self.con:
            self._lock()
            if provider == "chat_media" and media_message_key and self._withdrawn_media(media_message_key):
                return None
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

    def _withdrawn_media(self, message_key):
        marker = "tool:call:" + _digest("media-withdrawal", message_key)
        return self.con.execute("SELECT id FROM tool_calls WHERE id=?", (marker,)).fetchone() is not None

    def retract_chat_media(self, message_key, keys, scope, author, *, in_flight=False, now=None):
        """Fence all attachments of one verified original, including workers on another connection."""
        if (not isinstance(keys, list) or not 1 <= len(keys) <= 9
                or any(not isinstance(k, str) or not re.fullmatch(r"[0-9a-f]{64}", k) for k in [message_key, *keys])):
            raise ValueError("invalid_media_keys")
        ids = [observation_id("chat_media", key) for key in keys]
        marks = ",".join("?" for _ in keys)
        stamp = _now(now)
        with self.con:
            self._lock()
            if self._withdrawn_media(message_key):
                return False
            ticket = self.con.execute("SELECT id FROM tool_calls WHERE provider='chat_media' AND scope=? "
                f"AND subject_id=? AND args_digest IN ({marks}) LIMIT 1", (scope, author, *keys)).fetchone()
            cleared = self.con.execute("UPDATE tool_observations SET text='',active=0,"
                "timestamp=CASE WHEN timestamp>=? THEN timestamp+0.001 ELSE ? END "
                f"WHERE scope=? AND id IN ({marks}) AND active=1", (stamp, stamp, scope, *ids))
            if not (in_flight or ticket or cleared.rowcount):
                return False
            # One marker for a real accepted message. Do not register unrelated QQ notices.
            marker = "tool:call:" + _digest("media-withdrawal", message_key)
            self.con.execute("INSERT INTO tool_calls(id,provider,tool,args_digest,context_digest,status,scope,"
                "subject_id,budget_day,payload,created,updated) VALUES (?,'chat_media','withdrawal',?,'',"
                "'withdrawn',?,?,0,'{}',?,?)", (marker, message_key, scope, author, stamp, stamp))
            return True

    def retract_observation(self, provider, resource_key, now=None):
        identity = observation_id(provider, resource_key)
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

    def recent_chat_media(self, scope, kinds, *, now=None):
        """Return up to two recent candidates, including failed reads so an older one cannot impersonate them."""
        if not kinds or any(k not in ("image", "video", "bilibili") for k in kinds):
            return []
        stamp = _now(now)
        placeholders = ",".join("?" for _ in kinds)
        return self._read("SELECT args_digest,status FROM tool_calls WHERE provider=? AND scope=? "
                          f"AND tool IN ({placeholders}) AND created>=? AND created<=? ORDER BY created DESC,id DESC LIMIT 2",
                          ("chat_media", scope, *kinds, stamp - 900, stamp))

    def _mark_media_skipped(self, slot, kind, scope, key, day, stamp):
        """Called under the reservation lock. Keep one unread marker per audience/kind."""
        previous = self.con.execute("SELECT args_digest,created FROM tool_calls WHERE id=?", (slot,)).fetchone()
        if previous is None:
            self.con.execute("INSERT INTO tool_calls(id,provider,tool,args_digest,context_digest,status,scope,"
                             "subject_id,budget_day,payload,created,updated) VALUES (?,'chat_media',?,?,'','skipped',"
                             "?,'',?,'{}',?,?)", (slot, kind, key, scope, day, stamp, stamp))
            return
        previous_time = float(previous["created"])
        if stamp < previous_time - 900:
            return
        if abs(stamp - previous_time) <= 900 and previous["args_digest"] != key:
            # Several omissions cannot all be resolved by later reading one of them.
            key = _digest("multiple-skipped-media", scope, kind)
        newest = max(previous_time, stamp)
        self.con.execute("UPDATE tool_calls SET args_digest=?,created=?,updated=? WHERE id=?",
                         (key, newest, newest, slot))

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
                     day_start, max_daily, now=None, *, media_message_key=None):
        provider, tool = _name(provider, 40), _name(tool)
        if not isinstance(args_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", args_digest):
            raise ValueError("invalid_digest")
        request_key = _text(request_key, 512, code="invalid_request_key")
        scope, subject_id = _text(scope, 100), _text(subject_id, 100, empty=True)
        timestamp = _now(now)
        day_start = _day(day_start, timestamp)
        max_daily = _integer(max_daily)
        social_reply = provider == "mastodon" and tool == "mastodon_reply"
        if social_reply and (scope != "public:mastodon" or request_key != "one-reply-per-status"):
            return {"id": "", "allowed": False, "status": "social_reservation_required"}
        context_digest = _digest(request_key, scope, subject_id)
        identity = "tool:call:"+_digest(provider, tool, args_digest, context_digest)
        with self.con:
            self._lock()
            if provider == "chat_media" and media_message_key and self._withdrawn_media(media_message_key):
                return {"id": "", "status": "withdrawn", "allowed": False}
            old = self.con.execute("SELECT status FROM tool_calls WHERE id=?", (identity,)).fetchone()
            if old is not None:
                return {"id": identity, "status": old["status"], "allowed": False}
            if social_reply:
                used = self.con.execute("SELECT COUNT(*) FROM tool_calls WHERE provider=? AND tool=? "
                                        "AND subject_id=? AND budget_day=?", (provider, tool, subject_id, day_start)).fetchone()[0]
                if used >= 2:
                    return {"id": "", "status": "social_daily_limit", "allowed": False}
            # Chat attachments have an independent intake limit. Disabling MCP or exhausting its
            # quota must not enable, disable or consume the conversation's visual allowance.
            media_slot = "tool:call:" + _digest("skipped-media", scope, tool) if provider == "chat_media" else None
            pool = "provider=? AND status NOT IN ('skipped','withdrawn')" if media_slot else "provider<>?"
            count = self.con.execute("SELECT COUNT(*) FROM tool_calls WHERE budget_day=? AND " + pool,
                                     (day_start, "chat_media")).fetchone()[0]
            latest = self.con.execute("SELECT MAX(budget_day) FROM tool_calls WHERE " + pool, ("chat_media",)).fetchone()[0]
            if count >= max_daily or (latest is not None and day_start < latest):
                if media_slot:
                    self._mark_media_skipped(media_slot, tool, scope, args_digest, day_start, timestamp)
                return {"id": "", "status": "budget_exhausted", "allowed": False}
            if media_slot:
                # An exact retry now has a normal receipt. A marker for other omissions stays.
                self.con.execute("DELETE FROM tool_calls WHERE id=? AND args_digest=? AND status='skipped'",
                                 (media_slot, args_digest))
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
        if any(_reserved_research_key(key) for key in keys):
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
        if _reserved_research_key(key) or type(success) is not bool:
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
        if any(_reserved_research_key(key) for key in keys):
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

    def new_research_candidates(self, owner, question_digest, candidates, now=None):
        """Skip recently enqueued versions for this exact question, preserving alternative sources."""
        from .growth_store import source_fingerprint
        from .unified_expression import _public_source_url
        _social_article_inputs(owner, candidates)
        _social_digest(question_digest)
        if len(candidates) > 3:
            raise ValueError("invalid_research_candidates")
        if not candidates:
            return set()
        stamp = _now(now)
        prefix = _RESEARCH_READ + _digest(owner, question_digest) + ":"
        slots = {prefix + _digest(url): url for url in candidates}
        marks = ",".join("?" for _ in slots)
        rows = self._read("SELECT question_key,status,lease_token,updated FROM tool_research "
                          "WHERE question_key IN (" + marks + ") AND updated>? AND updated<=?",
                          (*slots, stamp-86400, stamp))
        records = {r["id"]: r for r in self.resolve([row["lease_token"] for row in rows])}
        blocked = set()
        for row in rows:
            url = slots[row["question_key"]]
            record = records.get(row["lease_token"])
            if (record and _public_source_url(record) == url
                    and row["status"] == _digest(candidates[url], source_fingerprint(record))):
                blocked.add(url)
        return set(candidates) - blocked

    def mark_research_read(self, owner, question_digest, url, version, record, *, call_id, now=None):
        """Called after durable growth enqueue; retain at most 256 source-version markers."""
        from .growth_store import source_fingerprint
        from .social_discovery import browse_receipt
        from .unified_expression import _public_source_url
        _social_article_inputs(owner, {url: version})
        _social_digest(question_digest)
        if not isinstance(record, dict) or _public_source_url(record) != url:
            return False
        if not isinstance(call_id, str) or not re.fullmatch(r"tool:call:[0-9a-f]{64}", call_id):
            raise ValueError("invalid_call")
        stamp = _now(now)
        calls = self._read("SELECT payload FROM tool_calls WHERE id=? AND status='succeeded' "
                           "AND scope=? AND subject_id=? AND updated BETWEEN ? AND ? "
                           "AND ((provider='news' AND tool IN ('news_read','web_read')) "
                           "OR (provider='reference' AND tool='reference_read'))",
                           (call_id, "private:"+owner, owner, stamp-60, stamp))
        if not calls:
            return False
        try:
            payload = json.loads(calls[0]["payload"])
        except (ValueError, TypeError):
            return False
        receipt = browse_receipt(record)
        if not receipt or not isinstance(payload, dict) or payload.get("summary") != receipt:
            return False
        current = self.resolve([record["id"]])
        if not current or source_fingerprint(current[0]) != source_fingerprint(record):
            return False
        slot = _RESEARCH_READ + _digest(owner, question_digest) + ":" + _digest(url)
        version = _digest(version, source_fingerprint(record))
        with self.con:
            self._lock()
            self.con.execute("INSERT OR IGNORE INTO tool_research(question_key) VALUES (?)", (slot,))
            self.con.execute("UPDATE tool_research SET status=?,lease_token=?,updated=? "
                             "WHERE question_key=? AND updated<=?", (version, record["id"], stamp, slot, stamp))
            oldest = self.con.execute("SELECT question_key FROM tool_research WHERE question_key>=? AND question_key<? "
                                      "ORDER BY updated DESC,question_key DESC LIMIT 1 OFFSET 256",
                                      (_RESEARCH_READ, _RESEARCH_READ + "~")).fetchone()
            if oldest:
                self.con.execute("DELETE FROM tool_research WHERE question_key=?", (oldest["question_key"],))
        return True

    def status(self):
        # Dedicated reads are side-effect free even on a new empty database.
        calls = {row["status"]: int(row["n"]) for row in self._read("SELECT status,COUNT(*) AS n FROM tool_calls WHERE provider<>? GROUP BY status", ("chat_media",))}
        active = {int(row["active"]): int(row["n"]) for row in self._read("SELECT active,COUNT(*) AS n FROM tool_observations GROUP BY active")}
        daily = self._read("SELECT budget_day,COUNT(*) AS n FROM tool_calls WHERE provider<>? GROUP BY budget_day ORDER BY budget_day DESC LIMIT 1", ("chat_media",))
        control = self._read("SELECT attempts,last_attempt,budget_day,daily_count FROM tool_research WHERE question_key=?", (_CONTROL,))
        return {"observations": {"active": active.get(1, 0), "inactive": active.get(0, 0)},
                "calls": {name: calls.get(name, 0) for name in ("reserved", "succeeded", "failed", "uncertain")},
                "call_budget": {"day_start": daily[0]["budget_day"] if daily else 0,
                                "count": daily[0]["n"] if daily else 0},
                "tasks": self._read("SELECT COUNT(*) AS n FROM tool_tasks")[0]["n"],
                "research": control[0] if control else {"attempts": 0, "last_attempt": 0, "budget_day": 0, "daily_count": 0}}

    def claim_social_window(self, key, day_start, now=None):
        """Return a durable round number, or 0 when another process owns this window."""
        slot = _SOCIAL + "window:" + _social_digest(key)
        stamp = _now(now)
        day = _day(day_start, stamp)
        with self.con:
            self._lock()
            self.con.execute("INSERT OR IGNORE INTO tool_research(question_key) VALUES (?)", (slot,))
            row = self.con.execute("SELECT attempts,next_attempt,budget_day,daily_count FROM tool_research "
                                   "WHERE question_key=?", (slot,)).fetchone()
            used = int(row["daily_count"]) if row["budget_day"] == day else 0
            if row["budget_day"] > day or row["next_attempt"] > stamp or used >= 4:
                return 0
            turn = int(row["attempts"]) + 1
            self.con.execute("UPDATE tool_research SET attempts=?,last_attempt=?,next_attempt=?,"
                             "budget_day=?,daily_count=?,updated=? WHERE question_key=?",
                             (turn, stamp, stamp + 3*3600, day, used + 1, stamp, slot))
            return turn

    def social_help(self, key, code, now=None, *, clear_code=None):
        """Set a fixed owner-help reason, or clear all/a resolved reason without resetting its cooldown."""
        prefix = _SOCIAL + "help:" + _social_digest(key) + ":"
        stamp = _now(now)
        if (code not in (None, "") and (not isinstance(code, str) or code not in _SOCIAL_HELP)
                or clear_code is not None and (not isinstance(clear_code, str) or clear_code not in _SOCIAL_HELP or code)):
            raise ValueError("invalid_social_help")
        with self.con:
            self._lock()
            if code:
                slot = prefix + code
                self.con.execute("INSERT OR IGNORE INTO tool_research(question_key) VALUES (?)", (slot,))
                self.con.execute("UPDATE tool_research SET status='pending',updated=? WHERE question_key=? "
                                 "AND status<>'pending' AND updated<=?", (stamp, slot, stamp))
            else:
                for resolved in (clear_code,) if clear_code is not None else _SOCIAL_HELP:
                    self.con.execute("UPDATE tool_research SET status='idle',updated=? WHERE question_key=? "
                                     "AND status='pending' AND updated<=?", (stamp, prefix + resolved, stamp))

    def take_social_help(self, key, now=None, *, consume=True):
        """Read the oldest eligible owner-help reason. Only consumption starts its 24h cooldown."""
        prefix = _SOCIAL + "help:" + _social_digest(key) + ":"
        stamp = _now(now)
        if type(consume) is not bool:
            raise ValueError("invalid_social_help")
        slots = [prefix + code for code in _SOCIAL_HELP]
        marks = ",".join("?" for _ in slots)
        sql = ("SELECT question_key FROM tool_research WHERE question_key IN (" + marks + ") "
               "AND status='pending' AND next_attempt<=? AND updated<=? ORDER BY updated,question_key LIMIT 1")
        if not consume:
            rows = self._read(sql, (*slots, stamp, stamp))
            return rows[0]["question_key"][len(prefix):] if rows else ""
        with self.con:
            self._lock()
            row = self.con.execute(sql, (*slots, stamp, stamp)).fetchone()
            if row is None:
                return ""
            slot = row["question_key"]
            self.con.execute("UPDATE tool_research SET next_attempt=?,last_attempt=?,attempts=attempts+1 "
                             "WHERE question_key=?", (stamp + 86400, stamp, slot))
            return slot[len(prefix):]

    def reserve_social_reply(self, account_key, target_key, day_start, max_daily, now=None):
        """Reserve one lifetime reply per verified account/status, inside both daily budgets."""
        return self.reserve_call("mastodon", "mastodon_reply", _social_digest(target_key), "one-reply-per-status",
                                 "public:mastodon", _social_digest(account_key), day_start, max_daily, now=now)

    def recent_social_reads(self, owner, now=None, *, limit=2):
        """Return bounded self-selected public excerpts still at the exact version read."""
        from .owner import normalize_qq
        from .social_discovery import RECEIPT_PREFIX, browse_receipt, parse_receipt
        if not normalize_qq(owner) or normalize_qq(owner) != owner:
            return []
        _integer(limit, 1, 40)
        stamp = _now(now)
        # The budget_day predicate uses the existing daily call index. Only bounded
        # autonomous browsing/research carries this marker; ordinary lookups do not.
        rows = self._read("SELECT payload,updated FROM tool_calls WHERE budget_day>=? AND updated BETWEEN ? AND ? "
                          "AND ((provider='news' AND tool IN ('news_read','web_read')) "
                          "OR (provider='reference' AND tool='reference_read')) AND status='succeeded' "
                          "AND scope=? AND subject_id=? AND payload LIKE ? ORDER BY updated DESC,id DESC LIMIT 40",
                          (stamp - 8*86400, stamp - 7*86400, stamp, "private:"+owner, owner,
                           '{"summary":"' + RECEIPT_PREFIX + '%'))
        receipts = []
        for row in rows:
            try:
                data = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            summary = data.get("summary") if isinstance(data, dict) else None
            ref = parse_receipt(summary)
            if ref:
                receipts.append((ref[0], summary, float(row["updated"])))
        records = {r["id"]: r for r in self.resolve([entry[0] for entry in receipts])}
        selected, origins = [], set()
        for ident, receipt, finished in receipts:
            record = records.get(ident)
            if record and record["origin_key"] not in origins and browse_receipt(record) == receipt:
                selected.append({**record, "browse_time": finished})
                origins.add(record["origin_key"])
                del records[ident]
                if len(selected) == limit:
                    break
        return selected

    def new_social_articles(self, owner, candidates, now=None):
        """Keep changed or unseen listings, with a 24-hour ceiling on read-body suppression."""
        from .social_discovery import browse_receipt
        _social_article_inputs(owner, candidates)
        if not candidates:
            return set()
        stamp = _now(now)
        eligible = {r["url"]: r for r in self.recent_social_reads(owner, now=stamp, limit=40)
                    if r["url"] in candidates and 0 <= stamp-r["browse_time"] < 86400
                    and r["evidence_status"] in {"publisher_excerpt", "web_excerpt"}}
        if not eligible:
            return set(candidates)
        slots = {_SOCIAL + "article:" + _digest(owner, url): url for url in eligible}
        marks = ",".join("?" for _ in slots)
        rows = self._read("SELECT question_key,lease_token,updated FROM tool_research WHERE question_key IN (" +
                          marks + ")", tuple(slots))
        blocked = set()
        for row in rows:
            url = slots[row["question_key"]]
            if (0 <= stamp-float(row["updated"]) < 86400 and
                    row["lease_token"] == _digest(candidates[url], browse_receipt(eligible[url]))):
                blocked.add(url)
        return set(candidates) - blocked

    def mark_social_article_read(self, owner, url, version, *, call_id, now=None):
        """Remember the listing fingerprint only after a freshly completed autonomous body read."""
        from .social_discovery import browse_receipt, parse_receipt
        _social_article_inputs(owner, {url: version})
        if not isinstance(call_id, str) or not re.fullmatch(r"tool:call:[0-9a-f]{64}", call_id):
            raise ValueError("invalid_call")
        stamp = _now(now)
        rows = self._read("SELECT payload FROM tool_calls WHERE id=? AND provider='news' "
                          "AND tool IN ('news_read','web_read') AND status='succeeded' "
                          "AND scope=? AND subject_id=? AND updated BETWEEN ? AND ?",
                          (call_id, "private:"+owner, owner, stamp-60, stamp))
        if not rows:
            return False
        try:
            data = json.loads(rows[0]["payload"])
        except (TypeError, ValueError):
            return False
        summary = data.get("summary") if isinstance(data, dict) else None
        ref = parse_receipt(summary)
        records = self.resolve([ref[0]]) if ref else []
        record = records[0] if records else None
        if (not record or record["url"] != url or browse_receipt(record) != summary
                or record["evidence_status"] not in {"publisher_excerpt", "web_excerpt"}):
            return False
        slot = _SOCIAL + "article:" + _digest(owner, url)
        token = _digest(version, summary)
        with self.con:
            self._lock()
            self.con.execute("INSERT OR IGNORE INTO tool_research(question_key) VALUES (?)", (slot,))
            self.con.execute("UPDATE tool_research SET lease_token=?,updated=? WHERE question_key=? AND updated<=?",
                             (token, stamp, slot, stamp))
        return True

    def new_social_posts(self, account_key, versions):
        """Filter at most ten target/version fingerprints with two indexed batch reads."""
        account = _social_digest(account_key)
        if not isinstance(versions, dict) or len(versions) > 10:
            raise ValueError("invalid_social_batch")
        versions = {_social_digest(target): _social_digest(version) for target, version in versions.items()}
        if not versions:
            return set()
        context = _digest("one-reply-per-status", "public:mastodon", account)
        calls = {"tool:call:" + _digest("mastodon", "mastodon_reply", target, context): target for target in versions}
        slots = {_SOCIAL + "read:" + _digest(account, target): target for target in versions}
        placeholders = ",".join("?" for _ in versions)
        attempted = self._read("SELECT id FROM tool_calls WHERE id IN (" + placeholders + ")", tuple(calls))
        read = self._read("SELECT question_key,lease_token FROM tool_research WHERE question_key IN (" +
                         placeholders + ")", tuple(slots))
        blocked = {calls[row["id"]] for row in attempted}
        blocked.update(slots[row["question_key"]] for row in read
                       if row["lease_token"] == versions[slots[row["question_key"]]])
        return set(versions) - blocked

    def mark_social_post_read(self, account_key, target_key, version, now=None):
        """Retain only the latest read-content fingerprint, never a public conversation transcript."""
        account, target, version = (_social_digest(value) for value in (account_key, target_key, version))
        slot = _SOCIAL + "read:" + _digest(account, target)
        stamp = _now(now)
        with self.con:
            self._lock()
            self.con.execute("INSERT OR IGNORE INTO tool_research(question_key) VALUES (?)", (slot,))
            self.con.execute("UPDATE tool_research SET lease_token=?,updated=? WHERE question_key=? AND updated<=?",
                             (version, stamp, slot, stamp))
