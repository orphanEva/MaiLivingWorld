"""Source-bound reading progress and explicitly accepted owner plans.

Platform identity and source resolution belong to trusted callers. Notes are
derived material, never a substitute for original tool evidence or owner consent.
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
from .owner import normalize_qq
from .sql_logging import connect_sqlite


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(*values):
    return hashlib.sha256(_json(values).encode("utf-8")).hexdigest()


def _text(value, maximum=191, *, empty=False, multiline=False):
    if (not isinstance(value, str) or len(value) > maximum or (not value.strip() and not empty)
            or any(ord(c) < 32 and (not multiline or c not in "\n\r\t") for c in value)):
        raise ValueError("invalid_text")
    return value.strip()


def _owner(value):
    if not isinstance(value, str) or not value or normalize_qq(value) != value:
        raise ValueError("invalid_owner")
    return value


def _number(value):
    if type(value) not in (int, float):
        raise ValueError("invalid_number")
    try:
        if not math.isfinite(value) or not 0 <= value <= 1e12:
            raise ValueError("invalid_number")
    except OverflowError:
        raise ValueError("invalid_number") from None
    return float(value)


def _now(value=None):
    return _number(time.time() if value is None else value)


def _integer(value, minimum=0, maximum=100000000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("invalid_integer")
    return value


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("invalid_digest")
    return value


def _refs(values, *, empty=False):
    if not isinstance(values, (list, tuple)) or not (0 if empty else 1) <= len(values) <= 512:
        raise ValueError("invalid_sources")
    refs = {}
    for value in values:
        if not isinstance(value, dict) or set(value) != {"id", "digest", "scope", "sensitivity"}:
            raise ValueError("invalid_sources")
        identity, sensitivity = value["id"], value["sensitivity"]
        if not isinstance(identity, str) or not re.fullmatch(r"tool:observation:[0-9a-f]{64}", identity):
            raise ValueError("invalid_sources")
        if not isinstance(sensitivity, str) or sensitivity not in {"public", "personal", "unknown", "confidential"}:
            raise ValueError("invalid_sources")
        ref = {"id": identity, "digest": _digest(value["digest"]),
               "scope": _text(value["scope"], 100), "sensitivity": sensitivity}
        if identity in refs and refs[identity] != ref:
            raise ValueError("source_revision_conflict")
        refs[identity] = ref
    if len(refs) > 256:
        raise ValueError("source_capacity")
    return sorted(refs.values(), key=lambda row: row["id"])


def _note(value):
    if isinstance(value, str):
        value = {"summary": _text(value, 2000, empty=True, multiline=True), "questions": [], "quotes": [], "self_response": ""}
    if not isinstance(value, dict) or set(value)-{"summary", "questions", "quotes", "self_response"}:
        raise ValueError("invalid_note")
    result = {"summary": _text(value.get("summary", ""), 2000, empty=True, multiline=True),
              "self_response": _text(value.get("self_response", ""), 1000, empty=True, multiline=True)}
    for key in ("questions", "quotes"):
        items = value.get(key, [])
        if not isinstance(items, list) or len(items) > 8:
            raise ValueError("invalid_note")
        result[key] = [_text(item, 600, multiline=True) for item in items]
    if len(_json(result)) > 4000:
        raise ValueError("invalid_note")
    return result


class CompanionStore:
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
        CREATE TABLE IF NOT EXISTS companion_control (
          slot INTEGER PRIMARY KEY CHECK(slot=1), updated REAL NOT NULL DEFAULT 0,
          owner_id TEXT NOT NULL DEFAULT '', budget_day REAL NOT NULL DEFAULT 0,
          daily_count INTEGER NOT NULL DEFAULT 0, last_cycle REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS companion_readings (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, scope TEXT NOT NULL,
          provider TEXT NOT NULL, resource_type TEXT NOT NULL, resource_id TEXT NOT NULL,
          version TEXT NOT NULL, status TEXT NOT NULL, cursor_pos INTEGER NOT NULL DEFAULT 0,
          total_units INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL,
          lease_token TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0,
          payload TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_companion_readings_owner ON companion_readings(owner_id,status,updated);
        CREATE TABLE IF NOT EXISTS companion_units (
          id TEXT PRIMARY KEY, reading_id TEXT NOT NULL, unit_index INTEGER NOT NULL,
          next_cursor INTEGER NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL,
          UNIQUE(reading_id,unit_index)
        );
        CREATE TABLE IF NOT EXISTS companion_plans (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, scope TEXT NOT NULL,
          status TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
          created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_companion_plans_owner ON companion_plans(owner_id,status,updated);
        CREATE TABLE IF NOT EXISTS companion_audit (
          id TEXT PRIMARY KEY, entity_id TEXT NOT NULL, owner_id TEXT NOT NULL,
          action TEXT NOT NULL, message_digest TEXT NOT NULL, summary_hash TEXT NOT NULL,
          payload TEXT NOT NULL, created REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_companion_audit_entity ON companion_audit(entity_id,created);
        """)
        self.con.commit()

    def close(self):
        self.con.close()

    def reserve_reading_cycle(self, *, owner_id, day_start, interval_seconds, daily_limit, now=None):
        owner_id, timestamp, day_start = _owner(owner_id), _now(now), _number(day_start)
        _integer(interval_seconds, 0, 30*86400)
        _integer(daily_limit, 0, 100000)
        if not day_start <= timestamp < day_start+86400:
            raise ValueError("invalid_day")
        if daily_limit == 0:
            return False
        with self.con:
            self._lock()
            current = self.con.execute("SELECT * FROM companion_control WHERE slot=1").fetchone()
            if (day_start < current["budget_day"]
                    or (current["daily_count"] and current["last_cycle"]+interval_seconds > timestamp)
                    or (current["budget_day"] == day_start and current["daily_count"] >= daily_limit)):
                return False
            count = current["daily_count"]+1 if day_start == current["budget_day"] else 1
            self.con.execute("UPDATE companion_control SET owner_id=?,budget_day=?,daily_count=?,last_cycle=?,updated=? "
                             "WHERE slot=1", (owner_id, day_start, count, timestamp, timestamp))
            return True

    def _lock(self):
        # InnoDB must acquire an exclusive lock without an INSERT IGNORE shared
        # lock upgrade. This write precedes every transaction's snapshot reads.
        self.con.execute("INSERT INTO companion_control(slot) VALUES (1) "
                         "ON CONFLICT(slot) DO UPDATE SET updated=updated")

    def _read(self, sql, parameters=()):
        if isinstance(self.con, MySQLCompatConnection):
            return [dict(row) for row in self.con.fetch_readonly(sql, parameters)]
        cursor = self.con.execute(sql, parameters)
        try:
            return [dict(row) for row in cursor.fetchall()]
        finally:
            cursor.close()

    @staticmethod
    def _unpack(row):
        if row is None:
            return None
        value = dict(row)
        value.update(json.loads(value.pop("payload")))
        if "cursor_pos" in value:
            value["cursor"] = value.pop("cursor_pos")
        if "total_units" in value and value["total_units"] == 0:
            value["total_units"] = None
        return value

    def _row(self, table, identity):
        row = self.con.execute("SELECT * FROM "+table+" WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise ValueError("record_missing")
        return self._unpack(row)

    def reading(self, id, *, owner_id=None):
        rows = self._read("SELECT * FROM companion_readings WHERE id=?", (_text(id),))
        value = self._unpack(rows[0]) if rows else None
        if value is not None and owner_id is not None:
            self._authorize(value, owner_id)
        return value

    def plan(self, id, *, owner_id=None):
        rows = self._read("SELECT * FROM companion_plans WHERE id=?", (_text(id),))
        value = self._unpack(rows[0]) if rows else None
        if value is not None and owner_id is not None:
            self._authorize(value, owner_id)
        return value

    def readings(self, owner_id=None, limit=50, *, status=None, resource_type=None, order="newest"):
        _integer(limit, 1, 200)
        clauses, parameters = [], []
        if owner_id is not None:
            clauses.append("owner_id=?")
            parameters.append(_owner(owner_id))
        if status is not None:
            if not isinstance(status, str) or status not in {"active", "paused", "completed"}:
                raise ValueError("invalid_status")
            clauses.append("status=?")
            parameters.append(status)
        if resource_type is not None:
            if not isinstance(resource_type, str) or resource_type not in {"novel", "pdf", "illust"}:
                raise ValueError("invalid_resource")
            clauses.append("resource_type=?")
            parameters.append(resource_type)
        if not isinstance(order, str) or order not in {"newest", "oldest"}:
            raise ValueError("invalid_order")
        clause = " WHERE "+" AND ".join(clauses) if clauses else ""
        direction = "ASC" if order == "oldest" else "DESC"
        return [self._unpack(row) for row in self._read(
            "SELECT * FROM companion_readings"+clause+" ORDER BY updated "+direction+",id LIMIT ?", (*parameters, limit))]

    def plans(self, owner_id=None, status=None, limit=50, *, due_before=None, oldest=False):
        _integer(limit, 1, 200)
        if type(oldest) is not bool:
            raise ValueError("invalid_order")
        clauses, values = [], []
        if owner_id is not None:
            clauses.append("owner_id=?")
            values.append(_owner(owner_id))
        if status is not None:
            if not isinstance(status, str) or status not in {"proposed", "active", "paused", "completed", "cancelled"}:
                raise ValueError("invalid_status")
            clauses.append("status=?")
            values.append(status)
        if due_before is not None:
            due_before = _number(due_before)
            # Both SQLite JSON1 and MySQL expose JSON_EXTRACT. An explicit
            # numeric cast avoids MySQL JSON type-order comparisons. Filter
            # before LIMIT so recent future plans cannot hide an old due plan.
            due = "CAST(JSON_EXTRACT(payload,'$.due_at') AS DECIMAL(30,10))"
            clauses.extend((due+">0", due+"<=?"))
            values.append(due_before)
        where = " WHERE "+" AND ".join(clauses) if clauses else ""
        direction = "ASC" if oldest else "DESC"
        return [self._unpack(row) for row in self._read(
            "SELECT * FROM companion_plans"+where+" ORDER BY updated "+direction+",id LIMIT ?", (*values, limit))]

    def _reading_save(self, value):
        payload = {key: value[key] for key in ("title", "source_refs", "binding_refs", "pause_reason", "last_error")}
        self.con.execute("UPDATE companion_readings SET status=?,cursor_pos=?,revision=?,lease_token=?,"
                         "lease_until=?,payload=?,updated=? WHERE id=?",
                         (value["status"], value["cursor"], value["revision"], value["lease_token"],
                          value["lease_until"], _json(payload), value["updated"], value["id"]))

    def open_reading(self, provider, resource_type, resource_id, version, *, owner_id, scope,
                     total_units, source_refs, title="", now=None):
        if (not isinstance(provider, str) or provider not in {"cap", "pixiv"}
                or not isinstance(resource_type, str) or resource_type not in {"pdf", "novel", "illust"}):
            raise ValueError("invalid_resource")
        owner_id, scope = _owner(owner_id), _text(scope, 100)
        resource_id, version = _text(resource_id), _text(version)
        total = 0 if total_units is None else _integer(total_units, 1)
        sources, title, timestamp = _refs(source_refs), _text(title, 240, empty=True), _now(now)
        # A withdrawn and re-observed source has a new evidence version even if
        # the document bytes match. Start new progress; never rebind old notes.
        identity = "reading:"+_hash(owner_id, provider, resource_type, resource_id, version, sources)
        with self.con:
            self._lock()
            existing = self.con.execute("SELECT * FROM companion_readings WHERE id=?", (identity,)).fetchone()
            if existing is not None:
                current = self._unpack(existing)
                if current["total_units"] != total_units or current["binding_refs"] != sources:
                    raise ValueError("reading_binding_conflict")
                # Scope records the original provenance; changing windows does
                # not fork a reading or make its earlier private sources public.
                return current
            payload = _json({"title": title, "source_refs": sources, "binding_refs": sources,
                             "pause_reason": "", "last_error": ""})
            self.con.execute("INSERT INTO companion_readings(id,owner_id,scope,provider,resource_type,resource_id,"
                             "version,status,total_units,revision,payload,created,updated) "
                             "VALUES (?,?,?,?,?,?,?,'active',?,1,?,?,?)",
                             (identity, owner_id, scope, provider, resource_type, resource_id, version,
                              total, payload, timestamp, timestamp))
            return self._row("companion_readings", identity)

    @staticmethod
    def _authorize(row, owner_id, expected_revision=None):
        if row["owner_id"] != _owner(owner_id):
            raise ValueError("owner_mismatch")
        if expected_revision is not None and row["revision"] != _integer(expected_revision, 1):
            raise ValueError("revision_conflict")

    def claim_reading(self, id, *, owner_id, expected_revision=None, lease_seconds=120, now=None):
        timestamp = _now(now)
        _integer(lease_seconds, 1, 600)
        with self.con:
            self._lock()
            current = self._row("companion_readings", _text(id))
            self._authorize(current, owner_id, expected_revision)
            if current["status"] != "active" or current["lease_until"] > timestamp:
                return None
            current.update(lease_token=uuid.uuid4().hex, lease_until=timestamp+lease_seconds,
                           revision=current["revision"]+1, updated=timestamp)
            self._reading_save(current)
            return {**current, "token": current["lease_token"]}

    def release_reading(self, id, token, *, owner_id, reason="read_failed", now=None):
        timestamp, token = _now(now), _text(token)
        if not isinstance(reason, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", reason):
            raise ValueError("invalid_reason")
        with self.con:
            self._lock()
            current = self._row("companion_readings", _text(id))
            self._authorize(current, owner_id)
            if current["lease_token"] != token:
                raise ValueError("reading_lease_lost")
            current.update(lease_token="", lease_until=0, last_error=reason,
                           revision=current["revision"]+1, updated=timestamp)
            self._reading_save(current)
            return current

    def complete_unit(self, id, token, *, owner_id, expected_revision, version, index, unit_id,
                      source_refs, excerpt="", image_sha256="", note="", note_status="pending", now=None):
        timestamp, index = _now(now), _integer(index)
        expected_revision = _integer(expected_revision, 1)
        token, unit_id, version = _text(token), _text(unit_id), _text(version)
        sources = _refs(source_refs)
        if (not isinstance(excerpt, str) or len(excerpt) > 12000 or "\0" in excerpt
                or any(ord(c) < 32 and c not in "\n\r\t" for c in excerpt)):
            raise ValueError("invalid_excerpt")
        if not isinstance(image_sha256, str):
            raise ValueError("invalid_digest")
        if image_sha256:
            _digest(image_sha256)
        if not excerpt and not image_sha256:
            raise ValueError("empty_reading_unit")
        if not isinstance(note_status, str) or note_status not in {"pending", "reviewed"}:
            raise ValueError("invalid_note_status")
        note = _note(note)
        if note["quotes"] and (not excerpt or any(quote not in excerpt for quote in note["quotes"])):
            raise ValueError("ungrounded_quote")
        payload = {"unit_id": unit_id, "excerpt": excerpt, "image_sha256": image_sha256,
                   "content_sha256": hashlib.sha256(excerpt.encode("utf-8")).hexdigest() if excerpt else image_sha256,
                   "note": note, "note_status": note_status, "source_refs": sources, "token_digest": _hash(token)}
        payload["submission_hash"] = _hash(payload)
        identity = "reading-unit:"+_hash(id, index)
        with self.con:
            self._lock()
            current = self._row("companion_readings", _text(id))
            self._authorize(current, owner_id)
            if current["version"] != version:
                raise ValueError("source_version_changed")
            old = self.con.execute("SELECT * FROM companion_units WHERE id=?", (identity,)).fetchone()
            if old is not None:
                if json.loads(old["payload"])["submission_hash"] != payload["submission_hash"]:
                    raise ValueError("unit_conflict")
                return current
            self._authorize(current, owner_id, expected_revision)
            if current["status"] != "active" or current["lease_token"] != token or current["lease_until"] <= timestamp:
                raise ValueError("reading_lease_lost")
            if current["cursor"] != index:
                raise ValueError("noncontiguous_reading")
            if current["resource_type"] == "novel":
                if not excerpt or image_sha256:
                    raise ValueError("invalid_novel_unit")
                next_cursor = index + len(excerpt)
            else:
                if current["resource_type"] == "illust" and not image_sha256:
                    raise ValueError("invalid_image_unit")
                next_cursor = index + 1
            if current["total_units"] is not None and next_cursor > current["total_units"]:
                raise ValueError("reading_out_of_bounds")
            lookup = {ref["id"]: ref for ref in sources}
            if any(lookup.get(ref["id"]) != ref for ref in current["source_refs"]):
                raise ValueError("source_reference_changed")
            # The resource-version roots stay fixed. Per-page references live
            # on each unit, so a long book cannot exhaust a growing root list.
            self.con.execute("INSERT INTO companion_units(id,reading_id,unit_index,next_cursor,payload,created) "
                             "VALUES (?,?,?,?,?,?)", (identity, id, index, next_cursor, _json(payload), timestamp))
            current.update(cursor=next_cursor, revision=current["revision"]+1, lease_token="", lease_until=0,
                           status="completed" if next_cursor == current["total_units"] else "active", last_error="", updated=timestamp)
            self._reading_save(current)
            return current

    def check_reading(self, id, *, owner_id, version, current_refs, available=True, now=None):
        if type(available) is not bool:
            raise ValueError("invalid_availability")
        version, refs, timestamp = _text(version), _refs(current_refs, empty=True), _now(now)
        lookup = {ref["id"]: ref for ref in refs}
        with self.con:
            self._lock()
            current = self._row("companion_readings", _text(id))
            self._authorize(current, owner_id)
            reason = ("source_unavailable" if not available else "source_version_changed" if version != current["version"]
                      else "source_reference_changed" if any(lookup.get(ref["id"]) != ref for ref in current["source_refs"]) else "")
            if reason and (current["status"] != "paused" or current["pause_reason"] != reason):
                current.update(status="paused", pause_reason=reason, lease_token="", lease_until=0,
                               revision=current["revision"]+1, updated=timestamp)
                self._reading_save(current)
            return current

    def resume_reading(self, id, *, owner_id, expected_revision, version, current_refs, now=None):
        expected_revision = _integer(expected_revision, 1)
        timestamp, version, refs = _now(now), _text(version), _refs(current_refs)
        lookup = {ref["id"]: ref for ref in refs}
        with self.con:
            self._lock()
            current = self._row("companion_readings", _text(id))
            self._authorize(current, owner_id, expected_revision)
            if version != current["version"] or any(lookup.get(ref["id"]) != ref for ref in current["source_refs"]):
                raise ValueError("source_reference_changed")
            if current["status"] != "paused":
                return current
            current.update(status="completed" if current["cursor"] == current["total_units"] else "active",
                           pause_reason="", revision=current["revision"]+1, updated=timestamp)
            self._reading_save(current)
            return current

    def units(self, id, *, owner_id=None, offset=0, limit=50):
        _integer(offset)
        _integer(limit, 1, 200)
        if owner_id is not None:
            self.reading(id, owner_id=owner_id)
        return [self._unpack(row) for row in self._read(
            "SELECT * FROM companion_units WHERE reading_id=? ORDER BY unit_index LIMIT ? OFFSET ?",
            (_text(id), limit, offset))]

    def notes(self, id, *, owner_id=None, current_refs, limit=50):
        """Only reviewed derived notes with all current dependencies may be used."""
        refs = {ref["id"]: ref for ref in _refs(current_refs, empty=True)}
        reading = self.reading(id, owner_id=owner_id)
        if (reading is None or reading["status"] == "paused"
                or any(refs.get(ref["id"]) != ref for ref in reading["source_refs"])):
            return []
        return [{"id": "reading-note:"+unit["id"].split(":", 1)[1], "source": "reading_note",
                 "evidence_status": "inferred", "note": unit["note"], "source_refs": unit["source_refs"],
                 "reading_id": id, "unit_index": unit["unit_index"]}
                for unit in self.units(id, limit=limit) if unit["note_status"] == "reviewed"
                and all(refs.get(ref["id"]) == ref for ref in unit["source_refs"])]

    def review_unit(self, id, unit_id, *, owner_id, expected_revision, note,
                    note_status="pending", current_refs, now=None):
        expected_revision = _integer(expected_revision, 1)
        timestamp, note, unit_id = _now(now), _note(note), _text(unit_id)
        refs = {ref["id"]: ref for ref in _refs(current_refs)}
        if not isinstance(note_status, str) or note_status not in {"pending", "reviewed"}:
            raise ValueError("invalid_note_status")
        with self.con:
            self._lock()
            current = self._row("companion_readings", _text(id))
            self._authorize(current, owner_id, expected_revision)
            if current["status"] == "paused" or any(refs.get(ref["id"]) != ref for ref in current["source_refs"]):
                raise ValueError("source_reference_changed")
            # Both the database unit ID and the adapter's canonical unit ID are
            # accepted; neither permits replacing the stored original content.
            rows = self.con.execute("SELECT * FROM companion_units WHERE reading_id=?", (id,)).fetchall()
            row = next((row for row in rows if row["id"] == unit_id
                        or json.loads(row["payload"])["unit_id"] == unit_id), None)
            if row is None:
                raise ValueError("unit_missing")
            payload = json.loads(row["payload"])
            if any(refs.get(ref["id"]) != ref for ref in payload["source_refs"]):
                raise ValueError("source_reference_changed")
            if note["quotes"] and (not payload["excerpt"] or any(quote not in payload["excerpt"] for quote in note["quotes"])):
                raise ValueError("ungrounded_quote")
            payload.update(note=note, note_status=note_status)
            self.con.execute("UPDATE companion_units SET payload=? WHERE id=?", (_json(payload), row["id"]))
            current.update(revision=current["revision"]+1, updated=timestamp)
            self._reading_save(current)
            return current

    def _audit_request(self, owner_id, scope, message_id, summary_hash, command):
        message = _text(message_id, 512)
        digest = _hash(owner_id, scope, message)
        return "companion-command:"+digest, digest, _digest(summary_hash), _hash(command)

    def _replayed(self, identity, request_hash):
        old = self.con.execute("SELECT * FROM companion_audit WHERE id=?", (identity,)).fetchone()
        if old is None:
            return None
        data = json.loads(old["payload"])
        if data["request_hash"] != request_hash:
            raise ValueError("idempotency_conflict")
        return self._row("companion_plans", old["entity_id"])

    def _audit(self, identity, message_digest, summary_hash, request_hash, before, after, action, timestamp):
        payload = _json({"request_hash": request_hash, "before": before, "after": after})
        self.con.execute("INSERT INTO companion_audit(id,entity_id,owner_id,action,message_digest,summary_hash,payload,created) "
                         "VALUES (?,?,?,?,?,?,?,?)",
                         (identity, after["id"], after["owner_id"], action, message_digest, summary_hash, payload, timestamp))

    def propose_plan(self, *, owner_id, scope, title, message_id, summary_hash,
                     next_step="", due_at=0, now=None):
        owner_id, scope, timestamp = _owner(owner_id), _text(scope, 100), _now(now)
        details = {"title": _text(title, 240), "next_step": _text(next_step, 600, empty=True),
                   "due_at": _number(due_at), "last_followup_at": 0, "followup_count": 0}
        audit, message, summary, request = self._audit_request(
            owner_id, scope, message_id, summary_hash, {"action": "propose", "details": details, "summary_hash": summary_hash})
        identity = "plan:"+_hash(owner_id, scope, message)
        with self.con:
            self._lock()
            old = self._replayed(audit, request)
            if old is not None:
                return old
            self.con.execute("INSERT INTO companion_plans(id,owner_id,scope,status,revision,payload,created,updated) "
                             "VALUES (?,?,?,'proposed',1,?,?,?)",
                             (identity, owner_id, scope, _json(details), timestamp, timestamp))
            after = self._row("companion_plans", identity)
            self._audit(audit, message, summary, request, None, after, "propose", timestamp)
            return after

    def command_plan(self, id, *, owner_id, action, expected_revision, message_id, summary_hash,
                     title=None, next_step=None, due_at=None, now=None):
        expected_revision = _integer(expected_revision, 1)
        owner_id, timestamp, id = _owner(owner_id), _now(now), _text(id)
        if not isinstance(action, str) or action not in {"accept", "pause", "resume", "complete", "cancel", "edit"}:
            raise ValueError("invalid_action")
        changes = {}
        if title is not None:
            changes["title"] = _text(title, 240)
        if next_step is not None:
            changes["next_step"] = _text(next_step, 600, empty=True)
        if due_at is not None:
            changes["due_at"] = _number(due_at)
        if (action != "edit" and changes) or (action == "edit" and not changes):
            raise ValueError("invalid_plan_changes")
        with self.con:
            self._lock()
            before = self._row("companion_plans", id)
            self._authorize(before, owner_id)
            audit, message, summary, request = self._audit_request(
                owner_id, before["scope"], message_id, summary_hash,
                {"id": id, "action": action, "changes": changes, "summary_hash": summary_hash})
            old = self._replayed(audit, request)
            if old is not None:
                return old
            self._authorize(before, owner_id, expected_revision)
            transitions = {"accept": ({"proposed"}, "active"), "pause": ({"active"}, "paused"),
                           "resume": ({"paused"}, "active"), "complete": ({"active", "paused"}, "completed"),
                           "cancel": ({"proposed", "active", "paused"}, "cancelled"),
                           "edit": ({"proposed", "active", "paused"}, before["status"])}
            allowed, status = transitions[action]
            if before["status"] not in allowed:
                raise ValueError("invalid_plan_transition")
            after = {**before, **changes, "status": status, "revision": before["revision"]+1, "updated": timestamp}
            payload = _json({key: after[key] for key in ("title", "next_step", "due_at", "last_followup_at", "followup_count")})
            self.con.execute("UPDATE companion_plans SET status=?,revision=?,payload=?,updated=? WHERE id=?",
                             (status, after["revision"], payload, timestamp, id))
            self._audit(audit, message, summary, request, before, after, action, timestamp)
            return after

    def claim_plan_followup(self, id, *, owner_id, expected_revision, interval_seconds=86400, now=None):
        expected_revision = _integer(expected_revision, 1)
        timestamp = _now(now)
        _integer(interval_seconds, 1, 30*86400)
        with self.con:
            self._lock()
            current = self._row("companion_plans", _text(id))
            self._authorize(current, owner_id, expected_revision)
            if (current["status"] != "active" or current["due_at"] > timestamp
                    or (current["followup_count"] and current["last_followup_at"]+interval_seconds > timestamp)):
                return False
            payload = {key: current[key] for key in ("title", "next_step", "due_at", "last_followup_at", "followup_count")}
            payload.update(last_followup_at=timestamp, followup_count=current["followup_count"]+1)
            # A delivery attempt has a separate cursor; it is not owner consent
            # to change the plan's status, contents or command revision.
            self.con.execute("UPDATE companion_plans SET payload=?,updated=? WHERE id=?", (_json(payload), timestamp, id))
            return True

    def history(self, entity_id, limit=20, *, owner_id=None):
        _integer(limit, 1, 200)
        if owner_id is not None:
            self.plan(entity_id, owner_id=owner_id)
        return [self._unpack(row) for row in self._read(
            "SELECT * FROM companion_audit WHERE entity_id=? ORDER BY created DESC,id LIMIT ?", (_text(entity_id), limit))]

    def status(self):
        def counts(table):
            return {row["status"]: int(row["n"]) for row in self._read(
                "SELECT status,COUNT(*) AS n FROM "+table+" GROUP BY status")}
        control = self._read("SELECT budget_day,daily_count,last_cycle FROM companion_control WHERE slot=1")
        return {"readings": counts("companion_readings"), "plans": counts("companion_plans"),
                "reading_budget": control[0] if control else {"budget_day": 0, "daily_count": 0, "last_cycle": 0},
                "units": int(self._read("SELECT COUNT(*) AS n FROM companion_units")[0]["n"])}
