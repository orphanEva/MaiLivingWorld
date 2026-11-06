"""Bounded topic references and owner-private mention history without chat text."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from .mysql_compat import MySQLCompatConnection
from .projects_store import _hex, _int, _now, _owner, references
from .sql_logging import connect_sqlite


MAX_PRIOR_MENTIONS = 32
MENTION_TTL = 30 * 86400


def _payload(raw, now):
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        data = {}
    if isinstance(data, list):  # Legacy focus-only format.
        data = {"refs": data}
    if not isinstance(data, dict):
        data = {}
    history = data.get("prior_mentions", {})
    live = {}
    if isinstance(history, dict) and len(history) <= MAX_PRIOR_MENTIONS:
        for fingerprint, timestamp in history.items():
            if type(timestamp) not in (float, int):
                continue
            try:
                fingerprint, timestamp = _hex(fingerprint), _now(timestamp)
            except ValueError:
                continue
            if 0 < timestamp <= now < timestamp + MENTION_TTL:
                live[fingerprint] = timestamp
    mentioned_at = data.get("mentioned_at")
    try:
        mentioned_at = _now(mentioned_at) if mentioned_at is not None else 0
    except ValueError:
        mentioned_at = 0
    try:
        focus = references(data.get("refs", []), empty=True)
    except ValueError:
        focus = []
    return {"refs": focus, "mentioned_at": mentioned_at, "prior_mentions": live}


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
        payload = _payload(row.pop("payload"), now)
        row["focus_refs"] = payload["refs"]
        row["mentioned_at"] = payload["mentioned_at"]
        row["prior_mentions"] = payload["prior_mentions"]
        row["active"] = bool(row["focus_refs"] and 0 < row["mentioned_at"] <= now < row["expires"])
        return row

    def _receipt(self, owner, receipt, kind, body, now):
        old = self.con.execute("SELECT receipt_id FROM reference_receipts WHERE receipt_id=?", (receipt,)).fetchone()
        if old:
            return False
        self.con.execute("INSERT INTO reference_receipts(receipt_id,owner_id,kind,text_digest,created) VALUES (?,?,?,?,?)",
                         (receipt, owner, kind, body, now))
        return True

    def capture(self, owner, receipt_id, text_digest, *, keep_focus=False, invalidate_focus=False,
                clear_mentions=False, now=None):
        owner, receipt_id, text_digest, now = _owner(owner), _hex(receipt_id), _hex(text_digest), _now(now)
        if any(type(flag) is not bool for flag in (keep_focus, invalidate_focus, clear_mentions)):
            raise ValueError("invalid_focus_policy")
        with self.con:
            row = self._lock(owner)
            if not self._receipt(owner, receipt_id, "input", text_digest, now):
                return False
            payload = _payload(row["payload"], now)
            if not keep_focus:
                payload.update(refs=[], mentioned_at=0)
            if clear_mentions:
                payload["prior_mentions"].clear()
            self.con.execute("UPDATE reference_control SET revision=revision+1,last_input=?,payload=?,expires=?,updated=? WHERE owner_id=?",
                (receipt_id, json.dumps(payload), row["expires"] if keep_focus and not invalidate_focus else 0, now, owner))
            return True

    def delivered(self, owner, receipt_id, text_digest, refs, *, expected_revision, input_receipt,
                  ttl_seconds, confirmed_refs=None, now=None):
        owner, receipt_id, text_digest = _owner(owner), _hex(receipt_id), _hex(text_digest)
        input_receipt, now = _hex(input_receipt), _now(now)
        _int(expected_revision, 1, 10**12)
        _int(ttl_seconds, 3600, 72*3600)
        if not isinstance(refs, list) or len(refs) > 3:
            raise ValueError("too_many_focus_sources")
        refs = references(refs)
        if confirmed_refs is None:
            confirmed_refs = []
        if not isinstance(confirmed_refs, list) or len(confirmed_refs) > 12:
            raise ValueError("invalid_confirmed_sources")
        confirmed_refs = references(confirmed_refs, empty=True)
        with self.con:
            row = self._lock(owner)
            if row["revision"] != expected_revision or row["last_input"] != input_receipt:
                return False
            if not self._receipt(owner, receipt_id, "send", text_digest, now):
                return False
            # Only this chunk's complete anchors can add or refresh history. A
            # previous chunk's focus or an old backup is never promoted to proof.
            history = _payload(row["payload"], now)["prior_mentions"]
            confirmed = {ref["digest"]: now for ref in confirmed_refs}
            combined = {**confirmed, **{k: v for k, v in history.items() if k not in confirmed}}
            newest = sorted(combined, key=combined.get, reverse=True)[:MAX_PRIOR_MENTIONS]
            payload = {"refs": refs, "mentioned_at": now, "prior_mentions": {k: combined[k] for k in newest}}
            self.con.execute("UPDATE reference_control SET revision=revision+1,payload=?,expires=?,updated=? WHERE owner_id=?",
                (json.dumps(payload, ensure_ascii=False, sort_keys=True), now+ttl_seconds, now, owner))
            return True
