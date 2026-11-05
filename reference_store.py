"""Bounded current topic references, without retaining conversation text."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from .mysql_compat import MySQLCompatConnection
from .projects_store import _hex, _int, _now, _owner, references
from .sql_logging import connect_sqlite


class ReferenceStore:
    def __init__(self, path: Path, *, logger=None, sql_debug=False, mysql_settings=None):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = (MySQLCompatConnection(**mysql_settings, logger=logger, sql_debug=sql_debug)
                    if mysql_settings is not None else connect_sqlite(str(path), timeout=10, logger=logger, sql_debug=sql_debug))
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA busy_timeout=10000")
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.executescript("""
        CREATE TABLE IF NOT EXISTS reference_control (
          owner_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0,
          last_input TEXT NOT NULL DEFAULT '', payload TEXT NOT NULL,
          expires REAL NOT NULL DEFAULT 0, updated REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS reference_receipts (
          receipt_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, kind TEXT NOT NULL,
          text_digest TEXT NOT NULL, created REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_reference_receipts_owner ON reference_receipts(owner_id,created);
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

    def _lock(self, owner):
        self.con.execute("INSERT INTO reference_control(owner_id,payload) VALUES (?,?) "
                         "ON CONFLICT(owner_id) DO UPDATE SET revision=revision", (owner, "[]"))
        return dict(self.con.execute("SELECT * FROM reference_control WHERE owner_id=?", (owner,)).fetchone())

    def state(self, owner, *, now=None):
        owner, now = _owner(owner), _now(now)
        rows = self._read("SELECT * FROM reference_control WHERE owner_id=?", (owner,))
        row = rows[0] if rows else {"owner_id": owner, "revision": 0, "last_input": "", "payload": "[]", "expires": 0, "updated": 0}
        payload = json.loads(row.pop("payload"))
        row["focus_refs"] = references(payload.get("refs", []) if isinstance(payload, dict) else payload, empty=True)
        row["mentioned_at"] = _now(payload.get("mentioned_at", 0)) if isinstance(payload, dict) else 0
        row["active"] = bool(row["focus_refs"] and row["expires"] > now)
        return row

    def _receipt(self, owner, receipt, kind, body, now):
        old = self.con.execute("SELECT receipt_id FROM reference_receipts WHERE receipt_id=?", (receipt,)).fetchone()
        if old:
            return False
        self.con.execute("INSERT INTO reference_receipts(receipt_id,owner_id,kind,text_digest,created) VALUES (?,?,?,?,?)",
                         (receipt, owner, kind, body, now))
        return True

    def capture(self, owner, receipt_id, text_digest, *, keep_focus=False, invalidate_focus=False, now=None):
        owner, receipt_id, text_digest, now = _owner(owner), _hex(receipt_id), _hex(text_digest), _now(now)
        if type(keep_focus) is not bool or type(invalidate_focus) is not bool:
            raise ValueError("invalid_focus_policy")
        with self.con:
            row = self._lock(owner)
            if not self._receipt(owner, receipt_id, "input", text_digest, now):
                return False
            self.con.execute("UPDATE reference_control SET revision=revision+1,last_input=?,payload=?,expires=?,updated=? WHERE owner_id=?",
                (receipt_id, row["payload"] if keep_focus else "[]", row["expires"] if keep_focus and not invalidate_focus else 0, now, owner))
            return True

    def delivered(self, owner, receipt_id, text_digest, refs, *, expected_revision, input_receipt, ttl_seconds, now=None):
        owner, receipt_id, text_digest = _owner(owner), _hex(receipt_id), _hex(text_digest)
        input_receipt, now = _hex(input_receipt), _now(now)
        _int(expected_revision, 1, 10**12)
        _int(ttl_seconds, 3600, 72*3600)
        if not isinstance(refs, list) or len(refs) > 3:
            raise ValueError("too_many_focus_sources")
        refs = references(refs)
        with self.con:
            row = self._lock(owner)
            if row["revision"] != expected_revision or row["last_input"] != input_receipt:
                return False
            if not self._receipt(owner, receipt_id, "send", text_digest, now):
                return False
            self.con.execute("UPDATE reference_control SET revision=revision+1,payload=?,expires=?,updated=? WHERE owner_id=?",
                (json.dumps({"refs": refs, "mentioned_at": now}, ensure_ascii=False, sort_keys=True), now+ttl_seconds, now, owner))
            return True
