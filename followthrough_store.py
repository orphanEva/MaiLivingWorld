"""Durable, bounded promises about existing jobs; no external work is started here.

Only the runtime may supply verified deliveries. A prepared report is not a
delivery, and a lost polling lease can never manufacture a terminal result.
"""
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
from .owner import normalize_qq
from .sql_logging import connect_sqlite

OPERATIONS = {
    "cap": {"jm_start_download": "PDF 下载任务", "jm_publish_pdf": "PDF 上传任务",
            "jm_receive_pdf": "PDF 接收任务"},
    "pixiv": {"archive_work": "Pixiv 归档任务", "archive_series": "Pixiv 合集中的归档任务",
              "generate_ugoira_preview": "Pixiv 动图预览任务", "run_sync_source": "Pixiv 来源扫描任务"},
}
TERMINAL = {"succeeded", "failed", "cancelled"}
MAX_POLLS = 168
MAX_AGE = 14 * 86400


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _now(value=None):
    value = time.time() if value is None else value
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1e12:
        raise ValueError("invalid_time")
    return float(value)


def _integer(value, low=0, high=1000000):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("invalid_integer")
    return value


def _text(value, maximum=200):
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError("invalid_text")
    return value


def _owner(value):
    if not isinstance(value, str) or not value or normalize_qq(value) != value:
        raise ValueError("invalid_owner")
    return value


def _hex(value, length=64):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{"+str(length)+"}", value):
        raise ValueError("invalid_digest")
    return value


def report_text(row):
    label = row["label"]
    suffix = {"succeeded": "服务已确认处理成功。", "failed": "服务已明确返回失败；我没有重试写操作。",
              "cancelled": "服务已确认任务取消。", "unknown": "我暂时无法确认最终结果，已停止自动查询；这不表示任务成功或失败。"}
    return "之前答应跟进的"+label+"，"+suffix[row["result"]]


