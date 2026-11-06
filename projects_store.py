"""Finite, source-bound personal projects; records conclusions, never hidden reasoning."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .mysql_compat import MySQLCompatConnection
from .growth_limits import MAX_INTEREST_SOURCES, MAX_RECORDS
from .owner import normalize_qq
from .sql_logging import connect_sqlite

MAX_SEED_SOURCES = MAX_INTEREST_SOURCES + MAX_RECORDS
MAX_PROJECT_SOURCES = MAX_SEED_SOURCES + MAX_RECORDS


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _now(value=None):
    value = time.time() if value is None else value
    if type(value) not in (int, float) or not 0 <= value <= 1e12 or not math.isfinite(value):
        raise ValueError("invalid_time")
    return float(value)


def _int(value, low=0, high=1000000):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("invalid_integer")
    return value


def _text(value, maximum=200):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError("invalid_text")
    return value.strip()


def _owner(value):
    if not isinstance(value, str) or not value or normalize_qq(value) != value:
        raise ValueError("invalid_owner")
    return value


def _hex(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("invalid_digest")
    return value


def references(rows, *, empty=False, limit=MAX_RECORDS):
    if type(limit) is not int or not 1 <= limit <= MAX_PROJECT_SOURCES:
        raise ValueError("invalid_sources")
    if not isinstance(rows, list) or not (0 if empty else 1) <= len(rows) <= limit:
        raise ValueError("invalid_sources")
    result = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"id", "digest", "scope", "sensitivity"}:
            raise ValueError("invalid_sources")
        ident = _text(row["id"], 240)
        from .unified_memory import _source_id
        _source_id(ident)
        if not isinstance(row["sensitivity"], str) or row["sensitivity"] not in {"public", "personal", "unknown", "confidential"}:
            raise ValueError("invalid_sources")
        checked = {"id": ident, "digest": _hex(row["digest"]), "scope": _text(row["scope"], 100), "sensitivity": row["sensitivity"]}
        if ident in result and result[ident] != checked:
            raise ValueError("source_conflict")
        result[ident] = checked
    return sorted(result.values(), key=lambda row: row["id"])


def seed_value(seed):
    fields = {"question_key", "interest_key", "topic", "question", "seed_refs", "source_signature"}
    if not isinstance(seed, dict) or set(seed) not in (fields, fields | {"interest_refs"}):
        raise ValueError("invalid_seed")
    if not re.fullmatch(r"q:[0-9a-f]{24}", str(seed["question_key"])) or not re.fullmatch(r"i:[0-9a-f]{24}", str(seed["interest_key"])):
        raise ValueError("invalid_seed")
    checked = {"question_key": seed["question_key"], "interest_key": seed["interest_key"],
            "topic": _text(seed["topic"], 180), "question": _text(seed["question"], 360),
            "seed_refs": references(seed["seed_refs"], limit=MAX_SEED_SOURCES),
            "source_signature": _hex(seed["source_signature"])}
    if "interest_refs" in seed:
        interest_refs = references(seed["interest_refs"], limit=MAX_INTEREST_SOURCES)
        all_refs = {ref["id"]: ref for ref in checked["seed_refs"]}
        if any(all_refs.get(ref["id"]) != ref for ref in interest_refs):
            raise ValueError("invalid_seed")
        checked["interest_refs"] = interest_refs
    return checked


class ProjectsStore:
    def __init__(self, path: Path, *, logger=None, sql_debug=False, mysql_settings=None):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = (MySQLCompatConnection(**mysql_settings, logger=logger, sql_debug=sql_debug)
                    if mysql_settings is not None else connect_sqlite(str(path), timeout=10, logger=logger, sql_debug=sql_debug))
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA busy_timeout=10000")
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.executescript("""
        CREATE TABLE IF NOT EXISTS projects_control (
          owner_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0,
          budget_day REAL NOT NULL DEFAULT 0, daily_actions INTEGER NOT NULL DEFAULT 0,
          last_action REAL NOT NULL DEFAULT 0, lease_token TEXT NOT NULL DEFAULT '',
          lease_until REAL NOT NULL DEFAULT 0, updated REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS projects_items (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, status TEXT NOT NULL,
          stage TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
          created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_projects_items_owner ON projects_items(owner_id,status,created);
        CREATE TABLE IF NOT EXISTS projects_audit (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, item_id TEXT NOT NULL,
          action TEXT NOT NULL, body_digest TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_projects_audit_item ON projects_audit(item_id,created);
        """)
        self.con.commit()

    def close(self):
        self.con.close()

    def _read(self, sql, parameters=()):
        if isinstance(self.con, MySQLCompatConnection):
            return [dict(row) for row in self.con.fetch_readonly(sql, parameters)]
        cursor = self.con.execute(sql, parameters)
        try:
            return [dict(row) for row in cursor.fetchall()]
        finally:
            cursor.close()

    def _lock(self, owner, now):
        self.con.execute("INSERT INTO projects_control(owner_id,updated) VALUES (?,?) "
                         "ON CONFLICT(owner_id) DO UPDATE SET revision=revision", (owner, now))
        return dict(self.con.execute("SELECT * FROM projects_control WHERE owner_id=?", (owner,)).fetchone())

    def state(self, owner):
        rows = self._read("SELECT * FROM projects_control WHERE owner_id=?", (_owner(owner),))
        return rows[0] if rows else {"owner_id": owner, "revision": 0, "budget_day": 0, "daily_actions": 0,
                                    "last_action": 0, "lease_token": "", "lease_until": 0, "updated": 0}

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        row = dict(row)
        row.update(json.loads(row.pop("payload")))
        return row

    def _get(self, owner, ident):
        return self._decode(self.con.execute("SELECT * FROM projects_items WHERE id=? AND owner_id=?", (ident, owner)).fetchone())

    def get(self, owner, ident):
        rows = self._read("SELECT * FROM projects_items WHERE id=? AND owner_id=?", (_text(ident, 100), _owner(owner)))
        return self._decode(rows[0]) if rows else None

    def items(self, owner):
        return [self._decode(row) for row in self._read("SELECT * FROM projects_items WHERE owner_id=? ORDER BY created,id LIMIT 32", (_owner(owner),))]

    def _save(self, row, now, *, new=False):
        keys = {"id", "owner_id", "status", "stage", "revision", "created", "updated"}
        payload = json.dumps({k: v for k, v in row.items() if k not in keys}, ensure_ascii=False, sort_keys=True, allow_nan=False)
        if new:
            self.con.execute("INSERT INTO projects_items(id,owner_id,status,stage,revision,payload,created,updated) VALUES (?,?,?,?,?,?,?,?)",
                             (row["id"], row["owner_id"], row["status"], row["stage"], row["revision"], payload, row["created"], now))
        else:
            self.con.execute("UPDATE projects_items SET status=?,stage=?,revision=?,payload=?,updated=? WHERE id=? AND owner_id=?",
                             (row["status"], row["stage"], row["revision"], payload, now, row["id"], row["owner_id"]))
        row["updated"] = now
        self.con.execute("UPDATE projects_control SET revision=revision+1,updated=? WHERE owner_id=?", (now, row["owner_id"]))

    def _audit(self, row, action, now, *, receipt="", body_digest=""):
        ident = receipt or digest([row["id"], row["revision"], action])
        payload = json.dumps({"revision": row["revision"], "status": row["status"], "stage": row["stage"],
                              "reason": row["reason"], "state_digest": digest(row)}, sort_keys=True)
        self.con.execute("INSERT INTO projects_audit(id,owner_id,item_id,action,body_digest,payload,created) VALUES (?,?,?,?,?,?,?)",
                         (ident, row["owner_id"], row["id"], action, body_digest, payload, now))

    def create(self, owner, seed, *, max_active=2, now=None):
        owner, now, seed = _owner(owner), _now(now), seed_value(seed)
        _int(max_active, 1, 3)
        ident = "projects:item:"+digest([owner, seed["question_key"]])
        with self.con:
            self._lock(owner, now)
            old = self._get(owner, ident)
            if old:
                return old  # Terminal/stopped identities never auto-respawn.
            rows = self.con.execute("SELECT status FROM projects_items WHERE owner_id=?", (owner,)).fetchall()
            if len(rows) >= 32 or sum(row["status"] == "active" for row in rows) >= max_active:
                return None
            row = {"id": ident, "owner_id": owner, "status": "active", "stage": "gather", "revision": 1,
                   "seed": seed, "evidence_refs": [], "result": {}, "reason": "", "attempts": 0, "failures": 0,
                   "next_action": now, "deadline": now+14*86400, "lease_token": "", "lease_until": 0, "created": now, "updated": now}
            self._save(row, now, new=True)
            self._audit(row, "selected", now)
            return row

    def claim(self, owner, eligible_ids, *, day_start, daily_limit=6, interval_seconds=900, lease_seconds=180, now=None):
        owner, now, day = _owner(owner), _now(now), _now(day_start)
        _int(daily_limit, 0, 24), _int(interval_seconds, 300, 86400), _int(lease_seconds, 1, 3600)
        if not isinstance(eligible_ids, (list, tuple)) or len(eligible_ids) > 32 or any(not isinstance(i, str) for i in eligible_ids):
            raise ValueError("invalid_candidates")
        if not day <= now < day+90000:
            raise ValueError("invalid_day")
        with self.con:
            control = self._lock(owner, now)
            if (control["lease_until"] > now or not daily_limit or day < control["budget_day"]
                    or control["daily_actions"] and control["last_action"]+interval_seconds > now
                    or control["budget_day"] == day and control["daily_actions"] >= daily_limit):
                return None
            rows = [self._decode(row) for row in self.con.execute("SELECT * FROM projects_items WHERE owner_id=? AND status='active'", (owner,)).fetchall()]
            candidates = [row for row in rows if row["id"] in eligible_ids and row["next_action"] <= now and row["lease_until"] <= now]
            if not candidates:
                return None
            row = min(candidates, key=lambda r: (r["next_action"], r["created"], r["id"]))
            if row["deadline"] <= now or row["attempts"] >= 12:
                row.update(status="unresolved", reason="project_limit", revision=row["revision"]+1, lease_token="", lease_until=0)
                self._save(row, now)
                self._audit(row, "bounded_stop", now)
                return None
            token = uuid.uuid4().hex
            count = control["daily_actions"]+1 if day == control["budget_day"] else 1
            self.con.execute("UPDATE projects_control SET budget_day=?,daily_actions=?,last_action=?,lease_token=?,lease_until=?,updated=? WHERE owner_id=?",
                             (day, count, now, token, now+lease_seconds, now, owner))
            row.update(lease_token=token, lease_until=now+lease_seconds, attempts=row["attempts"]+1, revision=row["revision"]+1)
            self._save(row, now)
            self._audit(row, "started", now)
            return row

    def valid(self, owner, ident, token, revision, *, now=None):
        now = _now(now)
        row, control = self.get(owner, ident), self.state(owner)
        return bool(row and row["status"] == "active" and row["revision"] == revision
                    and row["lease_token"] == token == control["lease_token"] and row["lease_until"] > now and control["lease_until"] > now)

    def finish(self, owner, ident, token, revision, *, action, evidence_refs=None, result=None, now=None, adaptive=False):
        owner, now = _owner(owner), _now(now)
        _int(revision, 1)
        if type(adaptive) is not bool:
            raise ValueError("invalid_adaptive_plan")
        if action not in {"gathered", "no_evidence", "reviewed", "failed", "deferred", "interrupted"}:
            raise ValueError("invalid_action")
        if action == "gathered":
            evidence_refs = references(evidence_refs)
        if action == "reviewed":
            from .projects_model import validate_result
            result = validate_result(result)
            if result is None:
                raise ValueError("invalid_result")
        with self.con:
            control = self._lock(owner, now)
            row = self._get(owner, ident)
            if (not row or row["status"] != "active" or row["revision"] != revision or row["lease_token"] != token
                    or control["lease_token"] != token or min(row["lease_until"], control["lease_until"]) <= now):
                return False
            if action == "gathered" and row["stage"] != "gather" or action == "reviewed" and row["stage"] != "review":
                raise ValueError("stage_conflict")
            if action == "reviewed" and any(q["source_id"] not in {r["id"] for r in row["evidence_refs"]} for q in result["quotes"]):
                raise ValueError("result_source_mismatch")
            if action == "gathered":
                if row.get("plan"):
                    combined = {ref["id"]: ref for ref in row["evidence_refs"]}
                    for ref in evidence_refs:
                        if ref["id"] in combined and ref != combined[ref["id"]]:
                            raise ValueError("source_conflict")
                        combined[ref["id"]] = ref
                    if len(combined) == len(row["evidence_refs"]):
                        raise ValueError("no_new_evidence")
                    row["plan"]["latest_refs"] = evidence_refs
                    evidence_refs = references(list(combined.values()))
                row.update(stage="review", evidence_refs=evidence_refs, failures=0, reason="")
            elif action == "reviewed":
                row.update(status="completed" if result["outcome"] == "supported" else "unresolved", result=result,
                           stage="finished", reason="reviewed_material_only", failures=0)
                if adaptive or row.get("plan"):
                    plan = row.setdefault("plan", {"rounds": [], "pending_question": row["seed"]["question"],
                                                   "evidence_needed": "", "latest_refs": row["evidence_refs"]})
                    plan["rounds"].append({"question": plan["pending_question"], "result": result,
                                           "sources": list(row["evidence_refs"]), "completed_at": now})
                    step = result.get("next_step")
                    repeated = step and any(part["question"] == step["question"] for part in plan["rounds"])
                    if (adaptive and step and not repeated and len(plan["rounds"]) < 3
                            and len(row["evidence_refs"]) < MAX_RECORDS):
                        plan.update(pending_question=step["question"], evidence_needed=step["evidence_needed"])
                        row.update(status="active", stage="gather", reason="more_evidence_needed")
                    elif result["outcome"] == "insufficient":
                        row["reason"] = "plan_needs_help"
            elif action in {"failed", "no_evidence"}:
                row.update(failures=row["failures"]+1, reason=action)
                if row["failures"] >= 3:
                    row["status"] = "unresolved" if action == "no_evidence" else "paused"
            else:
                row["reason"] = action
            row.update(revision=row["revision"]+1, lease_token="", lease_until=0,
                       next_action=now+min(3600, 300*(2**row["failures"])))
            self._save(row, now)
            self._audit(row, action, now)
            self.con.execute("UPDATE projects_control SET lease_token='',lease_until=0 WHERE owner_id=? AND lease_token=?", (owner, token))
            return True

    def pause_invalid(self, owner, ident, revision, *, now=None):
        owner, now = _owner(owner), _now(now)
        with self.con:
            self._lock(owner, now)
            row = self._get(owner, ident)
            if not row or row["revision"] != revision or row["status"] != "active":
                return False
            token = row["lease_token"]
            row.update(status="paused", reason="source_changed", lease_token="", lease_until=0, revision=revision+1)
            self._save(row, now)
            self._audit(row, "source_changed", now)
            self.con.execute("UPDATE projects_control SET lease_token='',lease_until=0 WHERE owner_id=? AND lease_token=?", (owner, token))
            return True

    def command(self, owner, ident, revision, action, receipt_id, body_digest, *, max_active=2, now=None):
        owner, now = _owner(owner), _now(now)
        _int(revision, 1), _hex(receipt_id), _hex(body_digest), _int(max_active, 1, 3)
        if action not in {"pause", "resume", "stop"}:
            raise ValueError("invalid_command")
        with self.con:
            self._lock(owner, now)
            prior = self.con.execute("SELECT body_digest FROM projects_audit WHERE id=?", (receipt_id,)).fetchone()
            if prior:
                if prior["body_digest"] != body_digest:
                    raise ValueError("receipt_conflict")
                return False
            row = self._get(owner, ident)
            if not row or row["revision"] != revision or row["status"] in {"completed", "stopped", "unresolved"}:
                return False
            if action == "resume":
                count = self.con.execute("SELECT COUNT(*) AS n FROM projects_items WHERE owner_id=? AND status='active'", (owner,)).fetchone()["n"]
                if row["status"] != "paused" or count >= max_active or row["deadline"] <= now or row["attempts"] >= 12:
                    return False
            elif action == "pause" and row["status"] != "active":
                return False
            token = row["lease_token"]
            row.update(status={"pause": "paused", "resume": "active", "stop": "stopped"}[action], reason="owner_"+action,
                       revision=revision+1, lease_token="", lease_until=0, next_action=now)
            self._save(row, now)
            self._audit(row, action, now, receipt=receipt_id, body_digest=body_digest)
            self.con.execute("UPDATE projects_control SET lease_token='',lease_until=0 WHERE owner_id=? AND lease_token=?", (owner, token))
            return True
