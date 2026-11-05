"""Delivered suggestions and explicit, revocable owner-reported outcomes."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid

from .continuity_store import ContinuityStore, _int, _json, _now, _owner, _text
from .projects_store import references as _refs
from .mysql_compat import MySQLCompatConnection
from .sql_logging import connect_sqlite


OUTCOMES = {"tried", "not_tried", "helpful", "unhelpful"}
LABELS = {"尝试过": "tried", "没尝试": "not_tried", "有用": "helpful", "没用": "unhelpful", "停止": "stop", "忘记": "forget"}


def digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def parse_command(text):
    match = re.fullmatch(r"/建议 (尝试过|没尝试|有用|没用|停止|忘记) (advice:item:[0-9a-f]{64}) ([1-9][0-9]{0,8})", text.strip()) if isinstance(text, str) else None
    return {"action": LABELS[match[1]], "item_id": match[2], "revision": int(match[3])} if match else None


def delivery_text(topic, advice):
    return "关于“"+topic+"”，我有个建议供你考虑："+advice


class AdviceStore:
    def __init__(self, path: Path, *, logger=None, sql_debug=False, mysql_settings=None):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = (MySQLCompatConnection(**mysql_settings, logger=logger, sql_debug=sql_debug) if mysql_settings is not None
                    else connect_sqlite(str(path), timeout=10, logger=logger, sql_debug=sql_debug))
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA busy_timeout=10000")
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.executescript("""
        CREATE TABLE IF NOT EXISTS advice_control (
          owner_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0,
          budget_day REAL NOT NULL DEFAULT 0, cycle_count INTEGER NOT NULL DEFAULT 0,
          last_cycle REAL NOT NULL DEFAULT 0, lease_token TEXT NOT NULL DEFAULT '',
          lease_until REAL NOT NULL DEFAULT 0, lease_id TEXT NOT NULL DEFAULT '',
          created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS advice_items (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, status TEXT NOT NULL,
          revision INTEGER NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_advice_items_owner ON advice_items(owner_id,status,updated);
        CREATE TABLE IF NOT EXISTS advice_inputs (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, body_digest TEXT NOT NULL,
          status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
          lease_token TEXT NOT NULL DEFAULT '', next_attempt REAL NOT NULL DEFAULT 0,
          expires REAL NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_advice_inputs_owner ON advice_inputs(owner_id,status,created);
        CREATE TABLE IF NOT EXISTS advice_receipts (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, item_id TEXT NOT NULL,
          kind TEXT NOT NULL, body_digest TEXT NOT NULL, created REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_advice_receipts_owner ON advice_receipts(owner_id,created);
        """)
        self.con.commit()

    def close(self):
        self.con.close()

    def _read(self, sql, args=()):
        if isinstance(self.con, MySQLCompatConnection):
            return [dict(row) for row in self.con.fetch_readonly(sql, args)]
        cursor = self.con.execute(sql, args)
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
        self.con.execute("INSERT INTO advice_control(owner_id,created,updated) VALUES (?,?,?) ON CONFLICT(owner_id) DO UPDATE SET revision=revision", (owner, now, now))
        return dict(self.con.execute("SELECT * FROM advice_control WHERE owner_id=?", (owner,)).fetchone())

    def state(self, owner):
        rows = self._read("SELECT * FROM advice_control WHERE owner_id=?", (_owner(owner),))
        return rows[0] if rows else {"owner_id": owner, "revision": 0, "budget_day": 0, "cycle_count": 0, "last_cycle": 0,
                                   "lease_id": "", "lease_token": "", "lease_until": 0, "created": 0, "updated": 0}

    def get(self, owner, ident):
        rows = self._read("SELECT * FROM advice_items WHERE owner_id=? AND id=?", (_owner(owner), _text(ident, 100)))
        return self._decode(rows[0]) if rows else None

    def items(self, owner):
        return [self._decode(r) for r in self._read("SELECT * FROM advice_items WHERE owner_id=? ORDER BY updated DESC,id", (_owner(owner),))]

    def _get(self, owner, ident):
        return self._decode(self.con.execute("SELECT * FROM advice_items WHERE owner_id=? AND id=?", (owner, ident)).fetchone())

    def _save(self, row, now, new=False):
        core = {"id", "owner_id", "status", "revision", "created", "updated"}
        payload = _json({k: v for k, v in row.items() if k not in core})
        if new:
            self.con.execute("INSERT INTO advice_items(id,owner_id,status,revision,payload,created,updated) VALUES (?,?,?,?,?,?,?)",
                             (row["id"], row["owner_id"], row["status"], row["revision"], payload, now, now))
        else:
            self.con.execute("UPDATE advice_items SET status=?,revision=?,payload=?,updated=? WHERE owner_id=? AND id=?",
                             (row["status"], row["revision"], payload, now, row["owner_id"], row["id"]))
        self.con.execute("UPDATE advice_control SET revision=revision+1,updated=? WHERE owner_id=?", (now, row["owner_id"]))

    def _receipt(self, owner, receipt, body_digest, item, kind, now):
        old = self.con.execute("SELECT body_digest FROM advice_receipts WHERE id=?", (receipt,)).fetchone()
        if old:
            if old["body_digest"] != body_digest:
                raise ValueError("receipt_conflict")
            return False
        self.con.execute("INSERT INTO advice_receipts(id,owner_id,item_id,kind,body_digest,created) VALUES (?,?,?,?,?,?)",
                         (receipt, owner, item, kind, body_digest, now))
        return True

    def prepare(self, owner, sid, mid, topic, advice, source_refs, *, now=None):
        owner, now = _owner(owner), _now(now)
        sid, mid = _text(sid, 200), _text(mid, 200)
        topic, advice, refs = _text(topic, 60), _text(advice, 240), _refs(source_refs)
        if not 1 <= len(refs) <= 4 or any(ord(c) < 32 for c in topic+advice):
            raise ValueError("invalid_advice")
        ident = "advice:item:"+digest([owner, sid, mid, topic, advice, refs])
        body = delivery_text(topic, advice)
        with self.con:
            self._lock(owner, now)
            existing = self._get(owner, ident)
            if existing:
                if existing["status"] != "draft" or existing["expires"] <= now:
                    raise ValueError("advice_already_used")
                return existing
            rows = [self._decode(r) for r in self.con.execute("SELECT * FROM advice_items WHERE owner_id=?", (owner,)).fetchall()]
            if len(rows) >= 64:
                raise ValueError("advice_capacity")
            if any(row.get("content_digest") == digest(body) for row in rows):
                raise ValueError("advice_already_known")
            row = {"id": ident, "owner_id": owner, "status": "draft", "revision": 1, "topic": topic, "advice": advice,
                   "source_refs": refs, "sid": sid, "mid": mid, "delivery_text": body, "content_digest": digest(body),
                   "expires": now+600, "outcome": "unknown", "feedback": "", "feedback_receipt": "", "created": now, "updated": now}
            self._save(row, now, True)
            return row

    def record_delivery(self, owner, ident, revision, sid, text, receipt_id, *, now=None):
        owner, now = _owner(owner), _now(now)
        _int(revision, 1), _text(receipt_id, 100)
        with self.con:
            self._lock(owner, now)
            row = self._get(owner, ident)
            if (not row or row["status"] != "draft" or row["revision"] != revision or row["expires"] <= now
                    or row["sid"] != sid or row["delivery_text"] != text):
                return False
            if not self._receipt(owner, "send:"+receipt_id, digest(text), ident, "sent", now):
                return False
            row.update(status="active", revision=revision+1, delivered_at=now, delivery_text="", expires=0)
            self._save(row, now)
            return True

    def command(self, message, owner, *, now=None):
        owner, now = _owner(owner), _now(now)
        receipt, body_digest, body = ContinuityStore._receipt(message, owner)
        command = parse_command(body)
        if not command or any(message.get(k) for k in ("is_picture", "is_voice", "is_emoji")):
            raise ValueError("invalid_advice_command")
        with self.con:
            self._lock(owner, now)
            previous_input = self.con.execute("SELECT body_digest FROM advice_inputs WHERE id=?", ("advice:input:"+receipt,)).fetchone()
            if previous_input:
                if previous_input["body_digest"] != body_digest:
                    raise ValueError("receipt_conflict")
                return False
            row = self._get(owner, command["item_id"])
            if not self._receipt(owner, "command:"+receipt, body_digest, command["item_id"], command["action"], now):
                return False
            if not row or row["revision"] != command["revision"] or row["status"] == "forgotten":
                return False
            action = command["action"]
            if action in OUTCOMES and row["status"] != "active":
                return False
            if action == "forget":
                keep = {k: row[k] for k in ("id", "owner_id", "revision", "content_digest", "created", "updated")}
                row = {**keep, "status": "forgotten", "topic": "", "advice": "", "source_refs": [], "feedback": "", "outcome": "unknown"}
                # Remove queued natural evidence for this item, retaining receipt fingerprints.
                for value in self.con.execute("SELECT * FROM advice_inputs WHERE owner_id=?", (owner,)).fetchall():
                    value = self._decode(value)
                    if value.get("item_id") == row["id"]:
                        self.con.execute("UPDATE advice_inputs SET payload=?,status='forgotten',lease_token='',updated=? WHERE id=?", (_json({"text": ""}), now, value["id"]))
            elif action == "stop":
                row.update(status="stopped", delivery_text="", expires=0)
            else:
                row.update(outcome=action, feedback=body, feedback_receipt=receipt, feedback_kind="explicit_command")
            row["revision"] += 1
            self._save(row, now)
            return True

    def enqueue(self, message, owner, item_id, revision, *, now=None):
        owner, now = _owner(owner), _now(now)
        receipt, body_digest, body = ContinuityStore._receipt(message, owner)
        _text(body, 600), _int(revision, 1)
        if message.get("is_command") or body.lstrip().startswith("/") or any(message.get(k) for k in ("is_picture", "is_voice", "is_emoji")):
            raise ValueError("ordinary_text_required")
        ident = "advice:input:"+receipt
        with self.con:
            self._lock(owner, now)
            previous_command = self.con.execute("SELECT body_digest FROM advice_receipts WHERE id=?", ("command:"+receipt,)).fetchone()
            if previous_command:
                if previous_command["body_digest"] != body_digest:
                    raise ValueError("receipt_conflict")
                return None
            old = self.con.execute("SELECT body_digest FROM advice_inputs WHERE id=?", (ident,)).fetchone()
            if old:
                if old["body_digest"] != body_digest:
                    raise ValueError("receipt_conflict")
                return None
            row = self._get(owner, item_id)
            if not row or row["status"] != "active" or row["revision"] != revision:
                return None
            self._expire(owner, now)
            count = self.con.execute("SELECT COUNT(*) AS n FROM advice_inputs WHERE owner_id=? AND status IN ('pending','running')", (owner,)).fetchone()["n"]
            if count >= 32:
                return None
            # A newer explicit report on this same advice supersedes an older
            # queued report; its late model result must not win afterwards.
            for raw in self.con.execute("SELECT * FROM advice_inputs WHERE owner_id=? AND status IN ('pending','running')", (owner,)).fetchall():
                previous = self._decode(raw)
                if previous.get("item_id") == item_id:
                    self.con.execute("UPDATE advice_inputs SET status='superseded',payload=?,lease_token='',updated=? WHERE id=?", (_json({"text": ""}), now, previous["id"]))
                    self.con.execute("UPDATE advice_control SET revision=revision+1,lease_id='',lease_token='',lease_until=0 WHERE owner_id=? AND lease_id=?", (owner, previous["id"]))
            self.con.execute("INSERT INTO advice_inputs(id,owner_id,body_digest,status,expires,payload,created,updated) VALUES (?,?,?,'pending',?,?,?,?)",
                             (ident, owner, body_digest, now+604800, _json({"text": body, "item_id": item_id, "item_revision": revision}), now, now))
            return ident

    def _expire(self, owner, now):
        self.con.execute("UPDATE advice_inputs SET status='expired',payload=?,lease_token='',updated=? WHERE owner_id=? AND status IN ('pending','running') AND expires<=?",
                         (_json({"text": ""}), now, owner, now))

    def claim(self, owner, *, day_start, interval_seconds, daily_limit, lease_seconds=120, now=None):
        owner, now, day = _owner(owner), _now(now), _now(day_start)
        _int(interval_seconds, 0, 86400), _int(daily_limit, 0, 24), _int(lease_seconds, 1, 600)
        if day > now or now-day > 172800:
            raise ValueError("invalid_day")
        with self.con:
            state = self._lock(owner, now)
            self._expire(owner, now)
            count = state["cycle_count"]
            if day > state["budget_day"]:
                count = 0
            if (state["lease_until"] > now or day < state["budget_day"] or count >= daily_limit
                    or state["cycle_count"] and now-state["last_cycle"] < interval_seconds):
                return None
            rows = self.con.execute("SELECT * FROM advice_inputs WHERE owner_id=? AND status IN ('pending','running') AND next_attempt<=? AND expires>? ORDER BY created,id", (owner, now, now)).fetchall()
            for raw in rows:
                item = self._decode(raw)
                source = self._get(owner, item.get("item_id", ""))
                if not source or source["status"] != "active" or source["revision"] != item.get("item_revision") or item["attempts"] >= 3:
                    self.con.execute("UPDATE advice_inputs SET payload=?,status='stale',lease_token='',updated=? WHERE id=?", (_json({"text": ""}), now, item["id"]))
                    continue
                token = uuid.uuid4().hex
                self.con.execute("UPDATE advice_control SET budget_day=?,cycle_count=?,last_cycle=?,lease_id=?,lease_token=?,lease_until=? WHERE owner_id=?",
                                 (day, count+1, now, item["id"], token, now+lease_seconds, owner))
                self.con.execute("UPDATE advice_inputs SET status='running',lease_token=?,attempts=attempts+1,updated=? WHERE id=?", (token, now, item["id"]))
                return {**item, "lease_token": token, "base_revision": state["revision"], "item": source}
            return None

    def valid(self, owner, claim, *, now=None):
        state, now = self.state(owner), _now(now)
        row = self.get(owner, claim["item_id"])
        return bool(state["lease_id"] == claim["id"] and state["lease_token"] == claim["lease_token"]
                    and state["lease_until"] > now and row and row["status"] == "active"
                    and row["revision"] == claim["item_revision"])

    def finish(self, owner, claim, outcome=None, quote="", *, retry=False, now=None):
        owner, now = _owner(owner), _now(now)
        if outcome is not None and outcome not in OUTCOMES:
            raise ValueError("invalid_advice_outcome")
        with self.con:
            state = self._lock(owner, now)
            if state["lease_id"] != claim["id"] or state["lease_token"] != claim["lease_token"]:
                return False
            raw = self.con.execute("SELECT * FROM advice_inputs WHERE id=? AND owner_id=?", (claim["id"], owner)).fetchone()
            item = self._decode(raw)
            row = self._get(owner, item.get("item_id", "")) if item else None
            valid = (item and item["status"] == "running" and item["lease_token"] == claim["lease_token"] and item["expires"] > now
                     and state["lease_until"] > now and row
                     and row["status"] == "active" and row["revision"] == item["item_revision"])
            changed = False
            if valid and outcome is not None:
                if not isinstance(quote, str) or not 3 <= len(quote) <= 240 or item["text"].count(quote) != 1:
                    raise ValueError("literal_feedback_required")
                row.update(outcome=outcome, feedback=quote, feedback_receipt=item["id"], feedback_kind="literal_owner_report", revision=row["revision"]+1)
                self._save(row, now)
                changed = True
            pending = bool(valid and retry and item["attempts"] < 3)
            if item and item["status"] == "running":
                self.con.execute("UPDATE advice_inputs SET status=?,payload=?,lease_token='',next_attempt=?,updated=? WHERE id=?",
                    ("pending" if pending else "done", _json({k: item[k] for k in ("text", "item_id", "item_revision")}) if pending else _json({"text": ""}), now+600 if pending else 0, now, item["id"]))
            self.con.execute("UPDATE advice_control SET lease_id='',lease_token='',lease_until=0 WHERE owner_id=?", (owner,))
            return changed