class FollowthroughStore:
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
        CREATE TABLE IF NOT EXISTS followthrough_control (
          owner_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0,
          budget_day REAL NOT NULL DEFAULT 0, daily_checks INTEGER NOT NULL DEFAULT 0,
          last_check REAL NOT NULL DEFAULT 0, updated REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS followthrough_items (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, status TEXT NOT NULL,
          revision INTEGER NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_followthrough_items_owner ON followthrough_items(owner_id,status,created);
        CREATE TABLE IF NOT EXISTS followthrough_receipts (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, item_id TEXT NOT NULL,
          kind TEXT NOT NULL, body_digest TEXT NOT NULL, created REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_followthrough_receipts_owner ON followthrough_receipts(owner_id,created);
        CREATE TABLE IF NOT EXISTS followthrough_sources (
          id TEXT PRIMARY KEY, provider TEXT NOT NULL, task_id TEXT NOT NULL,
          owner_id TEXT NOT NULL, scope TEXT NOT NULL, operation TEXT NOT NULL,
          source_signature TEXT NOT NULL, created REAL NOT NULL
        );
        """)
        self.con.commit()

    def close(self):
        self.con.close()

    def bind_source(self, provider, task_id, *, owner_id, scope, operation, source_signature, now=None):
        owner, now = _owner(owner_id), _now(now)
        _hex(task_id, 32), _hex(source_signature)
        base, _, requirement = operation.partition(":") if isinstance(operation, str) else ("", "", "")
        if (scope != "private:"+owner or base not in OPERATIONS.get(provider, {})
                or requirement not in {"", "account"}):
            raise ValueError("unsupported_binding")
        identity = digest([provider, task_id])
        expected = {"provider": provider, "task_id": task_id, "owner_id": owner, "scope": scope,
                    "operation": operation, "source_signature": source_signature}
        with self.con:
            self._lock(owner, now)
            old = self.con.execute("SELECT * FROM followthrough_sources WHERE id=?", (identity,)).fetchone()
            if old:
                if any(old[key] != value for key, value in expected.items()):
                    raise ValueError("source_binding_conflict")
                return dict(old)
            self.con.execute("INSERT INTO followthrough_sources(id,provider,task_id,owner_id,scope,operation,source_signature,created) "
                             "VALUES (?,?,?,?,?,?,?,?)", (identity, provider, task_id, owner, scope, operation, source_signature, now))
            return {"id": identity, **expected, "created": now}

    def source(self, provider, task_id):
        _hex(task_id, 32)
        if provider not in OPERATIONS:
            raise ValueError("invalid_provider")
        rows = self._read("SELECT * FROM followthrough_sources WHERE id=?", (digest([provider, task_id]),))
        return rows[0] if rows else None

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
        # One upsert acquires the InnoDB exclusive lock before any consistent read.
        self.con.execute("INSERT INTO followthrough_control(owner_id,updated) VALUES (?,?) "
                         "ON CONFLICT(owner_id) DO UPDATE SET revision=revision", (owner, now))
        return dict(self.con.execute("SELECT * FROM followthrough_control WHERE owner_id=?", (owner,)).fetchone())

    def _save(self, row, now, *, new=False):
        fields = {"id", "owner_id", "status", "revision", "created", "updated"}
        payload = json.dumps({k: v for k, v in row.items() if k not in fields}, ensure_ascii=False,
                             sort_keys=True, separators=(",", ":"), allow_nan=False)
        row["updated"] = now
        if new:
            self.con.execute("INSERT INTO followthrough_items(id,owner_id,status,revision,payload,created,updated) "
                             "VALUES (?,?,?,?,?,?,?)", (row["id"], row["owner_id"], row["status"], row["revision"],
                                                       payload, row["created"], now))
        else:
            self.con.execute("UPDATE followthrough_items SET status=?,revision=?,payload=?,updated=? WHERE id=? AND owner_id=?",
                             (row["status"], row["revision"], payload, now, row["id"], row["owner_id"]))

    def _get(self, owner, ident):
        return self._decode(self.con.execute("SELECT * FROM followthrough_items WHERE id=? AND owner_id=?", (ident, owner)).fetchone())

    def get(self, owner, ident):
        rows = self._read("SELECT * FROM followthrough_items WHERE id=? AND owner_id=?", (ident, _owner(owner)))
        return self._decode(rows[0]) if rows else None

    def items(self, owner):
        return [self._decode(row) for row in self._read(
            "SELECT * FROM followthrough_items WHERE owner_id=? ORDER BY created DESC,id DESC LIMIT 128", (_owner(owner),))]

    def draft(self, owner, sid, provider, task_id, operation, binding_digest, source_signature, *, now=None):
        owner, sid, now = _owner(owner), _text(sid), _now(now)
        _hex(task_id, 32), _hex(binding_digest), _hex(source_signature)
        base, _, requirement = operation.partition(":") if isinstance(operation, str) else ("", "", "")
        if base not in OPERATIONS.get(provider, {}) or requirement not in {"", "account"}:
            raise ValueError("unsupported_operation")
        ident = "followthrough:item:"+digest([owner, provider, task_id, source_signature])
        with self.con:
            control = self._lock(owner, now)
            old = self._get(owner, ident)
            if old and (old["status"] not in {"draft", "expired"}
                        or old["status"] == "draft" and old["draft_until"] > now and old["sid"] == sid):
                return old
            rows = [self._decode(row) for row in self.con.execute(
                "SELECT * FROM followthrough_items WHERE owner_id=?", (owner,)).fetchall()]
            if not old and len(rows) >= 128:
                raise ValueError("followthrough_capacity")
            for row in rows:
                if row["status"] == "draft" and (row["sid"] == sid or row["draft_until"] <= now):
                    row.update(status="expired", revision=row["revision"]+1)
                    self._save(row, now)
            number = control["revision"]+1
            self.con.execute("UPDATE followthrough_control SET revision=?,updated=? WHERE owner_id=?", (number, now, owner))
            label = "第 "+str(number)+" 项"+OPERATIONS[provider][base]
            row = {"id": ident, "owner_id": owner, "status": "draft", "revision": (old["revision"]+1 if old else 1),
                   "created": now, "updated": now, "sid": sid, "provider": provider, "task_id": task_id,
                   "operation": operation, "binding_digest": binding_digest, "source_signature": source_signature,
                   "label": label, "promise_text": "关于"+label+"，处理完我会告诉你结果。",
                   "draft_until": now+600, "deadline": now+MAX_AGE, "result": "pending", "polls": 0,
                   "failures": 0, "next_poll": now, "lease_token": "", "lease_until": 0,
                   "report_sid": "", "report_until": 0, "delivery_text": ""}
            self._save(row, now, new=old is None)
            return row

    def match_delivery(self, owner, sid, text, *, now=None):
        now = _now(now)
        if not isinstance(text, str):
            return None
        # An exact standalone message is intentionally narrower than semantic matching.
        for row in self.items(owner):
            if (row["status"] == "draft" and row["sid"] == sid and row["draft_until"] > now
                    and text.strip() == row["promise_text"]):
                return row
            if (row["status"] == "ready" and row["report_sid"] == sid and row["report_until"] > now
                    and text.strip() == row["delivery_text"]):
                return row
        return None

    def record_delivery(self, owner, sid, text, receipt_id, *, item_id, expected_revision, now=None):
        owner, now = _owner(owner), _now(now)
        _hex(receipt_id)
        _integer(expected_revision, 1)
        if not isinstance(text, str):
            return False
        with self.con:
            self._lock(owner, now)
            if self.con.execute("SELECT id FROM followthrough_receipts WHERE id=?", (receipt_id,)).fetchone():
                return False
            row = self._get(owner, item_id)
            if not row or row["revision"] != expected_revision:
                return False
            kind = ("promise" if row["status"] == "draft" and row["sid"] == sid
                    and row["draft_until"] > now and text.strip() == row["promise_text"] else
                    "report" if row["status"] == "ready" and row["report_sid"] == sid
                    and row["report_until"] > now and text.strip() == row["delivery_text"] else "")
            if not kind:
                return False
            self.con.execute("INSERT INTO followthrough_receipts(id,owner_id,item_id,kind,body_digest,created) VALUES (?,?,?,?,?,?)",
                             (receipt_id, owner, item_id, kind, hashlib.sha256(text.encode()).hexdigest(), now))
            row.update(status="active" if kind == "promise" else "reported", revision=row["revision"]+1,
                       lease_token="", lease_until=0, report_sid="", report_until=0, delivery_text="")
            self._save(row, now)
            return True

    def due(self, owner, *, now=None):
        now = _now(now)
        rows = [row for row in self.items(owner) if row["status"] == "ready" and row["report_until"] <= now]
        return min(rows, key=lambda row: (row["updated"], row["id"])) if rows else None

    def prepare_report(self, owner, sid, item_id, expected_revision, *, now=None):
        owner, sid, now = _owner(owner), _text(sid), _now(now)
        _integer(expected_revision, 1)
        with self.con:
            self._lock(owner, now)
            row = self._get(owner, item_id)
            if not row or row["status"] != "ready" or row["revision"] != expected_revision:
                return None
            if row["report_until"] > now:
                return row if row["report_sid"] == sid else None
            row.update(report_sid=sid, report_until=now+600, delivery_text=report_text(row), revision=row["revision"]+1)
            self._save(row, now)
            return row

    def valid_report(self, owner, sid, item_id, expected_revision, *, now=None):
        row = self.get(owner, item_id)
        return bool(row and row["status"] == "ready" and row["revision"] == expected_revision
                    and row["report_sid"] == sid and row["report_until"] > _now(now))

    def cancel(self, owner, item_id, expected_revision, *, now=None):
        owner, now = _owner(owner), _now(now)
        _integer(expected_revision, 1)
        with self.con:
            self._lock(owner, now)
            row = self._get(owner, item_id)
            if not row or row["revision"] != expected_revision or row["status"] not in {"draft", "active", "ready"}:
                return False
            row.update(status="cancelled", revision=row["revision"]+1, lease_token="", lease_until=0,
                       report_sid="", report_until=0, delivery_text="")
            self._save(row, now)
            return True

    def claim_poll(self, owner, *, day_start, daily_limit=12, interval_seconds=300, lease_seconds=120,
                   eligible_ids=None, now=None):
        owner, now, day = _owner(owner), _now(now), _now(day_start)
        _integer(daily_limit, 0, 12), _integer(interval_seconds, 300, 86400), _integer(lease_seconds, 1, 3600)
        if eligible_ids is not None:
            if (not isinstance(eligible_ids, (list, tuple)) or len(eligible_ids) > 128
                    or any(not isinstance(ident, str) or not re.fullmatch(r"followthrough:item:[0-9a-f]{64}", ident)
                           for ident in eligible_ids)):
                raise ValueError("invalid_eligible_ids")
            eligible_ids = set(eligible_ids)
        if not day <= now < day+90000:
            raise ValueError("invalid_day")
        with self.con:
            control = self._lock(owner, now)
            rows = [self._decode(row) for row in self.con.execute(
                "SELECT * FROM followthrough_items WHERE owner_id=? AND status='active' ORDER BY updated,id", (owner,)).fetchall()]
            candidates = []
            for row in rows:
                if row["lease_until"] > now:
                    continue
                if row["deadline"] <= now or row["polls"] >= MAX_POLLS:
                    row.update(status="ready", result="unknown", revision=row["revision"]+1, lease_token="", lease_until=0)
                    self._save(row, now)
                elif row["next_poll"] <= now and (eligible_ids is None or row["id"] in eligible_ids):
                    candidates.append(row)
            if (not candidates or not daily_limit or control["budget_day"] > day
                    or (control["budget_day"] == day and control["daily_checks"] >= daily_limit)
                    or (control["daily_checks"] and control["last_check"]+interval_seconds > now)):
                return None
            count = control["daily_checks"]+1 if control["budget_day"] == day else 1
            self.con.execute("UPDATE followthrough_control SET budget_day=?,daily_checks=?,last_check=?,updated=? WHERE owner_id=?",
                             (day, count, now, now, owner))
            row = min(candidates, key=lambda item: (item["next_poll"], item["updated"], item["id"]))
            row.update(polls=row["polls"]+1, revision=row["revision"]+1, lease_token=uuid.uuid4().hex,
                       lease_until=now+lease_seconds, next_poll=now+interval_seconds)
            self._save(row, now)
            return row

    def valid_poll(self, owner, item_id, token, revision, *, now=None):
        row = self.get(owner, item_id)
        return bool(row and row["status"] == "active" and row["revision"] == revision
                    and row["lease_token"] == token and row["lease_until"] > _now(now))

    def finish_poll(self, owner, item_id, token, revision, result, *, interval_seconds=300, now=None):
        owner, now = _owner(owner), _now(now)
        _integer(interval_seconds, 300, 86400)
        if result not in TERMINAL | {"pending", "unknown"}:
            raise ValueError("invalid_result")
        with self.con:
            self._lock(owner, now)
            row = self._get(owner, item_id)
            if (not row or row["status"] != "active" or row["revision"] != revision
                    or row["lease_token"] != token or row["lease_until"] <= now):
                return False
            failures = min(row["failures"]+1, 10) if result == "unknown" else 0
            row.update(result=result, failures=failures, lease_token="", lease_until=0, revision=row["revision"]+1,
                       next_poll=now+max(interval_seconds, min(3600, 300*(2**failures))))
            if result in TERMINAL or row["polls"] >= MAX_POLLS or row["deadline"] <= now:
                row["status"] = "ready"
                if result not in TERMINAL:
                    row["result"] = "unknown"
            self._save(row, now)
            return True
