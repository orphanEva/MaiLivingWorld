"""Version-pinned owner corrections and verified-send receipts.

Corrections never mutate their original ledgers. The caller resolves original
records before each use and applies only matching versions. Processed input text
is cleared; outgoing message text is never accepted or stored here.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from pathlib import Path

from .continuity_store import ContinuityStore, _hash, _int, _json, _now, _owner, _text
from .growth_store import source_fingerprint
from .mysql_compat import MySQLCompatConnection
from .sql_logging import connect_sqlite
from .unified_memory import _sensitivity

_RANK = {"public": 0, "personal": 1, "unknown": 2, "confidential": 3}


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("invalid_digest")
    return value


def _quote(value, limit):
    _text(value, limit)
    return value


def _default(owner):
    return {"owner_id": owner, "revision": 0, "budget_day": 0, "cycle_count": 0, "last_cycle": 0,
            "lease_token": "", "lease_until": 0, "lease_id": "", "created": 0, "updated": 0}


class RecollectionStore:
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
        CREATE TABLE IF NOT EXISTS recollection_control (
          owner_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0,
          budget_day REAL NOT NULL DEFAULT 0, cycle_count INTEGER NOT NULL DEFAULT 0,
          last_cycle REAL NOT NULL DEFAULT 0, lease_token TEXT NOT NULL DEFAULT '',
          lease_until REAL NOT NULL DEFAULT 0, lease_id TEXT NOT NULL DEFAULT '',
          created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS recollection_inputs (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, kind TEXT NOT NULL,
          body_digest TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
          lease_token TEXT NOT NULL DEFAULT '', next_attempt REAL NOT NULL DEFAULT 0,
          expires REAL NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_recollection_inputs_owner ON recollection_inputs(owner_id,status,created);
        CREATE TABLE IF NOT EXISTS recollection_revisions (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, target_id TEXT NOT NULL,
          target_digest TEXT NOT NULL, input_id TEXT NOT NULL, relation TEXT NOT NULL,
          status TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
          created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_recollection_revisions_owner ON recollection_revisions(owner_id,status,created);
        CREATE INDEX IF NOT EXISTS idx_recollection_revisions_target ON recollection_revisions(owner_id,target_id);
        CREATE TABLE IF NOT EXISTS recollection_deliveries (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, scope TEXT NOT NULL,
          text_digest TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_recollection_deliveries_owner ON recollection_deliveries(owner_id,created);
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
        self.con.execute("INSERT INTO recollection_control(owner_id,created,updated) VALUES (?,?,?) "
                         "ON CONFLICT(owner_id) DO UPDATE SET revision=revision", (owner, now, now))
        return dict(self.con.execute("SELECT * FROM recollection_control WHERE owner_id=?", (owner,)).fetchone())

    def state(self, owner_id, now=None):
        owner = _owner(owner_id)
        _now(now)
        rows = self._read("SELECT * FROM recollection_control WHERE owner_id=?", (owner,))
        return rows[0] if rows else _default(owner)

    @staticmethod
    def _receipt(message, owner, *, command=False):
        identity, digest, body = ContinuityStore._receipt(message, owner)
        if (not 3 <= len(body) <= 600 or any(message.get(key) for key in ("is_picture", "is_voice", "is_emoji"))
                or (not command and (message.get("is_command") or body.lstrip().startswith("/")))):
            raise ValueError("trusted_owner_text_required")
        return identity, digest, body

    def _expire(self, owner, now):
        rows = self.con.execute("SELECT id FROM recollection_inputs WHERE owner_id=? AND kind='input' "
                                "AND status IN ('pending','running') AND expires<=?", (owner, now)).fetchall()
        for row in rows:
            self.con.execute("UPDATE recollection_inputs SET status='expired',lease_token='',payload=?,updated=? WHERE id=?",
                             (_json({"text": ""}), now, row["id"]))

    def enqueue(self, message, *, owner_id, now=None, ttl_seconds=604800):
        owner, now, ttl = _owner(owner_id), _now(now), _int(ttl_seconds, 60, 31*86400)
        receipt, digest, body = self._receipt(message, owner)
        ident = "recollection:input:"+receipt
        with self.con:
            self._lock(owner, now)
            previous = self.con.execute("SELECT body_digest FROM recollection_inputs WHERE id=?", (ident,)).fetchone()
            if previous:
                if previous["body_digest"] != digest:
                    raise ValueError("idempotency_conflict")
                return ident
            self._expire(owner, now)
            count = self.con.execute("SELECT COUNT(*) FROM recollection_inputs WHERE owner_id=? AND kind='input' "
                                     "AND status IN ('pending','running')", (owner,)).fetchone()[0]
            if count >= 32:
                return None
            self.con.execute("INSERT INTO recollection_inputs(id,owner_id,kind,body_digest,status,expires,payload,created,updated) "
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
            self.con.execute("UPDATE recollection_control SET budget_day=?,cycle_count=?,last_cycle=? WHERE owner_id=?",
                             (day, count+1, now, owner))
            return True

    def _release(self, owner):
        self.con.execute("UPDATE recollection_control SET lease_token='',lease_until=0,lease_id='' WHERE owner_id=?", (owner,))

    def claim(self, owner_id, *, now=None, lease_seconds=120, limit=1):
        owner, now = _owner(owner_id), _now(now)
        seconds = _int(lease_seconds, 1, 600)
        _int(limit, 1, 1)
        with self.con:
            control = self._lock(owner, now)
            if control["lease_token"] and control["lease_until"] > now:
                return None
            self._expire(owner, now)
            self.con.execute("UPDATE recollection_inputs SET status='pending',lease_token='' "
                             "WHERE owner_id=? AND kind='input' AND status='running'", (owner,))
            row = self.con.execute("SELECT * FROM recollection_inputs WHERE owner_id=? AND kind='input' AND status='pending' "
                                   "AND next_attempt<=? AND expires>? ORDER BY created,id LIMIT 1", (owner, now, now)).fetchone()
            if row is None:
                self._release(owner)
                return None
            token = uuid.uuid4().hex
            self.con.execute("UPDATE recollection_control SET lease_token=?,lease_until=?,lease_id=? WHERE owner_id=?",
                             (token, now+seconds, row["id"], owner))
            self.con.execute("UPDATE recollection_inputs SET status='running',lease_token=?,updated=? WHERE id=?", (token, now, row["id"]))
            return {"id": row["id"], "token": token, "text": json.loads(row["payload"])["text"],
                    "timestamp": row["created"], "base_revision": control["revision"]}

    def _job(self, owner, ident, token, now):
        control = self._lock(owner, now)
        if (not isinstance(token, str) or not token or control["lease_token"] != token
                or control["lease_id"] != ident or control["lease_until"] <= now):
            raise ValueError("lease_conflict")
        row = self.con.execute("SELECT * FROM recollection_inputs WHERE id=? AND owner_id=? AND kind='input' "
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
            self.con.execute("UPDATE recollection_inputs SET status=?,attempts=?,lease_token='',next_attempt=?,payload=?,updated=? WHERE id=?",
                             (status, attempts, when, _json({"text": row["text"] if status == "pending" else "", "error_code": reason}), now, ident))
            self._release(owner)

    @staticmethod
    def _target(record, owner, ident, digest, input_time):
        if not isinstance(record, dict):
            raise ValueError("target_missing")
        source = record.get("source")
        prefixes = {"brain_fact": "brain:fact:", "mind": "mind:note:"+owner+":",
                    "continuity": "continuity:thread:", "bond": "bond:moment:"}
        if (not isinstance(source, str) or source not in prefixes or record.get("id") != ident
                or not ident.startswith(prefixes[source]) or len(ident) <= len(prefixes[source])
                or record.get("scope") != "private:"+owner or record.get("subject_id") != owner
                or record.get("derived") is True or record.get("active") is False
                or record.get("evidence_status") != ("owner_reported" if source in {"bond", "continuity"} else "active")
                or not isinstance(record.get("sensitivity"), str) or record["sensitivity"] not in _RANK
                or type(record.get("timestamp")) not in (int, float)):
            raise ValueError("invalid_target")
        _text(record.get("text"), 600)
        timestamp = _now(record.get("timestamp"))
        if timestamp >= input_time:
            raise ValueError("correction_not_newer")
        if source_fingerprint(record) != digest:
            raise ValueError("target_changed")
        return record

    def complete(self, ident, token, proposal, *, owner_id, expected_revision, targets, now=None):
        owner, now, expected = _owner(owner_id), _now(now), _int(expected_revision)
        if (not isinstance(proposal, dict) or set(proposal) != {"updates"} or not isinstance(proposal["updates"], list)
                or len(proposal["updates"]) > 2 or not isinstance(targets, dict) or len(targets) > 200):
            raise ValueError("invalid_proposal")
        with self.con:
            control, job = self._job(owner, ident, token, now)
            if control["revision"] != expected:
                raise ValueError("revision_conflict")
            applied, ignored, seen = [], 0, set()
            for update in proposal["updates"]:
                if not isinstance(update, dict) or set(update) != {"target_id", "target_digest", "old_quote", "quote", "relation"}:
                    raise ValueError("invalid_update")
                target_id = _text(update["target_id"], 191)
                target_digest = _digest(update["target_digest"])
                if target_id in seen:
                    raise ValueError("duplicate_target")
                seen.add(target_id)
                relation = update["relation"]
                if not isinstance(relation, str) or relation not in {"replace", "clarify"}:
                    raise ValueError("invalid_relation")
                old_quote, quote = _quote(update["old_quote"], 200), _quote(update["quote"], 180)
                target = self._target(targets.get(target_id), owner, target_id, target_digest, job["created"])
                if old_quote not in target["text"] or quote not in job["text"]:
                    raise ValueError("unverified_quote")
                first = target["text"].find(old_quote)
                if target["text"].find(old_quote, first+1) != -1:
                    raise ValueError("ambiguous_old_quote")
                latest = self.con.execute("SELECT * FROM recollection_revisions WHERE owner_id=? AND target_id=? AND target_digest=? "
                                          "ORDER BY updated DESC,created DESC,id DESC LIMIT 1", (owner, target_id, target_digest)).fetchone()
                if latest:
                    previous = self._decode(latest)
                    watermark = previous["updated"] if previous["status"] == "retracted" else previous["input_timestamp"]
                    if job["created"] <= watermark:
                        ignored += 1
                        continue
                current = self.con.execute("SELECT * FROM recollection_revisions WHERE owner_id=? AND target_id=? AND status='active'",
                                           (owner, target_id)).fetchall()
                superseded, retained = [], 0
                for row in current:
                    old = self._decode(row)
                    if old["target_digest"] != target_digest or old["old_quote"] == old_quote:
                        superseded.append(old["id"])
                        continue
                    old_start = target["text"].find(old["old_quote"])
                    if (old_start < 0 or not old["old_quote"]
                            or target["text"].find(old["old_quote"], old_start+1) != -1):
                        raise ValueError("ambiguous_active_quote")
                    if max(first, old_start) < min(first+len(old_quote), old_start+len(old["old_quote"])):
                        raise ValueError("overlapping_quote")
                    retained += 1
                if retained >= 4:
                    raise ValueError("fragment_capacity")
                count = self.con.execute("SELECT COUNT(*) FROM recollection_revisions WHERE owner_id=? AND status='active'", (owner,)).fetchone()[0]
                if count-len(superseded)+1 > 128:
                    raise ValueError("revision_capacity")
                for old_id in superseded:
                    self.con.execute("UPDATE recollection_revisions SET status='superseded',revision=revision+1,updated=? WHERE id=?", (now, old_id))
                entry_id = "recollection:revision:"+_hash(owner, ident, target_id, target_digest)
                sensitivity = max(("personal", target["sensitivity"], _sensitivity("private:"+owner, job["text"])), key=_RANK.get)
                payload = {"quote": quote, "old_quote": old_quote, "scope": "private:"+owner, "subject_id": owner,
                           "sensitivity": sensitivity, "input_digest": job["body_digest"], "input_timestamp": job["created"],
                           "evidence_status": "owner_reported", "source": target["source"]}
                self.con.execute("INSERT INTO recollection_revisions(id,owner_id,target_id,target_digest,input_id,relation,status,revision,payload,created,updated) "
                                 "VALUES (?,?,?,?,?,?,'active',1,?,?,?)",
                                 (entry_id, owner, target_id, target_digest, ident, relation, _json(payload), now, now))
                applied.append(entry_id)
            self.con.execute("UPDATE recollection_inputs SET status='processed',lease_token='',payload=?,updated=? WHERE id=?",
                             (_json({"text": "", "revision_ids": applied, "ignored": ignored}), now, ident))
            if applied:
                self.con.execute("UPDATE recollection_control SET revision=revision+1,updated=? WHERE owner_id=?", (now, owner))
            self._release(owner)
            return {"applied": len(applied), "ignored": ignored, "ids": applied, "revision": control["revision"]+int(bool(applied))}

    def active(self, owner_id, limit=200):
        owner, limit = _owner(owner_id), _int(limit, 1, 200)
        rows = self._read("SELECT * FROM recollection_revisions WHERE owner_id=? AND status='active' ORDER BY created DESC,id LIMIT ?", (owner, limit))
        return [self._decode(row) for row in rows]

    def retract(self, message, *, owner_id, revision_id, expected_revision, now=None):
        owner, now, expected = _owner(owner_id), _now(now), _int(expected_revision)
        revision_id = _text(revision_id, 191)
        receipt, digest, _ = self._receipt(message, owner, command=True)
        receipt_id, request = "recollection:command:"+receipt, _hash("retract", revision_id)
        with self.con:
            control = self._lock(owner, now)
            old = self.con.execute("SELECT * FROM recollection_inputs WHERE id=?", (receipt_id,)).fetchone()
            row = self.con.execute("SELECT * FROM recollection_revisions WHERE id=? AND owner_id=?", (revision_id, owner)).fetchone()
            if old:
                if old["body_digest"] != digest or json.loads(old["payload"])["request_hash"] != request:
                    raise ValueError("idempotency_conflict")
                return self._decode(row) if row else None
            if row is None or row["status"] != "active":
                raise ValueError("revision_not_active")
            if row["revision"] != expected:
                raise ValueError("revision_conflict")
            self.con.execute("UPDATE recollection_revisions SET status='retracted',revision=revision+1,updated=? WHERE id=?", (now, revision_id))
            self.con.execute("INSERT INTO recollection_inputs(id,owner_id,kind,body_digest,status,expires,payload,created,updated) "
                             "VALUES (?,?,'command',?,'receipt',0,?,?,?)",
                             (receipt_id, owner, digest, _json({"request_hash": request, "revision_id": revision_id}), now, now))
            self.con.execute("UPDATE recollection_inputs SET status='pending',lease_token='' WHERE owner_id=? AND kind='input' AND status='running'", (owner,))
            self._release(owner)
            self.con.execute("UPDATE recollection_control SET revision=revision+1,updated=? WHERE owner_id=?", (now, owner))
            return {**self._decode(row), "status": "retracted", "revision": row["revision"]+1, "updated": now}

    def record_delivery(self, *, owner_id, scope, receipt_id, topic_refs, text_digest, now=None):
        owner, now = _owner(owner_id), _now(now)
        if scope != "private:"+owner:
            raise ValueError("owner_private_scope_required")
        receipt_id, text_digest = _text(receipt_id, 256), _digest(text_digest)
        if not isinstance(topic_refs, list) or len(topic_refs) > 12:
            raise ValueError("invalid_topic_refs")
        refs = {}
        for ref in topic_refs:
            if not isinstance(ref, dict) or set(ref) != {"id", "digest"}:
                raise ValueError("invalid_topic_ref")
            key, version = _text(ref["id"], 191), _digest(ref["digest"])
            if key in refs and refs[key] != version:
                raise ValueError("topic_version_conflict")
            refs[key] = version
        normalized = [{"id": key, "digest": refs[key]} for key in sorted(refs)]
        ident = "recollection:delivery:"+_hash(owner, scope, receipt_id)
        request = _hash(scope, normalized, text_digest)
        with self.con:
            self._lock(owner, now)
            old = self.con.execute("SELECT body_digest FROM recollection_inputs WHERE id=?", (ident,)).fetchone()
            if old:
                if old["body_digest"] != request:
                    raise ValueError("idempotency_conflict")
                return False
            self.con.execute("INSERT INTO recollection_inputs(id,owner_id,kind,body_digest,status,expires,payload,created,updated) "
                             "VALUES (?,?,'delivery',?,'receipt',0,?,?,?)", (ident, owner, request, "{}", now, now))
            self.con.execute("INSERT INTO recollection_deliveries(id,owner_id,scope,text_digest,payload,created) VALUES (?,?,?,?,?,?)",
                             (ident, owner, scope, text_digest, _json({"topic_refs": normalized}), now))
            expired = self.con.execute("SELECT id FROM recollection_deliveries WHERE owner_id=? ORDER BY created DESC,id DESC LIMIT ? OFFSET ?",
                                       (owner, 1000, 256)).fetchall()
            for row in expired:
                self.con.execute("DELETE FROM recollection_deliveries WHERE id=? AND owner_id=?", (row["id"], owner))
            return True

    def recent_deliveries(self, owner_id, now=None, within_seconds=86400):
        owner, now, interval = _owner(owner_id), _now(now), _int(within_seconds, 1, 31*86400)
        rows = self._read("SELECT * FROM recollection_deliveries WHERE owner_id=? AND created>=? AND created<=? "
                          "ORDER BY created DESC,id DESC LIMIT 256", (owner, now-interval, now))
        return [self._decode(row) for row in rows]

    def status(self, owner_id, now=None):
        owner, now = _owner(owner_id), _now(now)
        state = self.state(owner, now=now)
        counts = self._read("SELECT status,COUNT(*) AS n FROM recollection_inputs WHERE owner_id=? AND kind='input' GROUP BY status", (owner,))
        queued = self._read("SELECT COUNT(*) AS n FROM recollection_inputs WHERE owner_id=? AND kind='input' "
                            "AND status IN ('pending','running') AND expires>?", (owner, now))[0]["n"]
        active = self._read("SELECT COUNT(*) AS n FROM recollection_revisions WHERE owner_id=? AND status='active'", (owner,))[0]["n"]
        deliveries = self._read("SELECT COUNT(*) AS n FROM recollection_deliveries WHERE owner_id=?", (owner,))[0]["n"]
        return {**state, "inputs": {row["status"]: row["n"] for row in counts}, "queued_inputs": queued,
                "queue_full": queued >= 32, "input_capacity": 32, "active_revisions": active,
                "revision_capacity": 128, "deliveries": deliveries}
