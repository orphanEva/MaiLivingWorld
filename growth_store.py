"""Durable bounded growth, source-backed revisions and restartable learning jobs.

This ledger never changes the owner binding. Jobs contain source references, not
chat text. Growth changes, source consumption and job completion commit together.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import sqlite3
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any

from .mysql_compat import MySQLCompatConnection
from .sql_logging import connect_sqlite
from .belief_revision import FIELDS as BELIEF_REVIEW_FIELDS, assess as assess_belief
from .emotion_episodes import active_items as active_emotions, apply_candidates as apply_emotions, receipt_keys as emotion_receipts
from .growth_limits import MAX_INTEREST_SOURCES, MAX_RECORDS

TRAITS = ("curiosity", "sociability", "patience", "caution")
COLLECTIONS = ("interests", "beliefs", "knowledge", "questions")
MAX_ITEMS = 32
MAX_PENDING = 128
HALF_LIFE = 6 * 3600
_FINGERPRINT_FIELDS = ("id", "text", "scope", "subject_id", "sensitivity",
                       "evidence_status", "source", "kind", "origin_key")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _number(value: Any, code: str = "invalid_number") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(code)
    return float(value)


def _now(value: float | None) -> float:
    result = time.time() if value is None else _number(value, "invalid_time")
    if result < 0:
        raise ValueError("invalid_time")
    return result


def _text(value: Any, limit: int, code: str = "invalid_text") -> str:
    if not isinstance(value, str):
        raise ValueError(code)
    result = " ".join(value.split())
    if not result or len(result) > limit or any(ord(c) < 32 for c in result):
        raise ValueError(code)
    return result


def _normalized(value: str) -> str:
    return re.sub(r"\W+", "", unicodedata.normalize("NFKC", value).casefold())


def source_fingerprint(record: dict[str, Any]) -> str:
    if not isinstance(record, dict):
        raise ValueError("invalid_source")
    data = {}
    for key in _FINGERPRINT_FIELDS:
        value = record.get(key, "")
        if not isinstance(value, str) or len(value) > (4000 if key == "text" else 191):
            raise ValueError("invalid_source")
        data[key] = value
    if not data["id"].strip() or not data["text"].strip():
        raise ValueError("invalid_source")
    if (data["id"].casefold().startswith("growth:")
            or data["source"].casefold().startswith("growth")
            or data["evidence_status"].casefold().startswith("growth")):
        raise ValueError("derived_source")
    if data["evidence_status"] in {"pending", "disputed", "superseded", "rejected"}:
        raise ValueError("inactive_source")
    data["timestamp"] = _number(record.get("timestamp", 0), "invalid_source")
    return hashlib.sha256(_json(data).encode("utf-8")).hexdigest()


def _records(records: Any) -> tuple[list[dict], dict[str, dict]]:
    if not isinstance(records, list) or len(records) > MAX_RECORDS:
        raise ValueError("invalid_sources")
    refs, lookup = {}, {}
    for record in records:
        digest = source_fingerprint(record)
        sid = record["id"]
        ref = {"id": sid, "digest": digest, "scope": record.get("scope", ""),
               "sensitivity": record.get("sensitivity", "unknown")}
        if sid in refs and refs[sid] != ref:
            raise ValueError("source_conflict")
        refs[sid], lookup[sid] = ref, record
    return [refs[sid] for sid in sorted(refs)], lookup


def _default(now: float) -> dict:
    return {**{key: [] for key in COLLECTIONS}, "traits": {key: 0.5 for key in TRAITS},
            "mood": {"valence": 0.0, "energy": 0.5}, "_mood_at": now,
            "_trait_day": int(now // 86400), "_trait_spent": {key: 0.0 for key in TRAITS}}


def _decayed(payload: dict, now: float) -> dict:
    factor = 2 ** (-max(0.0, now - payload["_mood_at"]) / HALF_LIFE)
    return {"valence": round(payload["mood"]["valence"] * factor, 8),
            "energy": round(0.5 + (payload["mood"]["energy"] - 0.5) * factor, 8)}


def _view(row: dict, now: float) -> dict:
    payload = json.loads(row["payload"])
    return {**{key: copy.deepcopy(payload[key]) for key in (*COLLECTIONS, "traits")},
            "mood": _decayed(payload, now), "revision": int(row["revision"]),
            "updated": float(row["updated"]),
            **({"emotions": active_emotions(payload, now)} if "emotions" in payload else {})}


def _apply_proposal(before: dict, proposal: Any, allowed: dict[str, dict],
                    records: dict[str, dict], consumed: set[str], now: float, *, review_beliefs=False,
                    emotional_episodes=False, skip_interest_overflow=False):
    fields = {*COLLECTIONS, "traits", "mood"} | ({"emotions"} if emotional_episodes else set())
    if not isinstance(proposal, dict) or set(proposal) - fields:
        raise ValueError("invalid_proposal")
    for category in (*COLLECTIONS, "traits"):
        values = proposal.get(category, [])
        if not isinstance(values, list) or len(values) > (1 if category == "questions" else 2):
            raise ValueError("proposal_limit")
    mood = proposal.get("mood", {})
    if not isinstance(mood, dict):
        raise ValueError("invalid_mood")
    after = copy.deepcopy(before)
    stats = {key: 0 for key in (*COLLECTIONS, "traits", "mood")}
    if emotional_episodes:
        stats["emotions"] = 0
    used, scalar_sources = set(), set()
    day = int(now // 86400)
    if day > after["_trait_day"]:
        after["_trait_day"] = day
        after["_trait_spent"] = {key: 0.0 for key in TRAITS}

    def evidence(item: dict) -> list[dict]:
        ids = item.get("evidence_ids")
        if (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_RECORDS
                or any(not isinstance(sid, str) or sid not in allowed for sid in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError("invalid_evidence")
        return [allowed[sid] for sid in sorted(ids)]

    def merge_sources(old: list[dict], new: list[dict], *, limit=MAX_RECORDS, skip_overflow=False) -> list[dict]:
        combined = {ref["id"]: ref for ref in old}
        for ref in new:
            previous = combined.get(ref["id"])
            if previous is not None and previous != ref:
                raise ValueError("source_revision_conflict")
            combined[ref["id"]] = ref
        if len(combined) > limit:
            # Check every proposed reference for conflicting versions before
            # classifying a valid merge as too large. Evidence is always nonempty.
            if skip_overflow:
                return []
            raise ValueError("source_capacity")
        return [combined[sid] for sid in sorted(combined)]

    def item_fields(item: Any, fields: set[str]) -> None:
        if not isinstance(item, dict) or set(item) != fields:
            raise ValueError("invalid_item")

    def lookup_item(category: str, topic: str):
        normalized = _normalized(topic)
        if not normalized:
            raise ValueError("invalid_topic")
        key = category[0] + ":" + hashlib.sha256(normalized.encode()).hexdigest()[:24]
        current = next((item for item in after[category] if item["key"] == key), None)
        return key, current

    def put(category: str, current: dict | None, item: dict) -> None:
        if current is None:
            if len(after[category]) >= MAX_ITEMS:
                raise ValueError("state_capacity")
            after[category].append(item)
        else:
            after[category][after[category].index(current)] = item
        stats[category] += 1
        used.update(ref["id"] for ref in item["sources"] if ref["id"] in allowed)

    seen_interests = set()
    skipped_interest_overflow = 0
    for item in proposal.get("interests", []):
        item_fields(item, {"topic", "delta", "evidence_ids"})
        topic = _text(item["topic"], 60, "invalid_topic")
        delta = max(-0.1, min(0.1, _number(item["delta"])))
        refs = evidence(item)
        fresh = [ref for ref in refs if ref["id"] not in consumed]
        key, current = lookup_item("interests", topic)
        if key in seen_interests:
            raise ValueError("duplicate_topic")
        seen_interests.add(key)
        previous = current["weight"] if current else 0.0
        weight = round(max(-1.0, min(1.0, previous + delta)), 8)
        if not fresh or weight == previous:
            continue
        merged = merge_sources(current["sources"] if current else [], refs,
                               limit=MAX_INTEREST_SOURCES, skip_overflow=skip_interest_overflow)
        if not merged:
            skipped_interest_overflow += 1
            continue
        changed = {"key": key, "topic": topic, "weight": weight,
                   "sources": merged,
                   "revision": current["revision"] + 1 if current else 1}
        put("interests", current, changed)
        scalar_sources.update(ref["id"] for ref in fresh)

    for category in ("beliefs", "knowledge", "questions"):
        seen = set()
        for item in proposal.get(category, []):
            is_question = category == "questions"
            fields = {"topic", "question", "status", "evidence_ids"} if is_question else {
                "topic", "statement", "confidence", "evidence_ids"}
            if category == "beliefs" and review_beliefs:
                fields |= BELIEF_REVIEW_FIELDS
            item_fields(item, fields)
            topic = _text(item["topic"], 60, "invalid_topic")
            text = _text(item["question"] if is_question else item["statement"], 280)
            confidence = 1.0 if is_question else _number(item["confidence"])
            if not 0.0 <= confidence <= 1.0:
                raise ValueError("invalid_confidence")
            status = item["status"] if is_question else "active"
            if status not in ({"open", "resolved"} if is_question else {"active"}):
                raise ValueError("invalid_status")
            refs = evidence(item)
            key, current = lookup_item(category, topic)
            if key in seen:
                raise ValueError("duplicate_topic")
            seen.add(key)
            if category == "beliefs" and review_beliefs:
                text = _text(item["statement"], 180)
                checked = {**item, "topic": topic, "statement": text, "confidence": confidence}
                changed = assess_belief(current, checked, refs, {sid: records[sid] for sid in allowed},
                                       key=key, merge_sources=merge_sources)
                if changed:
                    put(category, current, changed)
                continue
            if is_question and status == "resolved" and current is None:
                raise ValueError("question_missing")
            old_refs = {ref["id"]: ref for ref in current["sources"]} if current else {}
            if current and all(old_refs.get(ref["id"]) == ref for ref in refs):
                continue
            changed = {"key": key, "topic": topic, "text": text, "confidence": confidence,
                       "status": status, "sources": merge_sources(current["sources"] if current else [], refs),
                       "revision": current["revision"] + 1 if current else 1}
            put(category, current, changed)

    seen_traits = set()
    for item in proposal.get("traits", []):
        item_fields(item, {"trait", "delta", "evidence_ids"})
        trait = item["trait"]
        if not isinstance(trait, str) or trait not in TRAITS or trait in seen_traits:
            raise ValueError("invalid_trait")
        seen_traits.add(trait)
        requested = max(-0.01, min(0.01, _number(item["delta"])))
        refs = evidence(item)
        fresh = [ref for ref in refs if ref["id"] not in consumed]
        texts, origins, identities = set(), set(), set()
        for ref in fresh:
            record = records[ref["id"]]
            text = _normalized(record["text"])
            origin = record.get("origin_key", "")
            identity = (record.get("source", ""), ref["scope"], ref["id"])
            if not text or text in texts or identity in identities or (origin and origin in origins):
                continue
            texts.add(text)
            identities.add(identity)
            if origin:
                origins.add(origin)
        if len(identities) < 3 or day < after["_trait_day"]:
            continue
        allowance = max(0.0, 0.03 - after["_trait_spent"][trait])
        delta = math.copysign(min(abs(requested), allowance), requested)
        previous = after["traits"][trait]
        value = round(max(0.0, min(1.0, previous + delta)), 8)
        actual = abs(value - previous)
        if actual <= 1e-9:
            continue
        after["traits"][trait] = value
        after["_trait_spent"][trait] = round(after["_trait_spent"][trait] + actual, 8)
        used.update(ref["id"] for ref in refs)
        scalar_sources.update(ref["id"] for ref in fresh)
        stats["traits"] += 1

    if mood:
        item_fields(mood, {"valence_delta", "energy_delta", "evidence_ids"})
        refs = evidence(mood)
        fresh = [ref for ref in refs if ref["id"] not in consumed]
        valence = max(-0.1, min(0.1, _number(mood["valence_delta"])))
        energy = max(-0.1, min(0.1, _number(mood["energy_delta"])))
        decayed = _decayed(before, now)
        changed = {"valence": round(max(-1.0, min(1.0, decayed["valence"] + valence)), 8),
                   "energy": round(max(0.0, min(1.0, decayed["energy"] + energy)), 8)}
        if fresh and changed != decayed:
            after["mood"], after["_mood_at"] = changed, now
            used.update(ref["id"] for ref in refs)
            scalar_sources.update(ref["id"] for ref in fresh)
            stats["mood"] = 1
    if emotional_episodes:
        episodes, count, emotion_used, receipts = apply_emotions(before, proposal.get("emotions", []),
            allowed, records, consumed, now)
        if count:
            after["emotions"] = episodes
            stats["emotions"] = count
            used.update(emotion_used)
            scalar_sources.update(receipts)
    return after, stats, used, scalar_sources, skipped_interest_overflow


class GrowthStore:
    def __init__(self, path: Path, *, logger=None, sql_debug: bool = False,
                 mysql_settings: dict | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = (MySQLCompatConnection(**mysql_settings, logger=logger, sql_debug=sql_debug)
                    if mysql_settings is not None else
                    connect_sqlite(str(path), timeout=10, logger=logger, sql_debug=sql_debug))
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA busy_timeout=10000")
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.executescript("""
        CREATE TABLE IF NOT EXISTS growth_state (
          slot INTEGER PRIMARY KEY CHECK(slot=1), payload TEXT NOT NULL,
          revision INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS growth_jobs (
          id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL,
          mode TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
          lease_token TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0,
          next_attempt REAL NOT NULL DEFAULT 0, created REAL NOT NULL,
          updated REAL NOT NULL, error_code TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_growth_jobs_ready
          ON growth_jobs(status,next_attempt,lease_until);
        CREATE TABLE IF NOT EXISTS growth_history (
          revision INTEGER PRIMARY KEY, job_id TEXT NOT NULL UNIQUE,
          payload TEXT NOT NULL, created REAL NOT NULL,
          rolled_back INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS growth_consumed (
          source_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, consumed REAL NOT NULL
        );
        """)
        self.con.commit()

    def close(self) -> None:
        self.con.close()

    def _ensure(self, now: float) -> None:
        with self.con:
            self.con.execute("INSERT OR IGNORE INTO growth_state(slot,payload,revision,updated) "
                             "VALUES (1,?,0,?)", (_json(_default(now)), now))

    def _lock(self) -> None:
        # UPDATE obtains the writer lock before any read on both SQLite and MySQL.
        self.con.execute("UPDATE growth_state SET revision=revision WHERE slot=1")

    def _row(self) -> dict:
        return dict(self.con.execute("SELECT payload,revision,updated FROM growth_state WHERE slot=1").fetchone())

    def state(self, now: float | None = None) -> dict:
        timestamp = _now(now)
        self._ensure(timestamp)
        with self.con:
            return _view(self._row(), timestamp)

    def enqueue(self, records: list[dict], mode: str = "active", now: float | None = None) -> str:
        if mode not in {"active", "observe"}:
            raise ValueError("invalid_mode")
        timestamp = _now(now)
        refs, _ = _records(records)
        if not refs:
            return ""
        job_id = "J" + hashlib.sha256(_json({"sources": refs, "mode": mode}).encode()).hexdigest()
        self._ensure(timestamp)
        with self.con:
            self._lock()
            if self.con.execute("SELECT id FROM growth_jobs WHERE id=?", (job_id,)).fetchone():
                return job_id
            pending = self.con.execute("SELECT COUNT(*) FROM growth_jobs WHERE status IN ('pending','running')").fetchone()[0]
            if pending >= MAX_PENDING:
                raise ValueError("queue_capacity")
            self.con.execute("INSERT INTO growth_jobs(id,payload,status,mode,created,updated) "
                             "VALUES (?,?,'pending',?,?,?)", (job_id, _json(refs), mode, timestamp, timestamp))
        return job_id

    def claim(self, now: float | None = None, lease_seconds: int = 120,
              mode: str | None = None) -> dict | None:
        timestamp = _now(now)
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError("invalid_lease")
        if mode is not None and mode not in {"active", "observe"}:
            raise ValueError("invalid_mode")
        self._ensure(timestamp)
        with self.con:
            self._lock()
            query = ("SELECT * FROM growth_jobs WHERE attempts<3 AND "
                     "((status='pending' AND next_attempt<=?) OR (status='running' AND lease_until<=?))")
            params: list[Any] = [timestamp, timestamp]
            if mode is not None:
                query += " AND mode=?"
                params.append(mode)
            row = self.con.execute(query + " ORDER BY created,id LIMIT 1", tuple(params)).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            self.con.execute("UPDATE growth_jobs SET status='running',lease_token=?,lease_until=?,updated=? WHERE id=?",
                             (token, timestamp + lease_seconds, timestamp, row["id"]))
            return {"id": row["id"], "token": token, "sources": json.loads(row["payload"]),
                    "mode": row["mode"], "attempts": row["attempts"], "lease_until": timestamp + lease_seconds}

    def _leased(self, job_id: str, token: str, now: float) -> dict:
        if not isinstance(job_id, str) or not isinstance(token, str) or not token:
            raise ValueError("invalid_job")
        row = self.con.execute("SELECT * FROM growth_jobs WHERE id=?", (job_id,)).fetchone()
        if (row is None or row["status"] != "running" or row["lease_token"] != token
                or float(row["lease_until"]) <= now):
            raise ValueError("lease_lost")
        return dict(row)

    def _defer(self, job_id: str, token: str, reason: str, retry_at: float | None,
               consume_attempt: bool, now: float | None) -> dict:
        timestamp = _now(now)
        if not isinstance(reason, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", reason):
            raise ValueError("invalid_error_code")
        if type(consume_attempt) is not bool:
            raise ValueError("invalid_attempt_flag")
        if retry_at is not None and _now(retry_at) < timestamp:
            raise ValueError("invalid_retry_time")
        self._ensure(timestamp)
        with self.con:
            self._lock()
            job = self._leased(job_id, token, timestamp)
            attempts = int(job["attempts"]) + int(consume_attempt)
            status = "failed" if attempts >= 3 else "pending"
            retry = (timestamp + 60 * 2 ** max(0, attempts - 1)) if retry_at is None else retry_at
            self.con.execute("UPDATE growth_jobs SET status=?,attempts=?,lease_token='',lease_until=0,"
                             "next_attempt=?,updated=?,error_code=? WHERE id=?",
                             (status, attempts, retry, timestamp, reason, job_id))
            return {"id": job_id, "status": status, "attempts": attempts, "next_attempt": retry}

    def defer(self, job_id: str, token: str, reason: str, retry_at: float,
              consume_attempt: bool = False, *, now: float | None = None) -> dict:
        return self._defer(job_id, token, reason, retry_at, consume_attempt, now)

    def fail(self, job_id: str, token: str, reason: str, *, now: float | None = None) -> dict:
        return self._defer(job_id, token, reason, None, True, now)

    def status(self) -> dict:
        self._ensure(time.time())
        with self.con:
            counts = dict(self.con.execute("SELECT status,COUNT(*) FROM growth_jobs GROUP BY status").fetchall())
            timing = self.con.execute("SELECT MAX(created),MAX(CASE WHEN status='running' THEN updated ELSE 0 END) "
                                      "FROM growth_jobs").fetchone()
            return {"jobs": {name: int(counts.get(name, 0)) for name in ("pending", "running", "done", "failed")},
                    "revision": int(self._row()["revision"]),
                    "consumed": self.con.execute("SELECT COUNT(*) FROM growth_consumed").fetchone()[0],
                    "last_enqueued": float(timing[0] or 0), "last_started": float(timing[1] or 0)}

    def history(self, limit: int = 20) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid_limit")
        with self.con:
            rows = self.con.execute("SELECT * FROM growth_history ORDER BY revision DESC LIMIT ?", (limit,)).fetchall()
            return [{"revision": row["revision"], "job_id": row["job_id"], "created": row["created"],
                     "rolled_back": bool(row["rolled_back"]), **json.loads(row["payload"])} for row in rows]

    def complete(self, job_id: str, token: str, proposal: dict, records: list[dict],
                 now: float | None = None, *, expected_revision: int | None = None, review_beliefs: bool = False,
                 emotional_episodes: bool = False, skip_interest_overflow: bool = False) -> dict:
        timestamp = _now(now)
        if type(review_beliefs) is not bool:
            raise ValueError("invalid_belief_review_mode")
        if type(emotional_episodes) is not bool:
            raise ValueError("invalid_emotional_episodes_mode")
        if type(skip_interest_overflow) is not bool:
            raise ValueError("invalid_interest_overflow_mode")
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
            raise ValueError("invalid_revision")
        refs, lookup = _records(records)
        ref_map = {ref["id"]: ref for ref in refs}
        self._ensure(timestamp)
        with self.con:
            self._lock()
            job = self._leased(job_id, token, timestamp)
            queued = json.loads(job["payload"])
            if any(ref_map.get(ref["id"]) != ref for ref in queued):
                raise ValueError("source_changed")
            # Extra resolved records are not evidence for this queued job.
            allowed = {ref["id"]: ref for ref in queued}
            old = self._row()
            if expected_revision is not None and old["revision"] != expected_revision:
                raise ValueError("revision_conflict")
            before = json.loads(old["payload"])
            receipt_ids = set(allowed)
            if emotional_episodes:
                for sid in allowed:
                    receipt_ids.update(emotion_receipts(lookup[sid]))
            consumed = {row[0] for row in self.con.execute(
                "SELECT source_id FROM growth_consumed WHERE source_id IN (" + ",".join("?" for _ in receipt_ids) + ")",
                tuple(sorted(receipt_ids))).fetchall()}
            after, stats, used, scalar_sources, skipped = _apply_proposal(
                before, proposal, allowed, lookup, consumed, timestamp, review_beliefs=review_beliefs,
                emotional_episodes=emotional_episodes, skip_interest_overflow=skip_interest_overflow)
            changed = any(stats.values()) and job["mode"] == "active"
            revision = int(old["revision"])
            if changed:
                revision += 1
                self.con.execute("UPDATE growth_state SET payload=?,revision=?,updated=? WHERE slot=1",
                                 (_json(after), revision, timestamp))
                payload = {"kind": "growth", "before": before, "after": after,
                           "sources": [allowed[key] for key in sorted(used)]}
                self.con.execute("INSERT INTO growth_history(revision,job_id,payload,created) VALUES (?,?,?,?)",
                                 (revision, job_id, _json(payload), timestamp))
                for sid in scalar_sources:
                    self.con.execute("INSERT OR IGNORE INTO growth_consumed(source_id,job_id,consumed) VALUES (?,?,?)",
                                     (sid, job_id, timestamp))
            self.con.execute("UPDATE growth_jobs SET status='done',lease_token='',lease_until=0,"
                             "updated=?,error_code='' WHERE id=?", (timestamp, job_id))
            if skipped:
                stats["skipped_interest_overflow"] = skipped
            return {**stats, "revision": revision, "changed": bool(changed), "observed": job["mode"] == "observe"}

    def rollback(self, expected_revision: int, now: float | None = None) -> dict:
        timestamp = _now(now)
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("invalid_revision")
        self._ensure(timestamp)
        with self.con:
            self._lock()
            current = self._row()
            if current["revision"] != expected_revision:
                raise ValueError("revision_conflict")
            history = self.con.execute("SELECT * FROM growth_history WHERE revision=?", (expected_revision,)).fetchone()
            if history is None or history["rolled_back"]:
                raise ValueError("nothing_to_rollback")
            record = json.loads(history["payload"])
            if record["kind"] != "growth":
                raise ValueError("nothing_to_rollback")
            restored = copy.deepcopy(record["before"])
            existing = json.loads(current["payload"])
            # Undoing visible traits does not replenish their same-day allowance.
            if existing["_trait_day"] >= restored["_trait_day"]:
                restored["_trait_day"] = existing["_trait_day"]
                restored["_trait_spent"] = dict(existing["_trait_spent"])
            revision = expected_revision + 1
            self.con.execute("UPDATE growth_state SET payload=?,revision=?,updated=? WHERE slot=1",
                             (_json(restored), revision, timestamp))
            self.con.execute("UPDATE growth_history SET rolled_back=1 WHERE revision=?", (expected_revision,))
            self.con.execute("UPDATE growth_jobs SET status='pending',lease_token='',lease_until=0,"
                             "next_attempt=?,updated=?,error_code='state_rolled_back' WHERE status='running'",
                             (timestamp, timestamp))
            rollback = {"kind": "rollback", "before": existing, "after": restored,
                        "sources": record["sources"], "reverted_revision": expected_revision}
            self.con.execute("INSERT INTO growth_history(revision,job_id,payload,created,rolled_back) VALUES (?,?,?,?,1)",
                             (revision, "rollback:" + str(expected_revision), _json(rollback), timestamp))
            return _view({"payload": _json(restored), "revision": revision, "updated": timestamp}, timestamp)

    def retire(self, key: str, expected_revision: int, now: float | None = None) -> dict:
        """Explicit administrator retirement of one stale inference, never a model tool."""
        timestamp = _now(now)
        if not isinstance(key, str) or not re.fullmatch(r"[ibkqe]:[0-9a-f]{24}", key):
            raise ValueError("invalid_key")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("invalid_revision")
        self._ensure(timestamp)
        with self.con:
            self._lock()
            current = self._row()
            if current["revision"] != expected_revision:
                raise ValueError("revision_conflict")
            before = json.loads(current["payload"])
            after = copy.deepcopy(before)
            removed = None
            for category in (*COLLECTIONS, "emotions"):
                item = next((item for item in after.get(category, []) if item["key"] == key), None)
                if item is not None:
                    removed = item
                    after[category].remove(item)
                    break
            if removed is None:
                raise ValueError("item_missing")
            revision = expected_revision + 1
            self.con.execute("UPDATE growth_state SET payload=?,revision=?,updated=? WHERE slot=1",
                             (_json(after), revision, timestamp))
            audit = {"kind": "retire", "before": before, "after": after,
                     "sources": removed["sources"], "retired_key": key}
            self.con.execute("INSERT INTO growth_history(revision,job_id,payload,created) VALUES (?,?,?,?)",
                             (revision, "retire:" + str(revision) + ":" + key, _json(audit), timestamp))
            self.con.execute("UPDATE growth_jobs SET status='pending',lease_token='',lease_until=0,"
                             "next_attempt=?,updated=?,error_code='state_retired' WHERE status='running'",
                             (timestamp, timestamp))
            return _view({"payload": _json(after), "revision": revision, "updated": timestamp}, timestamp)
