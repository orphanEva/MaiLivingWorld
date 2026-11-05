"""Durable owner communication defaults with evidence thresholds and revocation watermarks."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
import unicodedata
from pathlib import Path

from .continuity_store import ContinuityStore, _hash, _int, _json, _now, _owner, _text
from .habits_model import SITUATIONS, VALUES, parse_habits_command, validate_habits
from .mysql_compat import MySQLCompatConnection
from .sql_logging import connect_sqlite

def _default(owner):
    return {"owner_id": owner, "revision": 0, "budget_day": 0, "cycle_count": 0, "last_cycle": 0,
            "lease_token": "", "lease_until": 0, "lease_id": "", "created": 0, "updated": 0}


class HabitsStore:
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
        CREATE TABLE IF NOT EXISTS habits_control (
          owner_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0,
          budget_day REAL NOT NULL DEFAULT 0, cycle_count INTEGER NOT NULL DEFAULT 0,
          last_cycle REAL NOT NULL DEFAULT 0, lease_token TEXT NOT NULL DEFAULT '',
          lease_until REAL NOT NULL DEFAULT 0, lease_id TEXT NOT NULL DEFAULT '',
          created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS habits_inputs (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, kind TEXT NOT NULL,
          body_digest TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
          lease_token TEXT NOT NULL DEFAULT '', next_attempt REAL NOT NULL DEFAULT 0,
          expires REAL NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_habits_inputs_owner ON habits_inputs(owner_id,status,created);
        CREATE TABLE IF NOT EXISTS habits_preferences (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, situation TEXT NOT NULL,
          dimension TEXT NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL,
          payload TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_habits_preferences_slot ON habits_preferences(owner_id,situation,dimension);
        CREATE TABLE IF NOT EXISTS habits_audit (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, preference_id TEXT NOT NULL,
          input_id TEXT NOT NULL, action TEXT NOT NULL, revision INTEGER NOT NULL,
          payload TEXT NOT NULL, created REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_habits_audit_owner ON habits_audit(owner_id,created);
        """)
        self.con.commit()

    def close(self):
        self.con.close()

    @staticmethod
    def _decode(row):
        result = dict(row)
        if "payload" in result:
            result.update(json.loads(result.pop("payload")))
        return result

    def _read(self, sql, parameters=()):
        if isinstance(self.con, MySQLCompatConnection):
            return [dict(row) for row in self.con.fetch_readonly(sql, parameters)]
        cursor = self.con.execute(sql, parameters)
        try:
            return [dict(row) for row in cursor.fetchall()]
        finally:
            cursor.close()

    def _lock(self, owner, now):
        # Exclusive owner lock precedes all transaction reads, including the first
        # ever writer; no INSERT IGNORE/shared-lock upgrade on MySQL.
        self.con.execute("INSERT INTO habits_control(owner_id,created,updated) VALUES (?,?,?) "
                         "ON CONFLICT(owner_id) DO UPDATE SET revision=revision", (owner, now, now))
        return dict(self.con.execute("SELECT * FROM habits_control WHERE owner_id=?", (owner,)).fetchone())

    def state(self, owner_id, now=None):
        owner = _owner(owner_id)
        _now(now)
        rows = self._read("SELECT * FROM habits_control WHERE owner_id=?", (owner,))
        return rows[0] if rows else _default(owner)

    @staticmethod
    def _receipt(message, owner, *, command=False):
        identity, digest, body = ContinuityStore._receipt(message, owner)
        if (not 3 <= len(body) <= 600 or any(message.get(key) for key in ("is_picture", "is_voice", "is_emoji"))
                or (not command and (message.get("is_command") or body.lstrip().startswith("/")))):
            raise ValueError("trusted_owner_text_required")
        return identity, digest, body

    def _expire(self, owner, now):
        rows = self.con.execute("SELECT id FROM habits_inputs WHERE owner_id=? AND kind='input' "
                                "AND status IN ('pending','running') AND expires<=?", (owner, now)).fetchall()
        for row in rows:
            self.con.execute("UPDATE habits_inputs SET status='expired',lease_token='',payload=?,updated=? WHERE id=?",
                             (_json({"text": ""}), now, row["id"]))

    def enqueue(self, message, *, owner_id, now=None, ttl_seconds=604800):
        owner, now, ttl = _owner(owner_id), _now(now), _int(ttl_seconds, 60, 31*86400)
        receipt, digest, body = self._receipt(message, owner)
        ident = "habits:input:"+receipt
        with self.con:
            self._lock(owner, now)
            other = self.con.execute("SELECT body_digest FROM habits_inputs WHERE id=?", ("habits:command:"+receipt,)).fetchone()
            if other and other["body_digest"] != digest:
                raise ValueError("idempotency_conflict")
            previous = self.con.execute("SELECT body_digest FROM habits_inputs WHERE id=?", (ident,)).fetchone()
            if previous:
                if previous["body_digest"] != digest:
                    raise ValueError("idempotency_conflict")
                return ident
            self._expire(owner, now)
            count = self.con.execute("SELECT COUNT(*) FROM habits_inputs WHERE owner_id=? AND kind='input' "
                                     "AND status IN ('pending','running')", (owner,)).fetchone()[0]
            if count >= 32:
                return None
            self.con.execute("INSERT INTO habits_inputs(id,owner_id,kind,body_digest,status,expires,payload,created,updated) "
                             "VALUES (?,?,'input',?,'pending',?,?,?,?)",
                             (ident, owner, digest, now+ttl, _json({"text": body}), now, now))
            return ident

    def reserve_cycle(self, owner_id, *, day_start, interval_seconds, daily_limit, now=None):
        owner, now, day = _owner(owner_id), _now(now), _now(day_start)
        interval, limit = _int(interval_seconds, 0, 86400), _int(daily_limit, 0, 1000)
        if day > now or now-day > 172800:
            raise ValueError("invalid_day")
        with self.con:
            row = self._lock(owner, now)
            count = row["cycle_count"] if row["budget_day"] == day else 0
            if count >= limit or day < row["budget_day"] or (row["cycle_count"] and now-row["last_cycle"] < interval):
                return False
            self.con.execute("UPDATE habits_control SET budget_day=?,cycle_count=?,last_cycle=? WHERE owner_id=?",
                             (day, count+1, now, owner))
            return True

    def _release(self, owner):
        self.con.execute("UPDATE habits_control SET lease_token='',lease_until=0,lease_id='' WHERE owner_id=?", (owner,))

    def claim(self, owner_id, *, now=None, lease_seconds=120, limit=1):
        owner, now = _owner(owner_id), _now(now)
        seconds = _int(lease_seconds, 1, 600)
        _int(limit, 1, 1)
        with self.con:
            control = self._lock(owner, now)
            if control["lease_token"] and control["lease_until"] > now:
                return None
            self._expire(owner, now)
            self.con.execute("UPDATE habits_inputs SET status='pending',lease_token='' "
                             "WHERE owner_id=? AND kind='input' AND status='running'", (owner,))
            row = self.con.execute("SELECT * FROM habits_inputs WHERE owner_id=? AND kind='input' AND status='pending' "
                                   "AND next_attempt<=? AND expires>? ORDER BY created,id LIMIT 1", (owner, now, now)).fetchone()
            if row is None:
                self._release(owner)
                return None
            token = uuid.uuid4().hex
            self.con.execute("UPDATE habits_control SET lease_token=?,lease_until=?,lease_id=? WHERE owner_id=?",
                             (token, now+seconds, row["id"], owner))
            self.con.execute("UPDATE habits_inputs SET status='running',lease_token=?,updated=? WHERE id=?", (token, now, row["id"]))
            return {"id": row["id"], "token": token, "text": json.loads(row["payload"])["text"],
                    "timestamp": row["created"], "base_revision": control["revision"]}

    def _job(self, owner, ident, token, now):
        control = self._lock(owner, now)
        if (not isinstance(token, str) or not token or control["lease_token"] != token
                or control["lease_id"] != ident or control["lease_until"] <= now):
            raise ValueError("lease_conflict")
        row = self.con.execute("SELECT * FROM habits_inputs WHERE id=? AND owner_id=? AND kind='input' "
                               "AND status='running' AND lease_token=?", (ident, owner, token)).fetchone()
        if row is None or row["expires"] <= now:
            raise ValueError("input_expired")
        row = self._decode(row)
        if hashlib.sha256(row["text"].encode("utf-8")).hexdigest() != row["body_digest"]:
            raise ValueError("input_changed")
        return control, row

    def defer(self, ident, token, *, owner_id, reason, retry_at, now=None):
        return self._retry(ident, token, owner_id=owner_id, reason=reason, retry_at=retry_at, now=now, failed=False)

    def fail(self, ident, token, *, owner_id, reason, now=None):
        return self._retry(ident, token, owner_id=owner_id, reason=reason, retry_at=None, now=now, failed=True)

    def _retry(self, ident, token, *, owner_id, reason, retry_at, now, failed):
        owner, now, reason = _owner(owner_id), _now(now), _text(reason, 80)
        if retry_at is not None:
            retry_at = _now(retry_at)
            if retry_at < now:
                raise ValueError("invalid_retry_time")
        with self.con:
            _, row = self._job(owner, ident, token, now)
            attempts = row["attempts"]+int(failed)
            status = "failed" if attempts >= 3 else "pending"
            when = retry_at if retry_at is not None else now+min(3600, 60*2**attempts)
            self.con.execute("UPDATE habits_inputs SET status=?,attempts=?,lease_token='',next_attempt=?,payload=?,updated=? WHERE id=?",
                             (status, attempts, when, _json({"text": row["text"] if status == "pending" else "", "error_code": reason}), now, ident))
            self._release(owner)


    @staticmethod
    def _blank(owner, situation, dimension, now):
        return {"id": "habits:preference:"+_hash(owner, situation, dimension), "owner_id": owner,
                "situation": situation, "dimension": dimension, "status": "cleared", "revision": 0,
                "value": "", "evidence": "", "evidence_count": 0, "sources": [], "pending": {},
                "watermark": 0, "confirmed_at": 0, "created": now, "updated": now}

    def _preference(self, owner, situation, dimension, now):
        row = self.con.execute("SELECT * FROM habits_preferences WHERE owner_id=? AND situation=? AND dimension=?",
                               (owner, situation, dimension)).fetchone()
        return self._decode(row) if row else self._blank(owner, situation, dimension, now)

    @staticmethod
    def _payload(row):
        return {key: row[key] for key in ("value", "evidence", "evidence_count", "sources", "pending", "watermark", "confirmed_at")}

    def _write(self, before, after, input_id, action, now):
        after = {**after, "revision": before["revision"]+1, "updated": now}
        payload = _json(self._payload(after))
        if before["revision"]:
            self.con.execute("UPDATE habits_preferences SET status=?,revision=?,payload=?,updated=? WHERE id=?",
                             (after["status"], after["revision"], payload, now, after["id"]))
        else:
            self.con.execute("INSERT INTO habits_preferences(id,owner_id,situation,dimension,status,revision,payload,created,updated) "
                             "VALUES (?,?,?,?,?,?,?,?,?)", (after["id"], after["owner_id"], after["situation"], after["dimension"],
                             after["status"], after["revision"], payload, before["created"], now))
        audit_id = "habits:audit:"+_hash(after["owner_id"], input_id, after["id"])
        self.con.execute("INSERT INTO habits_audit(id,owner_id,preference_id,input_id,action,revision,payload,created) VALUES (?,?,?,?,?,?,?,?)",
                         (audit_id, after["owner_id"], after["id"], input_id, action, after["revision"],
                          _json({"before": self._payload(before), "after": self._payload(after),
                                 "before_status": before["status"], "after_status": after["status"]}), now))

    def preferences(self, owner_id, include_inactive=True):
        owner = _owner(owner_id)
        if type(include_inactive) is not bool:
            raise ValueError("invalid_filter")
        sql = "SELECT * FROM habits_preferences WHERE owner_id=?"
        if not include_inactive:
            sql += " AND status='active'"
        return [self._decode(row) for row in self._read(sql+" ORDER BY situation,dimension", (owner,))]

    def command(self, message, *, owner_id, action, situation, dimension, value="", now=None):
        owner, now = _owner(owner_id), _now(now)
        receipt, digest, body = self._receipt(message, owner, command=True)
        expected = {"action": action, "situation": situation, "dimension": dimension, "value": value}
        if parse_habits_command(body) != expected:
            raise ValueError("unverified_command")
        ident, request = "habits:command:"+receipt, _hash(expected)
        with self.con:
            self._lock(owner, now)
            other = self.con.execute("SELECT body_digest FROM habits_inputs WHERE id=?", ("habits:input:"+receipt,)).fetchone()
            if other and other["body_digest"] != digest:
                raise ValueError("idempotency_conflict")
            old = self.con.execute("SELECT * FROM habits_inputs WHERE id=?", (ident,)).fetchone()
            if old:
                if old["body_digest"] != digest or json.loads(old["payload"])["request_hash"] != request:
                    raise ValueError("idempotency_conflict")
                return False
            before = self._preference(owner, situation, dimension, now)
            after = {**before, "status": "active" if action == "set" else "cleared",
                     "value": value if action == "set" else "", "evidence": "explicit" if action == "set" else "",
                     "evidence_count": int(action == "set"), "sources": [], "pending": {},
                     "watermark": now, "confirmed_at": now}
            if action == "set":
                after["sources"] = [{"id": ident, "body_digest": digest, "content_digest": digest,
                                     "quote_digest": digest, "timestamp": now}]
            self._write(before, after, ident, action, now)
            self.con.execute("INSERT INTO habits_inputs(id,owner_id,kind,body_digest,status,expires,payload,created,updated) "
                             "VALUES (?,?,'command',?,'receipt',0,?,?,?)",
                             (ident, owner, digest, _json({"request_hash": request}), now, now))
            self.con.execute("UPDATE habits_inputs SET status='pending',lease_token='' WHERE owner_id=? AND kind='input' AND status='running'", (owner,))
            self._release(owner)
            self.con.execute("UPDATE habits_control SET revision=revision+1,updated=? WHERE owner_id=?", (now, owner))
            return True

    def complete(self, ident, token, proposal, *, owner_id, expected_revision, now=None):
        owner, now, expected = _owner(owner_id), _now(now), _int(expected_revision)
        with self.con:
            control, job = self._job(owner, ident, token, now)
            if control["revision"] != expected:
                raise ValueError("revision_conflict")
            checked = validate_habits(proposal, {"id": ident, "text": job["text"], "timestamp": job["created"]})
            if checked is None:
                raise ValueError("invalid_proposal")
            applied, ignored = 0, 0
            content = re.sub(r"[^\w]", "", unicodedata.normalize("NFKC", job["text"]).casefold())
            for update in checked["updates"]:
                situation, dimension, value, kind = (update[key] for key in ("situation", "dimension", "value", "evidence"))
                before = self._preference(owner, situation, dimension, now)
                if (job["created"] <= before["watermark"]
                        or (kind == "feedback" and before["status"] == "active"
                            and (before["evidence"] == "explicit" or before["value"] == value))):
                    ignored += 1
                    continue
                evidence = {"id": ident, "body_digest": job["body_digest"], "content_digest": _hash(content),
                            "quote_digest": _hash(update["quote"]), "timestamp": job["created"]}
                after = {**before, "pending": {key: list(rows) for key, rows in before["pending"].items()}}
                if kind == "explicit":
                    # An explicit newer long-term request overrides defaults and
                    # establishes an input-time watermark for old queued feedback.
                    after.update(status="active", value=value, evidence="explicit", evidence_count=1,
                                 sources=[evidence], pending={}, watermark=job["created"], confirmed_at=job["created"])
                else:
                    evidence_id = "habits:evidence:"+_hash(owner, situation, dimension, value, before["watermark"], evidence["content_digest"])
                    if self.con.execute("SELECT id FROM habits_inputs WHERE id=?", (evidence_id,)).fetchone():
                        ignored += 1
                        continue
                    pending = after["pending"].setdefault(value, [])
                    if any(row["content_digest"] == evidence["content_digest"] for row in pending):
                        ignored += 1
                        continue
                    self.con.execute("INSERT INTO habits_inputs(id,owner_id,kind,body_digest,status,expires,payload,created,updated) "
                                     "VALUES (?,?,'evidence',?,'receipt',0,'{}',?,?)",
                                     (evidence_id, owner, evidence["content_digest"], now, now))
                    pending.append(evidence)
                    pending.sort(key=lambda row: (row["timestamp"], row["id"]))
                    if len(pending) > 6:
                        pending[:] = [pending[0]]+pending[-5:]
                    if before["status"] != "active":
                        after["status"] = "pending"
                    if len(pending) >= 3 and pending[-1]["timestamp"]-pending[0]["timestamp"] >= 86400:
                        sources = [pending[0]]+pending[-2:]
                        after.update(status="active", value=value, evidence="feedback", evidence_count=len(sources),
                                     sources=sources, pending={}, watermark=job["created"], confirmed_at=job["created"])
                self._write(before, after, ident, kind, now)
                applied += 1
            self.con.execute("UPDATE habits_inputs SET status='processed',lease_token='',payload=?,updated=? WHERE id=?",
                             (_json({"text": "", "applied": applied, "ignored": ignored}), now, ident))
            if applied:
                self.con.execute("UPDATE habits_control SET revision=revision+1,updated=? WHERE owner_id=?", (now, owner))
            self._release(owner)
            return {"applied": applied, "ignored": ignored, "revision": control["revision"]+int(bool(applied))}

    def status(self, owner_id, now=None):
        owner, now = _owner(owner_id), _now(now)
        inputs = {row["status"]: row["n"] for row in self._read(
            "SELECT status,COUNT(*) AS n FROM habits_inputs WHERE owner_id=? AND kind='input' GROUP BY status", (owner,))}
        queued = self._read("SELECT COUNT(*) AS n FROM habits_inputs WHERE owner_id=? AND kind='input' "
                            "AND status IN ('pending','running') AND expires>?", (owner, now))[0]["n"]
        preferences = self.preferences(owner)
        return {**self.state(owner, now=now), "inputs": inputs, "queued_inputs": queued, "queue_full": queued >= 32,
                "input_capacity": 32, "slot_capacity": 15, "active_preferences": sum(row["status"] == "active" for row in preferences),
                "pending_preferences": sum(row["status"] == "pending" for row in preferences)}
