"""Owner-authored relationship preferences and memories, with durable receipts.

Only the trusted private-message handler calls these writes after exact command
parsing. No relationship scores, absence penalties, model-written promises or
automatic permissions are represented in this ledger.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import math
import re
import sqlite3
import time
import unicodedata
from pathlib import Path

from .mysql_compat import MySQLCompatConnection
from .owner import extract_sender, normalize_qq
from .sql_logging import connect_sqlite


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(*values):
    return hashlib.sha256(_json(values).encode("utf-8")).hexdigest()


def _text(value, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError("invalid_text")
    value = unicodedata.normalize("NFKC", value).strip()
    if not value or len(value) > maximum:
        raise ValueError("invalid_text")
    return value


def _owner(value):
    if not isinstance(value, str) or not value or normalize_qq(value) != value:
        raise ValueError("invalid_owner")
    return value


def _address_key(value):
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def _integer(value, minimum=0, maximum=100000000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("invalid_integer")
    return value


def _now(value):
    value = time.time() if value is None else value
    if type(value) not in (int, float):
        raise ValueError("invalid_time")
    try:
        if not math.isfinite(value) or not 0 <= value <= 1e12:
            raise ValueError("invalid_time")
    except OverflowError:
        raise ValueError("invalid_time") from None
    return float(value)


def _date(value, required=False):
    if value == "" and not required:
        return ""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("invalid_date")
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        raise ValueError("invalid_date") from None
    return value


def _defaults(owner_id):
    return {"owner_id": owner_id, "revision": 0, "preferred_address": "", "blocked_addresses": [],
            "affection": "default", "boundaries": [], "created": 0, "updated": 0}


class BondStore:
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
        CREATE TABLE IF NOT EXISTS bond_profiles (
          owner_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0,
          payload TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bond_moments (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, scope TEXT NOT NULL,
          kind TEXT NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL,
          payload TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_bond_moments_owner ON bond_moments(owner_id,status,created);
        CREATE TABLE IF NOT EXISTS bond_audit (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, entity_id TEXT NOT NULL,
          action TEXT NOT NULL, message_digest TEXT NOT NULL, body_digest TEXT NOT NULL,
          payload TEXT NOT NULL, created REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_bond_audit_owner ON bond_audit(owner_id,created);
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

    def profile(self, owner_id):
        owner_id = _owner(owner_id)
        rows = self._read("SELECT * FROM bond_profiles WHERE owner_id=?", (owner_id,))
        return self._decode(rows[0]) if rows else _defaults(owner_id)

    @staticmethod
    def _payload(profile):
        return {key: profile[key] for key in ("preferred_address", "blocked_addresses", "affection", "boundaries")}

    def _lock(self, owner_id, now):
        # Single upsert acquires an InnoDB exclusive owner-row lock without an
        # INSERT IGNORE shared-lock upgrade, before any transaction snapshot.
        self.con.execute("INSERT INTO bond_profiles(owner_id,payload,created,updated) VALUES (?,?,?,?) "
                         "ON CONFLICT(owner_id) DO UPDATE SET revision=revision",
                         (owner_id, _json(self._payload(_defaults(owner_id))), now, now))
        return self._decode(self.con.execute("SELECT * FROM bond_profiles WHERE owner_id=?", (owner_id,)).fetchone())

    @staticmethod
    def _receipt(message, owner_id):
        sender = extract_sender(message)
        if (not sender or sender.platform != "qq" or sender.kind != "private" or sender.user_id != owner_id
                or sender.target_id != owner_id or not sender.stream_id or not sender.message_id
                or str(message.get("account_id") or "") == owner_id):
            raise ValueError("trusted_owner_private_message_required")
        stream, mid = _text(sender.stream_id, 256), _text(sender.message_id, 256)
        body = message.get("processed_plain_text")
        if not isinstance(body, str) or not body.strip() or len(body) > 10000 or "\0" in body:
            raise ValueError("trusted_owner_text_required")
        message_digest = _hash("qq", owner_id, stream, mid)
        return "bond-command:"+message_digest, message_digest, hashlib.sha256(body.encode("utf-8")).hexdigest()

    def _replay(self, receipt, owner_id, request):
        row = self.con.execute("SELECT * FROM bond_audit WHERE id=?", (receipt[0],)).fetchone()
        if row is None:
            return None
        if row["owner_id"] != owner_id or row["body_digest"] != receipt[2] or json.loads(row["payload"])["request_hash"] != request:
            raise ValueError("idempotency_conflict")
        if row["entity_id"] == "profile:"+owner_id:
            return self._decode(self.con.execute("SELECT * FROM bond_profiles WHERE owner_id=?", (owner_id,)).fetchone())
        return self._decode(self.con.execute("SELECT * FROM bond_moments WHERE id=? AND owner_id=?",
                                            (row["entity_id"], owner_id)).fetchone())

    @staticmethod
    def _revision(row, expected):
        if expected is not None and row["revision"] != _integer(expected):
            raise ValueError("revision_conflict")

    def _save_profile(self, profile, now):
        profile = {**profile, "revision": profile["revision"]+1, "updated": now}
        self.con.execute("UPDATE bond_profiles SET revision=?,payload=?,updated=? WHERE owner_id=?",
                         (profile["revision"], _json(self._payload(profile)), now, profile["owner_id"]))
        return profile

    def _audit(self, receipt, owner_id, entity_id, action, request, before, after, profile_revision, now):
        self.con.execute("INSERT INTO bond_audit(id,owner_id,entity_id,action,message_digest,body_digest,payload,created) "
                         "VALUES (?,?,?,?,?,?,?,?)", (*receipt[:1], owner_id, entity_id, action, receipt[1], receipt[2],
                         _json({"request_hash": request, "before": before, "after": after, "profile_revision": profile_revision}), now))

    def command(self, message, *, owner_id, action, value=None, expected_revision=None, now=None):
        owner_id, timestamp = _owner(owner_id), _now(now)
        receipt = self._receipt(message, owner_id)
        if not isinstance(action, str) or action not in {
            "set_address", "ban_address", "set_affection", "add_boundary", "remove_boundary"
        }:
            raise ValueError("invalid_action")
        if action in {"set_address", "ban_address"}:
            value = "" if action == "set_address" and value == "" else _text(value, 24)
        elif action == "set_affection":
            if not isinstance(value, str) or value not in {"default", "allow", "avoid"}:
                raise ValueError("invalid_affection")
        elif action == "add_boundary":
            value = _text(value, 120)
        elif not isinstance(value, str) or not re.fullmatch(r"boundary:[0-9a-f]{64}", value):
            raise ValueError("invalid_boundary")
        request = _hash(action, value)
        with self.con:
            before = self._lock(owner_id, timestamp)
            replay = self._replay(receipt, owner_id, request)
            if replay is not None:
                return replay
            self._revision(before, expected_revision)
            after = json.loads(_json(before))
            if action == "set_address":
                after["preferred_address"] = value
                after["blocked_addresses"] = [name for name in after["blocked_addresses"]
                                              if _address_key(name) != _address_key(value)]
            elif action == "ban_address":
                if _address_key(value) not in {_address_key(name) for name in after["blocked_addresses"]}:
                    if len(after["blocked_addresses"]) >= 20:
                        raise ValueError("blocked_address_capacity")
                    after["blocked_addresses"].append(value)
                if _address_key(after["preferred_address"]) == _address_key(value):
                    after["preferred_address"] = ""
            elif action == "set_affection":
                after["affection"] = value
            elif action == "add_boundary":
                key = "boundary:"+_hash(value)
                if not any(item["key"] == key for item in after["boundaries"]):
                    if len(after["boundaries"]) >= 20:
                        raise ValueError("boundary_capacity")
                    after["boundaries"].append({"key": key, "text": value})
            else:
                if not any(item["key"] == value for item in after["boundaries"]):
                    raise ValueError("boundary_missing")
                after["boundaries"] = [item for item in after["boundaries"] if item["key"] != value]
            if self._payload(after) != self._payload(before):
                after = self._save_profile(after, timestamp)
            self._audit(receipt, owner_id, "profile:"+owner_id, action, request, before, after, after["revision"], timestamp)
            return after

    def confirm_moment(self, message, *, owner_id, summary, kind="memory", occurred_on="", source_refs=None,
                       expected_revision=None, now=None):
        owner_id, summary, timestamp = _owner(owner_id), _text(summary, 400), _now(now)
        receipt = self._receipt(message, owner_id)
        if not isinstance(kind, str) or kind not in {"memory", "anniversary"}:
            raise ValueError("invalid_kind")
        occurred_on = _date(occurred_on, required=kind == "anniversary")
        if source_refs is not None and (not isinstance(source_refs, list) or source_refs):
            raise ValueError("owner_authored_memory_only")
        request = _hash("confirm_moment", summary, kind, occurred_on)
        identity = "bond:moment:"+_hash(owner_id, receipt[1])
        with self.con:
            profile = self._lock(owner_id, timestamp)
            replay = self._replay(receipt, owner_id, request)
            if replay is not None:
                return replay
            self._revision(profile, expected_revision)
            count = self.con.execute("SELECT COUNT(*) FROM bond_moments WHERE owner_id=? AND status='active'",
                                     (owner_id,)).fetchone()[0]
            if count >= 200:
                raise ValueError("moment_capacity")
            details = {"summary": summary, "occurred_on": occurred_on, "source_refs": []}
            self.con.execute("INSERT INTO bond_moments(id,owner_id,scope,kind,status,revision,payload,created,updated) "
                             "VALUES (?,?,?,?,'active',1,?,?,?)",
                             (identity, owner_id, "private:"+owner_id, kind, _json(details), timestamp, timestamp))
            after = self._decode(self.con.execute("SELECT * FROM bond_moments WHERE id=?", (identity,)).fetchone())
            profile = self._save_profile(profile, timestamp)
            self._audit(receipt, owner_id, identity, "confirm_moment", request, None, after, profile["revision"], timestamp)
            return after

    def retire_moment(self, message, *, owner_id, moment_id, expected_revision, now=None):
        owner_id, moment_id, timestamp = _owner(owner_id), _text(moment_id, 191), _now(now)
        _integer(expected_revision, 1)
        receipt = self._receipt(message, owner_id)
        request = _hash("retire_moment", moment_id)
        with self.con:
            profile = self._lock(owner_id, timestamp)
            replay = self._replay(receipt, owner_id, request)
            if replay is not None:
                return replay
            row = self.con.execute("SELECT * FROM bond_moments WHERE id=? AND owner_id=?", (moment_id, owner_id)).fetchone()
            if row is None:
                raise ValueError("moment_missing")
            before = self._decode(row)
            self._revision(before, expected_revision)
            if before["status"] != "active":
                raise ValueError("moment_retired")
            after = {**before, "status": "retired", "revision": before["revision"]+1, "updated": timestamp}
            self.con.execute("UPDATE bond_moments SET status='retired',revision=?,updated=? WHERE id=?",
                             (after["revision"], timestamp, moment_id))
            profile = self._save_profile(profile, timestamp)
            self._audit(receipt, owner_id, moment_id, "retire_moment", request, before, after, profile["revision"], timestamp)
            return after

    def moments(self, owner_id, limit=30, *, status="active"):
        owner_id = _owner(owner_id)
        _integer(limit, 1, 200)
        if not isinstance(status, str) or status not in {"active", "retired"}:
            raise ValueError("invalid_status")
        return [self._decode(row) for row in self._read(
            "SELECT * FROM bond_moments WHERE owner_id=? AND status=? ORDER BY created DESC,id LIMIT ?",
            (owner_id, status, limit))]

    def audit(self, owner_id, limit=20):
        owner_id = _owner(owner_id)
        _integer(limit, 1, 200)
        return [self._decode(row) for row in self._read(
            "SELECT * FROM bond_audit WHERE owner_id=? ORDER BY created DESC,id LIMIT ?", (owner_id, limit))]
