"""Trusted owner contact preferences and evidence-backed conversational continuity.

The host validates the current owner binding; this ledger additionally validates
each captured private message. Models cannot write contact/support preferences.
No relationship scores, silence penalties or autonomous commitments are stored.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
import unicodedata
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


def _text(value, limit, *, blank=False):
    if (not isinstance(value, str) or len(value) > limit or (not blank and not value.strip())
            or any(ord(c) < 32 and c not in "\n\r\t" for c in value)):
        raise ValueError("invalid_text")
    return value.strip()


def _key(value):
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def _defaults(owner_id):
    return {"owner_id": owner_id, "revision": 0,
            "contact": {"mode": "normal", "until": 0},
            "support": {"mode": "normal", "until": 0},
            "budget_day": 0, "cycle_count": 0, "last_cycle": 0,
            "lease_token": "", "lease_until": 0, "lease_id": "", "created": 0, "updated": 0}


class ContinuityStore:
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
        CREATE TABLE IF NOT EXISTS continuity_control (
          owner_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0,
          payload TEXT NOT NULL, budget_day REAL NOT NULL DEFAULT 0,
          cycle_count INTEGER NOT NULL DEFAULT 0, last_cycle REAL NOT NULL DEFAULT 0,
          lease_token TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0,
          lease_id TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS continuity_inputs (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, body_digest TEXT NOT NULL,
          status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
          lease_token TEXT NOT NULL DEFAULT '', next_attempt REAL NOT NULL DEFAULT 0,
          expires REAL NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_continuity_inputs_owner ON continuity_inputs(owner_id,status,created);
        CREATE TABLE IF NOT EXISTS continuity_threads (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, kind TEXT NOT NULL,
          status TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
          created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_continuity_threads_owner ON continuity_threads(owner_id,status,updated);
        CREATE TABLE IF NOT EXISTS continuity_audit (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, entity_id TEXT NOT NULL,
          action TEXT NOT NULL, body_digest TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_continuity_audit_owner ON continuity_audit(owner_id,created);
        """)
        self.con.commit()

    def close(self):
        self.con.close()

    @staticmethod
    def _decode(row):
        result = dict(row)
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

    @staticmethod
    def _effective(row, now):
        result = json.loads(_json(row))
        for field in ("contact", "support"):
            value = result[field]
            value["active"] = value["mode"] != "normal" and (not value["until"] or value["until"] > now)
            value["effective_mode"] = value["mode"] if value["active"] else "normal"
        return result

    def state(self, owner_id, now=None):
        owner_id, now = _owner(owner_id), _now(now)
        rows = self._read("SELECT * FROM continuity_control WHERE owner_id=?", (owner_id,))
        return self._effective(self._decode(rows[0]) if rows else _defaults(owner_id), now)

    def _lock(self, owner_id, now):
        # Acquire an exclusive owner row before any consistent-read snapshot.
        # INSERT IGNORE followed by UPDATE would deadlock concurrent MySQL creators.
        default = _defaults(owner_id)
        self.con.execute("INSERT INTO continuity_control(owner_id,payload,created,updated) VALUES (?,?,?,?) "
                         "ON CONFLICT(owner_id) DO UPDATE SET revision=revision",
                         (owner_id, _json({k: default[k] for k in ("contact", "support")}), now, now))
        return self._decode(self.con.execute("SELECT * FROM continuity_control WHERE owner_id=?", (owner_id,)).fetchone())

    @staticmethod
    def _receipt(message, owner_id, maximum=600):
        sender = extract_sender(message)
        if (not sender or sender.platform != "qq" or sender.kind != "private" or sender.user_id != owner_id
                or sender.target_id != owner_id or not sender.stream_id or not sender.message_id
                or str(message.get("account_id") or "") == owner_id):
            raise ValueError("trusted_owner_private_message_required")
        parts = message.get("raw_message")
        if isinstance(parts, list) and any(not isinstance(p, dict) or p.get("type") != "text" for p in parts):
            raise ValueError("trusted_owner_text_required")
        body = message.get("processed_plain_text")
        _text(body, maximum)
        stream, mid = _text(sender.stream_id, 256), _text(sender.message_id, 256)
        return _hash("qq", owner_id, stream, mid), hashlib.sha256(body.encode("utf-8")).hexdigest(), body

    def _audit(self, audit_id, owner, entity, action, body_digest, payload, now):
        self.con.execute("INSERT INTO continuity_audit(id,owner_id,entity_id,action,body_digest,payload,created) "
                         "VALUES (?,?,?,?,?,?,?)", (audit_id, owner, entity, action, body_digest, _json(payload), now))

    @staticmethod
    def _revision(state, expected):
        if expected is not None and state["revision"] != _int(expected):
            raise ValueError("revision_conflict")

    def command(self, message, *, owner_id, action, value=None, until=None, expected_revision=None,
                with_receipt=False, now=None):
        owner, now = _owner(owner_id), _now(now)
        if type(with_receipt) is not bool:
            raise ValueError("invalid_receipt_option")
        receipt, digest, _ = self._receipt(message, owner)
        if not isinstance(action, str) or action not in {"contact_pause", "contact_resume", "support"}:
            raise ValueError("invalid_action")
        if action == "contact_pause" and (not isinstance(value, str) or value not in {"busy", "rest", "manual"}):
            raise ValueError("invalid_contact_mode")
        if action == "support" and (not isinstance(value, str) or value not in {"listen", "advice", "normal"}):
            raise ValueError("invalid_support_mode")
        until = 0 if until is None else _now(until)
        if action == "contact_resume":
            value, until = "normal", 0
        if action == "support" and value == "normal":
            until = 0
        # Relative-time commands are re-parsed later on host replay. The trusted
        # message body and action define identity; the first expiry is retained.
        request_hash = _hash(action, value)
        with self.con:
            before = self._lock(owner, now)
            old = self.con.execute("SELECT * FROM continuity_audit WHERE id=?", ("command:"+receipt,)).fetchone()
            if old is not None:
                if old["body_digest"] != digest or json.loads(old["payload"])["request_hash"] != request_hash:
                    raise ValueError("idempotency_conflict")
                state = self._effective(before, now)
                return {"state": state, "new_receipt": False} if with_receipt else state
            self._revision(before, expected_revision)
            if until and not now < until <= now+31*86400:
                raise ValueError("invalid_expiry")
            if action == "contact_pause" and value in {"busy", "rest"} and not until:
                raise ValueError("expiry_required")
            payload = {key: before[key] for key in ("contact", "support")}
            field = "support" if action == "support" else "contact"
            payload[field] = {"mode": value, "until": until}
            changed = any(payload[key] != before[key] for key in payload)
            revision = before["revision"]+int(changed)
            if changed:
                self.con.execute("UPDATE continuity_control SET payload=?,revision=?,updated=? WHERE owner_id=?",
                                 (_json(payload), revision, now, owner))
            self._audit("command:"+receipt, owner, "owner:"+owner, action, digest,
                        {"request_hash": request_hash, "before": {k: before[k] for k in payload},
                         "after": payload, "revision": revision}, now)
            state = self._effective({**before, **payload, "revision": revision,
                                     "updated": now if changed else before["updated"]}, now)
            return {"state": state, "new_receipt": True} if with_receipt else state

    def reserve_cycle(self, owner_id, *, day_start, interval_seconds, daily_limit, now=None):
        owner, now, day = _owner(owner_id), _now(now), _now(day_start)
        gap, limit = _int(interval_seconds, 0, 31*86400), _int(daily_limit, 0, 1000)
        if day > now or now-day >= 86400:
            raise ValueError("invalid_day")
        with self.con:
            row = self._lock(owner, now)
            count = row["cycle_count"] if row["budget_day"] == day else 0
            if count >= limit or (row["cycle_count"] and now-row["last_cycle"] < gap) or day < row["budget_day"]:
                return False
            self.con.execute("UPDATE continuity_control SET budget_day=?,cycle_count=?,last_cycle=? WHERE owner_id=?",
                             (day, count+1, now, owner))
            return True

    def enqueue(self, message, *, owner_id, source_ref=None, now=None, ttl_seconds=604800):
        owner, now = _owner(owner_id), _now(now)
        receipt, digest, body = self._receipt(message, owner)
        ttl = _int(ttl_seconds, 60, 31*86400)
        if source_ref is not None:
            if (not isinstance(source_ref, dict) or set(source_ref) != {"id", "digest", "scope", "sensitivity"}
                    or source_ref["scope"] != "private:"+owner
                    or source_ref["sensitivity"] not in {"public", "personal", "unknown", "confidential"}):
                raise ValueError("invalid_source_ref")
            for key in source_ref:
                _text(source_ref[key], 191)
        ident = "continuity:input:"+receipt
        with self.con:
            self._lock(owner, now)
            old = self.con.execute("SELECT body_digest,payload FROM continuity_inputs WHERE id=?", (ident,)).fetchone()
            if old is not None:
                if old["body_digest"] != digest or json.loads(old["payload"])["source_ref"] != source_ref:
                    raise ValueError("idempotency_conflict")
                return ident
            self._expire(owner, now)
            size = self.con.execute("SELECT COUNT(*) FROM continuity_inputs WHERE owner_id=? AND status IN ('pending','running')",
                                    (owner,)).fetchone()[0]
            if size >= 64:
                return None
            self.con.execute("INSERT INTO continuity_inputs(id,owner_id,body_digest,status,expires,payload,created,updated) "
                             "VALUES (?,?,?,'pending',?,?,?,?)",
                             (ident, owner, digest, now+ttl, _json({"text": body, "source_ref": source_ref}), now, now))
            return ident

    def _expire(self, owner, now):
        rows = self.con.execute("SELECT * FROM continuity_inputs WHERE owner_id=? "
                                "AND status IN ('pending','running') AND expires<=?", (owner, now)).fetchall()
        for row in rows:
            payload = json.loads(row["payload"])
            payload["text"] = ""
            self.con.execute("UPDATE continuity_inputs SET status='expired',lease_token='',payload=?,updated=? WHERE id=?",
                             (_json(payload), now, row["id"]))

    def claim(self, owner_id, *, now=None, lease_seconds=120, limit=4):
        owner, now = _owner(owner_id), _now(now)
        seconds, limit = _int(lease_seconds, 1, 600), _int(limit, 1, 4)
        with self.con:
            control = self._lock(owner, now)
            if control["lease_token"] and control["lease_until"] > now:
                return None
            self._expire(owner, now)
            self.con.execute("UPDATE continuity_inputs SET status='pending',lease_token='' "
                             "WHERE owner_id=? AND status='running'", (owner,))
            rows = self.con.execute("SELECT * FROM continuity_inputs WHERE owner_id=? AND status='pending' "
                                    "AND next_attempt<=? AND expires>? ORDER BY created,id LIMIT ?",
                                    (owner, now, now, limit)).fetchall()
            if not rows:
                self.con.execute("UPDATE continuity_control SET lease_token='',lease_until=0,lease_id='' WHERE owner_id=?", (owner,))
                return None
            token, ident = uuid.uuid4().hex, rows[0]["id"]
            self.con.execute("UPDATE continuity_control SET lease_token=?,lease_until=?,lease_id=? WHERE owner_id=?",
                             (token, now+seconds, ident, owner))
            inputs = []
            for row in rows:
                self.con.execute("UPDATE continuity_inputs SET status='running',lease_token=?,updated=? WHERE id=?", (token, now, row["id"]))
                inputs.append({"id": row["id"], "text": json.loads(row["payload"])["text"], "timestamp": row["created"]})
            return {"id": ident, "token": token, "inputs": inputs, "base_revision": control["revision"]}

    def _batch(self, owner, ident, token, now):
        control = self._lock(owner, now)
        if (not isinstance(token, str) or not token or control["lease_token"] != token
                or control["lease_id"] != ident or control["lease_until"] <= now):
            raise ValueError("lease_conflict")
        rows = self.con.execute("SELECT * FROM continuity_inputs WHERE owner_id=? AND status='running' AND lease_token=? "
                                "ORDER BY created,id", (owner, token)).fetchall()
        if not rows or any(row["expires"] <= now for row in rows):
            raise ValueError("input_expired")
        return control, {row["id"]: self._decode(row) for row in rows}

    def _release(self, owner):
        self.con.execute("UPDATE continuity_control SET lease_token='',lease_until=0,lease_id='' WHERE owner_id=?", (owner,))

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
            _, rows = self._batch(owner, ident, token, now)
            for row in rows.values():
                attempts = row["attempts"]+int(failed)
                status = "failed" if attempts >= 3 else "pending"
                payload = {"text": row["text"] if status == "pending" else "", "source_ref": row["source_ref"], "error_code": reason}
                next_attempt = retry_at if retry_at is not None else now+min(3600, 60*2**attempts)
                self.con.execute("UPDATE continuity_inputs SET status=?,attempts=?,lease_token='',next_attempt=?,payload=?,updated=? WHERE id=?",
                                 (status, attempts, next_attempt, _json(payload), now, row["id"]))
            self._release(owner)

    def _sources_valid(self, row, *, connection=False):
        for ref in row["source_refs"]:
            sql = "SELECT owner_id,body_digest,status FROM continuity_inputs WHERE id=?"
            source = (self.con.execute(sql, (ref["id"],)).fetchone() if connection else
                      next(iter(self._read(sql, (ref["id"],))), None))
            if (source is None or source["owner_id"] != row["owner_id"] or source["body_digest"] != ref["digest"]
                    or source["status"] not in {"processed", "running"}):
                return False
        return bool(row["source_refs"])

    def complete(self, ident, token, proposal, *, owner_id, expected_revision, now=None):
        owner, now = _owner(owner_id), _now(now)
        expected_revision = _int(expected_revision)
        if not isinstance(proposal, dict) or set(proposal) != {"updates"} or not isinstance(proposal["updates"], list) or len(proposal["updates"]) > 2:
            raise ValueError("invalid_proposal")
        with self.con:
            control, inputs = self._batch(owner, ident, token, now)
            self._revision(control, expected_revision)
            stats, updated_ids = {"opened": 0, "resolved": 0, "dismissed": 0, "ignored": 0}, set()
            for update in proposal["updates"]:
                if not isinstance(update, dict) or set(update) != {"action", "thread_id", "kind", "title", "summary", "evidence"}:
                    raise ValueError("invalid_update")
                action, kind = update["action"], update["kind"]
                if (not isinstance(action, str) or action not in {"open", "resolve", "dismiss"}
                        or not isinstance(kind, str) or kind not in {"event", "concern", "wish"}):
                    raise ValueError("invalid_update")
                title, summary = _text(update["title"], 60), _text(update["summary"], 180)
                thread_id = _text(update["thread_id"], 191, blank=True)
                if action != "open" and not thread_id:
                    raise ValueError("invalid_thread_target")
                evidence = update["evidence"]
                if not isinstance(evidence, list) or not 1 <= len(evidence) <= 2:
                    raise ValueError("invalid_evidence")
                refs, quotes = {}, []
                for ev in evidence:
                    if not isinstance(ev, dict) or set(ev) != {"id", "quote"} or not isinstance(ev["id"], str) or ev["id"] not in inputs:
                        raise ValueError("unknown_evidence")
                    quote = _text(ev["quote"], 160)
                    source = inputs[ev["id"]]
                    if quote not in source["text"] or ev["id"] in refs:
                        raise ValueError("unverified_quote")
                    refs[ev["id"]] = {"id": ev["id"], "digest": source["body_digest"],
                                      "scope": "private:"+owner,
                                      "sensitivity": (source["source_ref"] or {}).get("sensitivity", "personal")}
                    if refs[ev["id"]]["sensitivity"] == "public":
                        refs[ev["id"]]["sensitivity"] = "personal"
                    quotes.append({"id": ev["id"], "quote": quote, "timestamp": source["created"]})
                explicit_target = bool(thread_id)
                if not thread_id:
                    # Kind is a classification, not identity: changing concern to
                    # event must not bypass a dismissed or forgotten title.
                    thread_id = "continuity:thread:"+_hash(owner, _key(title))
                if thread_id in updated_ids:
                    raise ValueError("duplicate_thread_update")
                updated_ids.add(thread_id)
                old_row = self.con.execute("SELECT * FROM continuity_threads WHERE id=? AND owner_id=?", (thread_id, owner)).fetchone()
                before = self._decode(old_row) if old_row else None
                if explicit_target and (before is None or before["status"] != "open"
                                        or before["kind"] != kind or before["title"] != title):
                    raise ValueError("thread_not_open")
                # A completed exam can recur. Only a fresh owner report captured
                # after completion starts a new round; pending old reports cannot.
                # Dismissed/forgotten topics remain suppressed across classifications.
                new_round = bool(before and before["status"] == "resolved" and not explicit_target
                                 and before["kind"] == kind
                                 and all(inputs[ref_id]["created"] > before["updated"] for ref_id in refs))
                if before and not new_round and (before["status"] != "open" or before["kind"] != kind
                                                or not self._sources_valid(before, connection=True)):
                    stats["ignored"] += 1
                    continue
                if before and not new_round:
                    for ref in before["source_refs"]:
                        if ref["id"] in refs and ref != refs[ref["id"]]:
                            raise ValueError("source_revision_conflict")
                        refs[ref["id"]] = ref
                    if len(refs) > 12:
                        raise ValueError("source_capacity")
                    # Keep evidence supporting earlier summaries without keeping entire chats.
                    quotes = before["evidence"][:1]+(before["evidence"][1:]+quotes)[-3:]
                if before is None or new_round:
                    rows = self.con.execute("SELECT * FROM continuity_threads WHERE owner_id=? AND status='open'", (owner,)).fetchall()
                    count = sum(self._sources_valid(self._decode(row), connection=True) for row in rows)
                    if count >= 64:
                        raise ValueError("thread_capacity")
                status = {"open": "open", "resolve": "resolved", "dismiss": "dismissed"}[action]
                payload = {"title": title, "summary": summary, "evidence": quotes,
                           "source_refs": sorted(refs.values(), key=lambda r: r["id"]),
                           "scope": "private:"+owner, "subject_id": owner, "evidence_status": "owner_reported",
                           "sensitivity": max((r["sensitivity"] for r in refs.values()),
                                              key={"personal": 1, "unknown": 2, "confidential": 3}.get)}
                revision = before["revision"]+1 if before else 1
                if before:
                    self.con.execute("UPDATE continuity_threads SET status=?,revision=?,payload=?,updated=? WHERE id=?",
                                     (status, revision, _json(payload), now, thread_id))
                else:
                    self.con.execute("INSERT INTO continuity_threads(id,owner_id,kind,status,revision,payload,created,updated) VALUES (?,?,?,?,?,?,?,?)",
                                     (thread_id, owner, kind, status, revision, _json(payload), now, now))
                after = {"id": thread_id, "owner_id": owner, "kind": kind, "status": status, "revision": revision,
                         **payload, "created": before["created"] if before else now, "updated": now}
                self._audit("update:"+_hash(token, thread_id), owner, thread_id, action, "",
                            {"before": before, "after": after, "input_ids": list(inputs)}, now)
                stats[{"open": "opened", "resolve": "resolved", "dismiss": "dismissed"}[action]] += 1
            for row in inputs.values():
                self.con.execute("UPDATE continuity_inputs SET status='processed',lease_token='',payload=?,updated=? WHERE id=?",
                                 (_json({"text": "", "source_ref": row["source_ref"]}), now, row["id"]))
            changed = stats["opened"]+stats["resolved"]+stats["dismissed"]
            if changed:
                self.con.execute("UPDATE continuity_control SET revision=revision+1,updated=? WHERE owner_id=?", (now, owner))
            self._release(owner)
            return {**stats, "revision": control["revision"]+int(bool(changed)), "processed": len(inputs)}

    def threads(self, owner_id, limit=30, *, status="open", now=None):
        owner, limit = _owner(owner_id), _int(limit, 1, 200)
        _now(now)
        if status not in {"open", "resolved", "dismissed", "all"}:
            raise ValueError("invalid_status")
        sql, args = "SELECT * FROM continuity_threads WHERE owner_id=?", [owner]
        if status != "all":
            sql += " AND status=?"
            args.append(status)
        # Invalid historical rows remain tombstones and must not consume either
        # the active quota or the result limit ahead of valid older records.
        result, offset = [], 0
        while len(result) < limit:
            rows = self._read(sql+" ORDER BY updated DESC,id LIMIT ? OFFSET ?", (*args, 200, offset))
            for row in map(self._decode, rows):
                if self._sources_valid(row):
                    result.append(row)
                    if len(result) >= limit:
                        break
            if len(rows) < 200:
                break
            offset += len(rows)
        return result

    def thread(self, ident, *, owner_id):
        owner = _owner(owner_id)
        rows = self._read("SELECT * FROM continuity_threads WHERE id=? AND owner_id=?", (_text(ident, 191), owner))
        if not rows:
            return None
        row = self._decode(rows[0])
        return row if self._sources_valid(row) else None

    def forget_thread(self, message, *, owner_id, thread_id, expected_revision, now=None):
        owner, now, thread_id = _owner(owner_id), _now(now), _text(thread_id, 191)
        receipt, digest, _ = self._receipt(message, owner)
        expected_revision = _int(expected_revision)
        request_hash = _hash("forget", thread_id)
        with self.con:
            control = self._lock(owner, now)
            old_audit = self.con.execute("SELECT * FROM continuity_audit WHERE id=?", ("command:"+receipt,)).fetchone()
            row = self.con.execute("SELECT * FROM continuity_threads WHERE id=? AND owner_id=?", (thread_id, owner)).fetchone()
            if old_audit:
                if old_audit["body_digest"] != digest or json.loads(old_audit["payload"])["request_hash"] != request_hash:
                    raise ValueError("idempotency_conflict")
                return self._decode(row) if row else None
            if row is None:
                raise ValueError("thread_missing")
            before = self._decode(row)
            self._revision(before, expected_revision)
            refs = before["source_refs"]
            for ref in refs:
                source = self.con.execute("SELECT payload FROM continuity_inputs WHERE id=? AND owner_id=?", (ref["id"], owner)).fetchone()
                if source:
                    payload = json.loads(source["payload"])
                    payload["text"] = ""
                    self.con.execute("UPDATE continuity_inputs SET status='revoked',lease_token='',payload=?,updated=? WHERE id=? AND owner_id=?",
                                     (_json(payload), now, ref["id"], owner))
            payload = {"title": "", "summary": "", "evidence": [], "source_refs": [], "forgotten": True,
                       "scope": "private:"+owner, "subject_id": owner, "evidence_status": "owner_reported",
                       "sensitivity": before["sensitivity"]}
            self.con.execute("UPDATE continuity_threads SET status='dismissed',revision=revision+1,payload=?,updated=? WHERE id=?",
                             (_json(payload), now, thread_id))
            # Preserve operation/revision hashes, remove this item's historic prose.
            audits = self.con.execute("SELECT id,payload FROM continuity_audit WHERE owner_id=? AND entity_id=?", (owner, thread_id)).fetchall()
            for audit in audits:
                data = json.loads(audit["payload"])
                self.con.execute("UPDATE continuity_audit SET payload=? WHERE id=?",
                                 (_json({k: v for k, v in data.items() if k not in {"before", "after"}} | {"forgotten": True}), audit["id"]))
            self.con.execute("UPDATE continuity_inputs SET status='pending',lease_token='' WHERE owner_id=? AND status='running'", (owner,))
            self._release(owner)
            self.con.execute("UPDATE continuity_control SET revision=revision+1,updated=? WHERE owner_id=?", (now, owner))
            self._audit("command:"+receipt, owner, thread_id, "forget", digest,
                        {"request_hash": request_hash, "revision": control["revision"]+1,
                         "thread_revision": before["revision"]+1, "revoked_inputs": [r["id"] for r in refs]}, now)
            return {**before, **payload, "status": "dismissed", "revision": before["revision"]+1, "updated": now}

    def revoke_input(self, ident, *, owner_id, now=None):
        owner, now, ident = _owner(owner_id), _now(now), _text(ident, 191)
        with self.con:
            control = self._lock(owner, now)
            row = self.con.execute("SELECT * FROM continuity_inputs WHERE id=? AND owner_id=?", (ident, owner)).fetchone()
            if row is None or row["status"] == "revoked":
                return False
            payload = json.loads(row["payload"])
            payload["text"] = ""
            self.con.execute("UPDATE continuity_inputs SET status='revoked',lease_token='',payload=?,updated=? WHERE id=?", (_json(payload), now, ident))
            self.con.execute("UPDATE continuity_inputs SET status='pending',lease_token='' WHERE owner_id=? AND status='running'", (owner,))
            self._release(owner)
            self.con.execute("UPDATE continuity_control SET revision=revision+1,updated=? WHERE owner_id=?", (now, owner))
            self._audit("revoke:"+_hash(ident), owner, ident, "revoke", row["body_digest"], {"revision": control["revision"]+1}, now)
            return True

    def audit(self, owner_id, limit=20):
        owner, limit = _owner(owner_id), _int(limit, 1, 200)
        return [self._decode(row) for row in self._read("SELECT * FROM continuity_audit WHERE owner_id=? ORDER BY created DESC,id LIMIT ?", (owner, limit))]

    def status(self, owner_id, now=None):
        owner, now = _owner(owner_id), _now(now)
        state = self.state(owner, now=now)
        rows = self._read("SELECT status,COUNT(*) AS n FROM continuity_inputs WHERE owner_id=? GROUP BY status", (owner,))
        queued = self._read("SELECT COUNT(*) AS n FROM continuity_inputs WHERE owner_id=? "
                            "AND status IN ('pending','running') AND expires>?", (owner, now))[0]["n"]
        return {"revision": state["revision"], "inputs": {row["status"]: row["n"] for row in rows},
                "queued_inputs": queued, "input_capacity": 64, "queue_full": queued >= 64,
                "open_threads": len(self.threads(owner, 200)), "contact": state["contact"], "support": state["support"],
                "budget_day": state["budget_day"], "cycle_count": state["cycle_count"], "last_cycle": state["last_cycle"]}
