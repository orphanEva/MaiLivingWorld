"""Versioned repairs of actual delivered speech, not edits to factual memories.

Only the runtime may supply a delivery already verified against the host. Owner
inputs and commands are independently authenticated here. Recent full bodies
expire; accepted repairs retain only the two exact quotations and fingerprints.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid

from .continuity_store import ContinuityStore, _int, _json, _now, _owner, _text
from .mysql_compat import MySQLCompatConnection
from .owner import extract_sender
from .sql_logging import connect_sqlite


TTL = 86400
MAX_RECENT = 12
MAX_ITEMS = 64
MAX_PENDING = 32


def digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("invalid_digest")
    return value


def _body(value, maximum):
    _text(value, maximum)
    if any(ord(c) < 32 and c not in "\n\r\t" or ord(c) == 127 for c in value):
        raise ValueError("invalid_text")
    return value


def parse_command(text):
    match = re.fullmatch(r"/修复 (撤回|忘记) (repair:item:[0-9a-f]{64}) ([1-9][0-9]{0,8})", text.strip()) if isinstance(text, str) else None
    return {"action": "retract" if match[1] == "撤回" else "forget", "item_id": match[2], "revision": int(match[3])} if match else None


class RepairStore:
    def __init__(self, path: Path, *, logger=None, sql_debug=False, mysql_settings=None):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = (MySQLCompatConnection(**mysql_settings, logger=logger, sql_debug=sql_debug) if mysql_settings is not None
                    else connect_sqlite(str(path), timeout=10, logger=logger, sql_debug=sql_debug))
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA busy_timeout=10000")
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.executescript("""
        CREATE TABLE IF NOT EXISTS repair_control (
          owner_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0,
          epoch INTEGER NOT NULL DEFAULT 0, budget_day REAL NOT NULL DEFAULT 0,
          cycle_count INTEGER NOT NULL DEFAULT 0, last_cycle REAL NOT NULL DEFAULT 0,
          lease_token TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0,
          lease_id TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS repair_items (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, status TEXT NOT NULL,
          revision INTEGER NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_repair_items_owner ON repair_items(owner_id,status,updated);
        CREATE TABLE IF NOT EXISTS repair_inputs (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, body_digest TEXT NOT NULL,
          status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
          lease_token TEXT NOT NULL DEFAULT '', next_attempt REAL NOT NULL DEFAULT 0,
          expires REAL NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_repair_inputs_owner ON repair_inputs(owner_id,status,created);
        CREATE TABLE IF NOT EXISTS repair_receipts (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, kind TEXT NOT NULL,
          body_digest TEXT NOT NULL, expires REAL NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_repair_receipts_owner ON repair_receipts(owner_id,kind,created);
        """)
        self.con.commit()

    def close(self):
        self.con.close()

    def _read(self, sql, args=()):
        if isinstance(self.con, MySQLCompatConnection):
            return [dict(r) for r in self.con.fetch_readonly(sql, args)]
        cursor = self.con.execute(sql, args)
        try:
            return [dict(r) for r in cursor.fetchall()]
        finally:
            cursor.close()

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        result = dict(row)
        if "payload" in result:
            result.update(json.loads(result.pop("payload")))
        return result

    def _lock(self, owner, now):
        self.con.execute("INSERT INTO repair_control(owner_id,created,updated) VALUES (?,?,?) ON CONFLICT(owner_id) DO UPDATE SET revision=revision", (owner, now, now))
        return dict(self.con.execute("SELECT * FROM repair_control WHERE owner_id=?", (owner,)).fetchone())

    def state(self, owner):
        rows = self._read("SELECT * FROM repair_control WHERE owner_id=?", (_owner(owner),))
        return rows[0] if rows else {"owner_id": owner, "revision": 0, "epoch": 0, "budget_day": 0, "cycle_count": 0,
            "last_cycle": 0, "lease_token": "", "lease_until": 0, "lease_id": "", "created": 0, "updated": 0}

    def _clear_input(self, ident, status, now):
        self.con.execute("UPDATE repair_inputs SET status=?,payload=?,lease_token='',updated=? WHERE id=?",
                         (status, _json({"text": ""}), now, ident))

    def _expire(self, owner, now):
        self.con.execute("UPDATE repair_receipts SET payload=? WHERE owner_id=? AND kind='sent' AND expires<=?",
                         (_json({"text": ""}), owner, now))
        rows = self.con.execute("SELECT * FROM repair_receipts WHERE owner_id=? AND kind='sent' AND expires>? ORDER BY created DESC,id DESC", (owner, now)).fetchall()
        live = [self._decode(r) for r in rows if self._decode(r).get("text")]
        for row in live[MAX_RECENT:]:
            self.con.execute("UPDATE repair_receipts SET payload=?,expires=? WHERE id=?", (_json({"text": ""}), now, row["id"]))
        self.con.execute("UPDATE repair_inputs SET status='expired',payload=?,lease_token='',updated=? WHERE owner_id=? AND status IN ('pending','running') AND expires<=?",
                         (_json({"text": ""}), now, owner, now))
        control = self.con.execute("SELECT lease_id FROM repair_control WHERE owner_id=?", (owner,)).fetchone()
        if control and control["lease_id"]:
            row = self.con.execute("SELECT status FROM repair_inputs WHERE id=?", (control["lease_id"],)).fetchone()
            if not row or row["status"] != "running":
                self.con.execute("UPDATE repair_control SET lease_id='',lease_token='',lease_until=0 WHERE owner_id=?", (owner,))

    def record_delivery(self, owner, receipt, *, now=None):
        """Accept only the runtime's verified delivery object; never a model draft."""
        owner, now = _owner(owner), _now(now)
        if not isinstance(receipt, dict) or receipt.get("owner") != owner:
            raise ValueError("verified_owner_delivery_required")
        sid, mid = _text(receipt.get("sid"), 200), _text(receipt.get("mid"), 200)
        text = _body(receipt.get("text"), 1200)
        from .repair_model import safe_text
        if not safe_text(text, 1200):
            raise ValueError("safe_delivery_text_required")
        body_digest = hashlib.sha256(text.encode()).hexdigest()
        if _digest(receipt.get("text_digest")) != body_digest:
            raise ValueError("delivery_digest_conflict")
        _digest(receipt.get("receipt_id"))
        ident = "repair:sent:"+digest([owner, sid, mid])
        with self.con:
            self._lock(owner, now)
            self._expire(owner, now)
            previous = self.con.execute("SELECT body_digest FROM repair_receipts WHERE id=?", (ident,)).fetchone()
            if previous:
                if previous["body_digest"] != body_digest:
                    raise ValueError("receipt_conflict")
                return False
            self.con.execute("INSERT INTO repair_receipts(id,owner_id,kind,body_digest,expires,payload,created) VALUES (?,?,'sent',?,?,?,?)",
                (ident, owner, body_digest, now+TTL, _json({"sid": sid, "mid": mid, "text": text, "receipt_id": receipt["receipt_id"]}), now))
            self._expire(owner, now)
            return True

    def recent(self, owner, *, now=None):
        owner, now = _owner(owner), _now(now)
        with self.con:
            self._lock(owner, now)
            self._expire(owner, now)
            rows = self.con.execute("SELECT * FROM repair_receipts WHERE owner_id=? AND kind='sent' AND expires>? AND created<=? ORDER BY created DESC,id DESC", (owner, now, now)).fetchall()
            return [{**r, "digest": r["body_digest"]} for raw in rows if (r := self._decode(raw)).get("text")][:MAX_RECENT]

    def _delivery(self, owner, ident, expected, now):
        raw = self.con.execute("SELECT * FROM repair_receipts WHERE owner_id=? AND id=? AND kind='sent'", (owner, ident)).fetchone()
        row = self._decode(raw)
        return {**row, "digest": row["body_digest"]} if (row and row.get("text") and row["body_digest"] == expected
                    and row["created"] <= now < row["expires"] and hashlib.sha256(row["text"].encode()).hexdigest() == expected) else None

    @staticmethod
    def _receipt(message, owner, *, command=False):
        ident, body_digest, body = ContinuityStore._receipt(message, owner)
        _body(body, 600)
        if (any(message.get(k) for k in ("is_picture", "is_voice", "is_emoji"))
                or not command and (message.get("is_command") or body.lstrip().startswith("/"))):
            raise ValueError("trusted_owner_text_required")
        return ident, body_digest, body

    def enqueue(self, message, owner, delivery_id, delivery_digest, *, now=None):
        owner, now = _owner(owner), _now(now)
        receipt, body_digest, body = self._receipt(message, owner)
        from .repair_model import should_repair
        if not should_repair(body):
            raise ValueError("explicit_repair_required")
        sender = extract_sender(message)
        _text(delivery_id, 100), _digest(delivery_digest)
        ident = "repair:input:"+receipt
        with self.con:
            state = self._lock(owner, now)
            self._expire(owner, now)
            previous = self.con.execute("SELECT body_digest FROM repair_inputs WHERE id=?", (ident,)).fetchone()
            command = self.con.execute("SELECT body_digest FROM repair_receipts WHERE id=?", ("command:"+receipt,)).fetchone()
            for row in (previous, command):
                if row:
                    if row["body_digest"] != body_digest:
                        raise ValueError("receipt_conflict")
                    return None
            source = self._delivery(owner, delivery_id, delivery_digest, now)
            if not source:
                return None
            pending = [self._decode(r) for r in self.con.execute("SELECT * FROM repair_inputs WHERE owner_id=? AND status IN ('pending','running')", (owner,)).fetchall()]
            # The newest report about the same sent message wins over an older
            # queued/inflight interpretation. Other messages remain independently queued.
            for row in pending:
                if row.get("delivery_id") == delivery_id:
                    self._clear_input(row["id"], "superseded", now)
                    self.con.execute("UPDATE repair_control SET revision=revision+1,lease_id='',lease_token='',lease_until=0 WHERE owner_id=? AND lease_id=?", (owner, row["id"]))
            if sum(row.get("delivery_id") != delivery_id for row in pending) >= MAX_PENDING:
                return None
            self.con.execute("INSERT INTO repair_inputs(id,owner_id,body_digest,status,expires,payload,created,updated) VALUES (?,?,?,'pending',?,?,?,?)",
                (ident, owner, body_digest, min(now+TTL, source["expires"]), _json({"text": body, "delivery_id": delivery_id,
                  "delivery_digest": delivery_digest, "epoch": state["epoch"], "sid": sender.stream_id,
                  "mid": sender.message_id, "input_digest": body_digest}), now, now))
            return ident

    def claim(self, owner, *, day_start, daily_limit, interval_seconds, lease_seconds=180, now=None):
        owner, now, day = _owner(owner), _now(now), _now(day_start)
        _int(daily_limit, 0, 24), _int(interval_seconds, 0, 86400), _int(lease_seconds, 1, 600)
        if day > now or now-day > 172800:
            raise ValueError("invalid_day")
        with self.con:
            self._lock(owner, now)
            self._expire(owner, now)
            state = dict(self.con.execute("SELECT * FROM repair_control WHERE owner_id=?", (owner,)).fetchone())
            count = state["cycle_count"] if day == state["budget_day"] else 0
            if (state["lease_until"] > now or day < state["budget_day"] or count >= daily_limit
                    or state["cycle_count"] and now-state["last_cycle"] < interval_seconds):
                return None
            rows = self.con.execute("SELECT * FROM repair_inputs WHERE owner_id=? AND status IN ('pending','running') AND next_attempt<=? AND expires>? ORDER BY created,id", (owner, now, now)).fetchall()
            for raw in rows:
                row = self._decode(raw)
                source = self._delivery(owner, row.get("delivery_id", ""), row.get("delivery_digest", ""), now)
                if not source or row.get("epoch") != state["epoch"] or row["attempts"] >= 3:
                    self._clear_input(row["id"], "stale", now)
                    continue
                token = uuid.uuid4().hex
                self.con.execute("UPDATE repair_control SET budget_day=?,cycle_count=?,last_cycle=?,lease_id=?,lease_token=?,lease_until=?,updated=? WHERE owner_id=?",
                    (day, count+1, now, row["id"], token, now+lease_seconds, now, owner))
                self.con.execute("UPDATE repair_inputs SET status='running',lease_token=?,attempts=attempts+1,updated=? WHERE id=?", (token, now, row["id"]))
                return {**row, "token": token, "lease_token": token, "control_revision": state["revision"], "delivery": source}
            return None

    def _valid(self, owner, claim, state, now):
        if (not isinstance(claim, dict) or state["lease_id"] != claim.get("id")
                or state["lease_token"] != claim.get("token") or state["lease_until"] <= now
                or state["revision"] != claim.get("control_revision") or state["epoch"] != claim.get("epoch")):
            return None
        raw = self.con.execute("SELECT * FROM repair_inputs WHERE owner_id=? AND id=?", (owner, claim["id"])).fetchone()
        row = self._decode(raw)
        if (not row or row["status"] != "running" or row["lease_token"] != claim["token"]
                or row.get("epoch") != state["epoch"] or row["expires"] <= now
                or row["body_digest"] != claim.get("body_digest") or not row.get("text")
                or hashlib.sha256(row["text"].encode()).hexdigest() != row["body_digest"]):
            return None
        source = self._delivery(owner, row["delivery_id"], row["delivery_digest"], now)
        if not source:
            return None
        return row, source

    def valid(self, owner, claim, *, now=None):
        owner, now = _owner(owner), _now(now)
        # A short owner lock gives both database backends the same current view;
        # no stale MySQL snapshot may authorize an old correction after a command.
        with self.con:
            state = self._lock(owner, now)
            return bool(self._valid(owner, claim, state, now))

    def get(self, owner, ident):
        rows = self._read("SELECT * FROM repair_items WHERE owner_id=? AND id=?", (_owner(owner), _text(ident, 100)))
        return self._decode(rows[0]) if rows else None

    def items(self, owner):
        return [self._decode(r) for r in self._read("SELECT * FROM repair_items WHERE owner_id=? ORDER BY updated DESC,id", (_owner(owner),))]

    def _save(self, row, now, *, new=False):
        core = {"id", "owner_id", "status", "revision", "created", "updated"}
        payload = _json({k: v for k, v in row.items() if k not in core})
        if new:
            self.con.execute("INSERT INTO repair_items(id,owner_id,status,revision,payload,created,updated) VALUES (?,?,?,?,?,?,?)",
                (row["id"], row["owner_id"], row["status"], row["revision"], payload, now, now))
        else:
            self.con.execute("UPDATE repair_items SET status=?,revision=?,payload=?,updated=? WHERE owner_id=? AND id=?",
                (row["status"], row["revision"], payload, now, row["owner_id"], row["id"]))
        self.con.execute("UPDATE repair_control SET revision=revision+1,updated=? WHERE owner_id=?", (now, row["owner_id"]))

    def finish(self, owner, claim, result=None, *, retry=False, now=None):
        owner, now = _owner(owner), _now(now)
        if type(retry) is not bool:
            raise ValueError("invalid_retry")
        with self.con:
            state = self._lock(owner, now)
            if (not isinstance(claim, dict) or state["lease_id"] != claim.get("id")
                    or state["lease_token"] != claim.get("token")):
                return False
            current = self._valid(owner, claim, state, now)
            changed = False
            if current and result is not None:
                row, source = current
                from .repair_model import validate_quotes
                if not validate_quotes(result, source["text"], row["text"]):
                    raise ValueError("literal_repair_required")
                old_quote, correction = result["old_quote"], result["correction_quote"]
                recent = [self._decode(raw) for raw in self.con.execute(
                    "SELECT * FROM repair_receipts WHERE owner_id=? AND kind='sent' AND expires>? AND created<=?",
                    (owner, now, row["created"])).fetchall()]
                matching = [sent["id"] for sent in recent if sent.get("text") and old_quote in sent["text"]]
                if matching != [source["id"]]:
                    raise ValueError("ambiguous_delivered_quote")
                ident = "repair:item:"+digest([owner, source["id"], old_quote])
                previous = self.con.execute("SELECT * FROM repair_items WHERE owner_id=? AND id=?", (owner, ident)).fetchone()
                previous = self._decode(previous)
                count = self.con.execute("SELECT COUNT(*) AS n FROM repair_items WHERE owner_id=?", (owner,)).fetchone()["n"]
                if (previous is None and count >= MAX_ITEMS):
                    raise ValueError("repair_capacity")
                if previous is None or previous["status"] == "active" and previous.get("input_id") != row["id"]:
                    item = {"id": ident, "owner_id": owner, "status": "active", "revision": previous["revision"]+1 if previous else 1,
                        "old_quote": old_quote, "correction_quote": correction, "delivery_id": source["id"],
                        "delivery_digest": source["body_digest"], "delivery_at": source["created"],
                        "input_id": row["id"], "input_digest": row["body_digest"], "input_at": row["created"],
                        "input_sid": row["sid"], "input_mid": row["mid"],
                        "evidence_status": "owner_reported", "created": previous["created"] if previous else now, "updated": now}
                    self._save(item, now, new=previous is None)
                    changed = True
            pending = bool(current and retry and current[0]["attempts"] < 3 and result is None)
            if pending:
                self.con.execute("UPDATE repair_inputs SET status='pending',lease_token='',next_attempt=?,updated=? WHERE id=?", (now+600, now, claim["id"]))
            else:
                self._clear_input(claim["id"], "done" if current else "stale", now)
            self.con.execute("UPDATE repair_control SET lease_id='',lease_token='',lease_until=0 WHERE owner_id=?", (owner,))
            return changed

    def command(self, message, owner, *, now=None):
        owner, now = _owner(owner), _now(now)
        receipt, body_digest, body = self._receipt(message, owner, command=True)
        command = parse_command(body)
        if not command:
            raise ValueError("invalid_repair_command")
        with self.con:
            self._lock(owner, now)
            for table, ident in (("repair_receipts", "command:"+receipt), ("repair_inputs", "repair:input:"+receipt)):
                previous = self.con.execute("SELECT body_digest FROM "+table+" WHERE id=?", (ident,)).fetchone()
                if previous:
                    if previous["body_digest"] != body_digest:
                        raise ValueError("receipt_conflict")
                    return False
            self.con.execute("INSERT INTO repair_receipts(id,owner_id,kind,body_digest,expires,payload,created) VALUES (?,?,'command',?,0,?,?)",
                ("command:"+receipt, owner, body_digest, _json({"item_id": command["item_id"], "action": command["action"]}), now))
            raw = self.con.execute("SELECT * FROM repair_items WHERE owner_id=? AND id=?", (owner, command["item_id"])).fetchone()
            row = self._decode(raw)
            if not row or row["revision"] != command["revision"] or row["status"] == "forgotten":
                return False
            if command["action"] == "retract" and row["status"] != "active":
                return False
            row["status"] = "retracted" if command["action"] == "retract" else "forgotten"
            if command["action"] == "forget":
                self.con.execute("UPDATE repair_receipts SET payload=?,expires=? WHERE owner_id=? AND id=?", (_json({"text": ""}), now, owner, row["delivery_id"]))
                row["old_quote"], row["correction_quote"] = "", ""
            row["revision"] += 1
            self._save(row, now)
            # Commands are an epoch barrier: no already queued interpretation
            # can revive withdrawn/forgotten conclusions after an awaited model.
            self.con.execute("UPDATE repair_inputs SET status='revoked',payload=?,lease_token='',updated=? WHERE owner_id=? AND status IN ('pending','running')", (_json({"text": ""}), now, owner))
            self.con.execute("UPDATE repair_control SET epoch=epoch+1,lease_id='',lease_token='',lease_until=0 WHERE owner_id=?", (owner,))
            return True
