"""Durable owner engagement, short conversation modes and background attention.

Stores message hashes and opaque task references, never conversation text or
model reasoning. The host verifies the current owner binding and candidate
versions before using a lease; each captured message is independently verified.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
from pathlib import Path

from .mysql_compat import MySQLCompatConnection
from .owner import extract_sender, normalize_qq
from .sql_logging import connect_sqlite


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(*values):
    return hashlib.sha256(_json(values).encode("utf-8")).hexdigest()


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


def _int(value, minimum=0, maximum=100000000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("invalid_integer")
    return value


def _owner(value):
    if not isinstance(value, str) or not value or normalize_qq(value) != value:
        raise ValueError("invalid_owner")
    return value


def _defaults(owner):
    return {"owner_id": owner, "revision": 0, "focus": None, "focus_streak": 0,
            "conversation": {"mode": "normal", "until": 0}, "latest_receipt": "",
            "engaged_until": 0, "lease_token": "", "lease_until": 0, "lease_revision": 0,
            "lease_focus": None, "last_finished": 0, "last_outcome": "",
            "budget_day": 0, "cycle_count": 0, "last_started": 0, "created": 0, "updated": 0}


def _identity(focus):
    return (focus["kind"], focus["key"]) if focus else None


class AttentionStore:
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
        CREATE TABLE IF NOT EXISTS attention_control (
          owner_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0,
          payload TEXT NOT NULL, budget_day REAL NOT NULL DEFAULT 0,
          cycle_count INTEGER NOT NULL DEFAULT 0, last_started REAL NOT NULL DEFAULT 0,
          engaged_until REAL NOT NULL DEFAULT 0, lease_token TEXT NOT NULL DEFAULT '',
          lease_until REAL NOT NULL DEFAULT 0, lease_revision INTEGER NOT NULL DEFAULT 0,
          last_finished REAL NOT NULL DEFAULT 0, last_outcome TEXT NOT NULL DEFAULT '',
          created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS attention_receipts (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, body_digest TEXT NOT NULL,
          revision INTEGER NOT NULL, mode_hash TEXT NOT NULL DEFAULT '', created REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_attention_receipts_owner ON attention_receipts(owner_id,created);
        CREATE TABLE IF NOT EXISTS attention_events (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, action TEXT NOT NULL,
          payload TEXT NOT NULL, created REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_attention_events_owner ON attention_events(owner_id,created);
        """)
        self.con.commit()

    def close(self):
        self.con.close()

    @staticmethod
    def _decode(row):
        result = dict(row)
        result.update(json.loads(result.pop("payload")))
        return result

    @staticmethod
    def _payload(row):
        return {key: row[key] for key in ("focus", "focus_streak", "conversation", "latest_receipt", "lease_focus")}

    def _read(self, sql, parameters=()):
        if isinstance(self.con, MySQLCompatConnection):
            return [dict(row) for row in self.con.fetch_readonly(sql, parameters)]
        cursor = self.con.execute(sql, parameters)
        try:
            return [dict(row) for row in cursor.fetchall()]
        finally:
            cursor.close()

    def _lock(self, owner, now):
        # Single upsert takes the InnoDB exclusive lock before any snapshot;
        # INSERT IGNORE followed by a lock upgrade can deadlock new owners.
        self.con.execute("INSERT INTO attention_control(owner_id,payload,created,updated) VALUES (?,?,?,?) "
                         "ON CONFLICT(owner_id) DO UPDATE SET revision=revision",
                         (owner, _json(self._payload(_defaults(owner))), now, now))
        return self._decode(self.con.execute("SELECT * FROM attention_control WHERE owner_id=?", (owner,)).fetchone())

    def _save(self, row, now):
        self.con.execute("UPDATE attention_control SET revision=?,payload=?,budget_day=?,cycle_count=?,last_started=?,"
                         "engaged_until=?,lease_token=?,lease_until=?,lease_revision=?,last_finished=?,last_outcome=?,updated=? "
                         "WHERE owner_id=?", (row["revision"], _json(self._payload(row)), row["budget_day"], row["cycle_count"],
                         row["last_started"], row["engaged_until"], row["lease_token"], row["lease_until"], row["lease_revision"],
                         row["last_finished"], row["last_outcome"], now, row["owner_id"]))

    def state(self, owner_id, now=None):
        owner, now = _owner(owner_id), _now(now)
        rows = self._read("SELECT * FROM attention_control WHERE owner_id=?", (owner,))
        state = self._decode(rows[0]) if rows else _defaults(owner)
        conversation = state["conversation"]
        conversation["active"] = conversation["mode"] != "normal" and conversation["until"] > now
        conversation["effective_mode"] = conversation["mode"] if conversation["active"] else "normal"
        state["lease"] = {"token": state.pop("lease_token"), "until": state.pop("lease_until"),
                          "revision": state.pop("lease_revision"), "focus": state.pop("lease_focus")}
        return state

    def _event(self, owner, action, payload, now):
        self.con.execute("INSERT INTO attention_events(id,owner_id,action,payload,created) VALUES (?,?,?,?,?)",
                         (uuid.uuid4().hex, owner, action, _json(payload), now))
        rows = self.con.execute("SELECT id FROM attention_events WHERE owner_id=? ORDER BY created DESC,id DESC LIMIT ? OFFSET ?",
                                (owner, 1000, 200)).fetchall()
        for row in rows:
            self.con.execute("DELETE FROM attention_events WHERE id=? AND owner_id=?", (row["id"], owner))

    @staticmethod
    def _receipt_identity(message, owner):
        sender = extract_sender(message)
        if (not sender or sender.platform != "qq" or sender.kind != "private" or sender.user_id != owner
                or sender.target_id != owner or not sender.stream_id or not sender.message_id
                or str(message.get("account_id") or "") == owner
                or len(sender.stream_id) > 256 or len(sender.message_id) > 256):
            return None
        return _hash("qq", owner, sender.stream_id, sender.message_id)

    @staticmethod
    def _receipt(message, owner):
        identity = AttentionStore._receipt_identity(message, owner)
        if (identity is None or message.get("is_command")
                or any(message.get(k) for k in ("is_picture", "is_voice", "is_emoji"))):
            return None
        parts = message.get("raw_message")
        if isinstance(parts, list) and any(not isinstance(p, dict) or p.get("type") != "text" for p in parts):
            return None
        body = message.get("processed_plain_text")
        if (not isinstance(body, str) or len(body) > 600 or not body.strip() or body.lstrip().startswith("/")
                or any(ord(c) < 32 and c not in "\n\r\t" for c in body)):
            return None
        return identity, hashlib.sha256(body.encode("utf-8")).hexdigest()

    @staticmethod
    def _activity_receipt(message, owner):
        strict = AttentionStore._receipt(message, owner)
        if strict is not None:
            # Both entry points identify the same plain-text message identically.
            return strict
        identity = AttentionStore._receipt_identity(message, owner)
        if identity is None:
            return None
        flags = [key for key in ("is_command", "is_picture", "is_voice", "is_emoji") if message.get(key)]
        parts = message.get("raw_message")
        kinds = []
        if isinstance(parts, list):
            allowed = {"text", "image", "voice", "record", "video", "file", "face", "emoji", "forward", "reply", "at"}
            for part in parts[:32]:
                kind = part.get("type") if isinstance(part, dict) else None
                kinds.append(kind if isinstance(kind, str) and len(kind) <= 16 and kind in allowed else "other")
            if len(parts) > 32:
                kinds.append("many_parts")
        body = message.get("processed_plain_text")
        if not (isinstance(body, str) and body) and not flags and not kinds:
            return None
        if isinstance(body, str) and len(body) > 100000:
            # Very large messages still interrupt work. Never traverse media data
            # or allocate an unbounded encoded copy merely to recognize activity.
            body_digest = _hash("activity", identity, "oversized", flags, kinds)
        else:
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest() if isinstance(body, str) else ""
            body_digest = _hash("activity", identity, digest, flags, kinds)
        return identity, body_digest

    def capture(self, message, *, owner_id, engagement_seconds=90, now=None):
        owner, now = _owner(owner_id), _now(now)
        seconds = _int(engagement_seconds, 1, 3600)
        receipt = self._receipt(message, owner)
        if receipt is None:
            return False
        return self._capture_receipt(owner, receipt, seconds, now)

    def capture_activity(self, message, *, owner_id, engagement_seconds=90, now=None):
        """Interrupt for trusted owner activity, without accepting it as model text."""
        owner, now = _owner(owner_id), _now(now)
        seconds = _int(engagement_seconds, 1, 3600)
        receipt = self._activity_receipt(message, owner)
        if receipt is None:
            return False
        return self._capture_receipt(owner, receipt, seconds, now)

    def _capture_receipt(self, owner, receipt, seconds, now):
        with self.con:
            row = self._lock(owner, now)
            previous = self.con.execute("SELECT body_digest FROM attention_receipts WHERE id=?", (receipt[0],)).fetchone()
            if previous:
                if previous["body_digest"] != receipt[1]:
                    raise ValueError("idempotency_conflict")
                return False
            row["revision"] += 1
            row["engaged_until"] = max(row["engaged_until"], now+seconds)
            row["latest_receipt"] = receipt[0]
            if row["conversation"]["mode"] == "repair":
                row["conversation"] = {"mode": "normal", "until": 0}
            self.con.execute("INSERT INTO attention_receipts(id,owner_id,body_digest,revision,created) VALUES (?,?,?,?,?)",
                             (receipt[0], owner, receipt[1], row["revision"], now))
            self._save(row, now)
            self._event(owner, "capture", {"receipt": receipt[0], "revision": row["revision"],
                                         "engaged_until": row["engaged_until"]}, now)
            return True

    def set_mode(self, owner_id, *, message, mode, until, expected_revision, now=None):
        owner, now, until = _owner(owner_id), _now(now), _now(until)
        expected_revision = _int(expected_revision)
        if not isinstance(mode, str) or mode not in {"normal", "listen", "advice", "repair", "light"}:
            raise ValueError("invalid_mode")
        if mode == "normal":
            until = 0
        receipt = self._receipt(message, owner)
        if receipt is None:
            return False
        with self.con:
            row = self._lock(owner, now)
            previous = self.con.execute("SELECT * FROM attention_receipts WHERE id=? AND owner_id=?", (receipt[0], owner)).fetchone()
            if previous and previous["body_digest"] != receipt[1]:
                raise ValueError("idempotency_conflict")
            if (previous is None or previous["mode_hash"] or row["latest_receipt"] != receipt[0]
                    or not 0 <= now-previous["created"] <= 300 or row["revision"] != expected_revision):
                return False
            if mode != "normal" and not now < until <= now+86400:
                raise ValueError("invalid_expiry")
            row["conversation"] = {"mode": mode, "until": until}
            row["revision"] += 1
            self.con.execute("UPDATE attention_receipts SET mode_hash=? WHERE id=?", (_hash(mode, until), receipt[0]))
            self._save(row, now)
            self._event(owner, "mode", {"mode": mode, "until": until, "receipt": receipt[0], "revision": row["revision"]}, now)
            return True

    @staticmethod
    def _candidates(values):
        if not isinstance(values, list) or len(values) > 200:
            raise ValueError("invalid_candidates")
        result, identities = [], {}
        for value in values:
            if not isinstance(value, dict) or set(value) != {"kind", "key", "version"}:
                raise ValueError("invalid_candidate")
            kind, key, version = value["kind"], value["key"], value["version"]
            if (not isinstance(kind, str) or kind not in {"reading", "research", "project"}
                    or not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9:_.-]{1,191}", key)
                    or not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{64}", version)):
                raise ValueError("invalid_candidate")
            ident = kind, key
            if ident in identities:
                if identities[ident] != version:
                    raise ValueError("candidate_version_conflict")
                continue
            identities[ident] = version
            result.append(dict(value))
        return result

    @staticmethod
    def _choose(row, candidates):
        identity = _identity(row["focus"])
        for index, candidate in enumerate(candidates):
            if _identity(candidate) == identity:
                rotate = row["focus_streak"] >= 2 or row["last_outcome"] in {"blocked", "failed"}
                return candidates[(index+int(rotate)) % len(candidates)]
        return candidates[0]

    def claim(self, owner_id, *, candidates, day_start, daily_limit, interval_seconds, lease_seconds=180, now=None):
        owner, now, day = _owner(owner_id), _now(now), _now(day_start)
        limit, interval = _int(daily_limit, 0, 1000), _int(interval_seconds, 0, 86400)
        seconds = _int(lease_seconds, 1, 3600)
        candidates = self._candidates(candidates)
        # Accept a civil day spanning a clock change; the host supplies its start.
        if day > now or now-day > 172800:
            raise ValueError("invalid_day")
        if not candidates:
            return None
        with self.con:
            row = self._lock(owner, now)
            if row["engaged_until"] > now or (row["lease_token"] and row["lease_until"] > now):
                return None
            count = row["cycle_count"] if row["budget_day"] == day else 0
            if (count >= limit or day < row["budget_day"]
                    or ((row["cycle_count"] or row["last_finished"])
                        and now-max(row["last_started"], row["last_finished"]) < interval)):
                return None
            if row["lease_token"]:
                self._event(owner, "expired", {"focus": row["lease_focus"], "revision": row["lease_revision"]}, now)
                row["last_outcome"] = "interrupted"
            chosen = self._choose(row, candidates)
            if _identity(chosen) != _identity(row["focus"]):
                row["focus_streak"] = 0
            row["focus"] = dict(chosen)
            row["lease_focus"] = dict(chosen)
            row["lease_token"] = uuid.uuid4().hex
            row["lease_until"] = now+seconds
            row["lease_revision"] = row["revision"]
            row["budget_day"], row["cycle_count"], row["last_started"] = day, count+1, now
            self._save(row, now)
            self._event(owner, "claim", {"focus": chosen, "revision": row["revision"], "lease_until": row["lease_until"]}, now)
            return {"token": row["lease_token"], "focus": dict(chosen), "revision": row["revision"]}

    def valid(self, owner_id, token, *, now=None):
        owner, now = _owner(owner_id), _now(now)
        if not isinstance(token, str) or not token:
            return False
        state = self.state(owner, now)
        lease = state["lease"]
        return bool(lease["token"] == token and lease["until"] > now
                    and lease["revision"] == state["revision"] and state["engaged_until"] <= now)

    def finish(self, owner_id, token, *, outcome, now=None):
        owner, now = _owner(owner_id), _now(now)
        if not isinstance(outcome, str) or outcome not in {"progress", "blocked", "interrupted", "failed"}:
            raise ValueError("invalid_outcome")
        if not isinstance(token, str) or not token:
            return False
        with self.con:
            row = self._lock(owner, now)
            if row["lease_token"] != token:
                return False
            accepted = not (outcome == "progress" and (row["lease_revision"] != row["revision"]
                            or row["lease_until"] <= now or row["engaged_until"] > now))
            if not accepted:
                outcome = "interrupted"
            if outcome == "progress":
                row["focus_streak"] = min(2, row["focus_streak"]+1)
            self._event(owner, "finish", {"focus": row["lease_focus"], "outcome": outcome,
                                        "revision": row["revision"], "claim_revision": row["lease_revision"]}, now)
            row["lease_token"], row["lease_until"], row["lease_revision"], row["lease_focus"] = "", 0, 0, None
            row["last_finished"], row["last_outcome"] = now, outcome
            self._save(row, now)
            return accepted

    def events(self, owner_id, limit=20):
        owner, limit = _owner(owner_id), _int(limit, 1, 200)
        rows = self._read("SELECT id,action,payload,created FROM attention_events WHERE owner_id=? "
                          "ORDER BY created DESC,id DESC LIMIT ?", (owner, limit))
        return [self._decode(row) for row in rows]
