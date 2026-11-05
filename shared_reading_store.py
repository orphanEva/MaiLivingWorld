"""Owner-confirmed reading boundaries and verified, actually mentioned units."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

from .companion_store import _integer, _now, _owner, _refs, _text
from .mysql_compat import MySQLCompatConnection
from .owner import extract_sender
from .sql_logging import connect_sqlite


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def command_text(action, reading_id, *, units=0, allow_spoilers=False):
    prefix = {"start": "开始", "position": "看到", "stop": "停止", "clear": "清除",
              "spoilers": "允许剧透" if allow_spoilers else "禁止剧透"}[action]
    return "/共读 "+prefix+" "+reading_id+(" "+str(units) if action == "position" else "")


def parse_command(text):
    if not isinstance(text, str) or len(text) > 200:
        return None
    match = re.fullmatch(r"/共读 (开始|看到|停止|清除|允许剧透|禁止剧透) (reading:[0-9a-f]{64})(?: ([0-9]{1,7}))?", text.strip())
    if not match or (match[1] == "看到") != (match[3] is not None):
        return None
    action = {"开始": "start", "看到": "position", "停止": "stop", "清除": "clear", "允许剧透": "spoilers", "禁止剧透": "spoilers"}[match[1]]
    return {"action": action, "reading_id": match[2], "units": int(match[3] or 0), "allow_spoilers": match[1] == "允许剧透"}


def binding(reading, owner):
    if (not isinstance(reading, dict) or reading.get("owner_id") != owner or reading.get("scope") != "private:"+owner
            or not isinstance(reading.get("id"), str) or not re.fullmatch(r"reading:[0-9a-f]{64}", reading["id"])
            or reading.get("provider") not in {"cap", "pixiv"} or reading.get("resource_type") not in {"novel", "pdf", "illust"}):
        raise ValueError("invalid_reading_binding")
    service = reading.get("service_signature", "")
    if not isinstance(service, str) or service and not re.fullmatch(r"[0-9a-f]{64}", service):
        raise ValueError("invalid_service_signature")
    return {"id": reading["id"], "owner_id": owner, "scope": "private:"+owner,
            "provider": reading["provider"], "resource_type": reading["resource_type"],
            "resource_id": _text(reading.get("resource_id")), "version": _text(reading.get("version")),
            "source_refs": _refs(reading.get("binding_refs", reading.get("source_refs"))), "service_signature": service}


class SharedReadingStore:
    def __init__(self, path: Path, *, logger=None, sql_debug=False, mysql_settings=None):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = (MySQLCompatConnection(**mysql_settings, logger=logger, sql_debug=sql_debug)
                    if mysql_settings is not None else connect_sqlite(str(path), timeout=10, logger=logger, sql_debug=sql_debug))
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA busy_timeout=10000")
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.executescript("""
        CREATE TABLE IF NOT EXISTS shared_reading_control (
          owner_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS shared_reading_sessions (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, reading_id TEXT NOT NULL,
          status TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
          created REAL NOT NULL, updated REAL NOT NULL, UNIQUE(owner_id,reading_id)
        );
        CREATE INDEX IF NOT EXISTS idx_shared_reading_sessions_owner ON shared_reading_sessions(owner_id,status,updated);
        CREATE TABLE IF NOT EXISTS shared_reading_receipts (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, session_id TEXT NOT NULL,
          kind TEXT NOT NULL, body_digest TEXT NOT NULL, created REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_shared_reading_receipts_owner ON shared_reading_receipts(owner_id,created);
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

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        row = dict(row)
        row.update(json.loads(row.pop("payload")))
        return row

    def _lock(self, owner, now):
        self.con.execute("INSERT INTO shared_reading_control(owner_id,updated) VALUES (?,?) "
                         "ON CONFLICT(owner_id) DO UPDATE SET revision=revision", (owner, now))
        return dict(self.con.execute("SELECT * FROM shared_reading_control WHERE owner_id=?", (owner,)).fetchone())

    def _get(self, owner, reading_id):
        return self._decode(self.con.execute("SELECT * FROM shared_reading_sessions WHERE owner_id=? AND reading_id=?", (owner, reading_id)).fetchone())

    def session(self, owner, reading_id):
        rows = self._read("SELECT * FROM shared_reading_sessions WHERE owner_id=? AND reading_id=?", (_owner(owner), _text(reading_id)))
        return self._decode(rows[0]) if rows else None

    def sessions(self, owner):
        return [self._decode(row) for row in self._read("SELECT * FROM shared_reading_sessions WHERE owner_id=? ORDER BY updated DESC,id", (_owner(owner),))]

    def revision(self, owner):
        rows = self._read("SELECT revision FROM shared_reading_control WHERE owner_id=?", (_owner(owner),))
        return rows[0]["revision"] if rows else 0

    def _save(self, row, now, *, new=False):
        core = {"id", "owner_id", "reading_id", "status", "revision", "created", "updated"}
        payload = json.dumps({k: v for k, v in row.items() if k not in core}, ensure_ascii=False,
                             sort_keys=True, separators=(",", ":"), allow_nan=False)
        row["updated"] = now
        if new:
            self.con.execute("INSERT INTO shared_reading_sessions(id,owner_id,reading_id,status,revision,payload,created,updated) VALUES (?,?,?,?,?,?,?,?)",
                (row["id"], row["owner_id"], row["reading_id"], row["status"], row["revision"], payload, row["created"], now))
        else:
            self.con.execute("UPDATE shared_reading_sessions SET status=?,revision=?,payload=?,updated=? WHERE id=? AND owner_id=?",
                (row["status"], row["revision"], payload, now, row["id"], row["owner_id"]))
        self.con.execute("UPDATE shared_reading_control SET revision=revision+1,updated=? WHERE owner_id=?", (now, row["owner_id"]))

    def command(self, message, *, owner_id, action, reading, unit_count, units=0, allow_spoilers=False, now=None):
        owner, now = _owner(owner_id), _now(now)
        proof = binding(reading, owner)
        _integer(unit_count, 0, 1000000), _integer(units, 0, 1000000)
        if action not in {"start", "position", "stop", "clear", "spoilers"} or type(allow_spoilers) is not bool:
            raise ValueError("invalid_command")
        sender = extract_sender(message)
        if (not sender or sender.kind != "private" or sender.user_id != owner or not sender.stream_id or not sender.message_id
                or str(message.get("account_id") or "") == owner):
            raise ValueError("owner_private_required")
        raw = message.get("raw_message")
        if ((isinstance(raw, list) and any(not isinstance(p, dict) or p.get("type") != "text" for p in raw))
                or any(message.get(k) for k in ("is_picture", "is_voice", "is_emoji"))):
            raise ValueError("pure_text_required")
        expected = {"action": action, "reading_id": proof["id"], "units": units, "allow_spoilers": allow_spoilers}
        body = message.get("processed_plain_text")
        if parse_command(body) != expected or (action == "position" and units > unit_count):
            raise ValueError("invalid_reading_position")
        receipt = digest([sender.stream_id, sender.message_id])
        body_digest = digest(body)
        with self.con:
            control = self._lock(owner, now)
            old = self.con.execute("SELECT body_digest FROM shared_reading_receipts WHERE id=?", (receipt,)).fetchone()
            if old:
                if old["body_digest"] != body_digest:
                    raise ValueError("receipt_conflict")
                return False
            row = self._get(owner, proof["id"])
            new = row is None
            if new:
                if action != "start":
                    raise ValueError("shared_reading_not_started")
                count = self.con.execute("SELECT COUNT(*) AS n FROM shared_reading_sessions WHERE owner_id=?", (owner,)).fetchone()["n"]
                if count >= 64:
                    raise ValueError("shared_reading_capacity")
                row = {"id": "shared-reading:"+digest([owner, proof["id"]]), "owner_id": owner,
                       "reading_id": proof["id"], "status": "active", "revision": 0, "binding": proof,
                       "label_number": count+1, "owner_units": 0, "discussed_units": 0,
                       "allow_spoilers": False, "draft": None, "created": now}
            if row["binding"] != proof:
                raise ValueError("source_binding_changed")
            if action == "start":
                row["status"] = "active"
            elif action == "stop":
                row["status"] = "stopped"
            elif action == "clear":
                row.update(status="stopped", owner_units=0, discussed_units=0)
            elif action == "position":
                if row["status"] != "active":
                    raise ValueError("shared_reading_stopped")
                row["owner_units"] = units
            else:
                row["allow_spoilers"] = allow_spoilers
            row.update(revision=row["revision"]+1, draft=None)
            self._save(row, now, new=new)
            self.con.execute("INSERT INTO shared_reading_receipts(id,owner_id,session_id,kind,body_digest,created) VALUES (?,?,?,'command',?,?)",
                             (receipt, owner, row["id"], body_digest, now))
            return True

    def prepare(self, owner, sid, reading_id, expected_revision, unit_number, unit_digest, text, *, now=None):
        owner, sid, now = _owner(owner), _text(sid, 200), _now(now)
        _integer(expected_revision, 1), _integer(unit_number, 1)
        if not isinstance(unit_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", unit_digest):
            raise ValueError("invalid_unit_digest")
        text = _text(text, 500, multiline=True)
        with self.con:
            self._lock(owner, now)
            row = self._get(owner, reading_id)
            if (not row or row["status"] != "active" or row["revision"] != expected_revision
                    or unit_number > row["owner_units"]):
                return None
            current = row["draft"]
            if current and current["until"] > now:
                return row if all(current[k] == v for k, v in {"sid": sid, "unit": unit_number, "unit_digest": unit_digest, "text": text}.items()) else None
            row.update(draft={"sid": sid, "unit": unit_number, "unit_digest": unit_digest, "text": text, "until": now+600}, revision=row["revision"]+1)
            self._save(row, now)
            return row

    def record_delivery(self, owner, sid, text, receipt_id, *, reading_id, expected_revision, now=None):
        owner, now = _owner(owner), _now(now)
        if not isinstance(receipt_id, str) or not re.fullmatch(r"[0-9a-f]{64}", receipt_id):
            raise ValueError("invalid_receipt")
        if not isinstance(text, str):
            return False
        with self.con:
            self._lock(owner, now)
            if self.con.execute("SELECT id FROM shared_reading_receipts WHERE id=?", (receipt_id,)).fetchone():
                return False
            row = self._get(owner, reading_id)
            draft = row.get("draft") if row else None
            if (not row or row["status"] != "active" or row["revision"] != expected_revision or not draft
                    or draft["until"] <= now or draft["sid"] != sid or draft["text"] != text.strip()
                    or draft["unit"] > row["owner_units"]):
                return False
            row.update(discussed_units=max(row["discussed_units"], draft["unit"]), draft=None, revision=row["revision"]+1)
            self._save(row, now)
            self.con.execute("INSERT INTO shared_reading_receipts(id,owner_id,session_id,kind,body_digest,created) VALUES (?,?,?,'discussion',?,?)",
                             (receipt_id, owner, row["id"], digest(text), now))
            return True
