"""Bounded read-only retrieval for one mind across all conversation sources.

``scope`` is provenance, never a retrieval permission. Returned records are
internal candidates; the expression layer must decide what an audience may hear.
"""
from __future__ import annotations

import math
import re
import time
import unicodedata
from dataclasses import dataclass, replace
from typing import Any

from .sql_logging import SQLMySQLCursor
from .mysql_compat import MySQLCompatConnection

MAX_RESULTS = 50
MAX_SCAN = 1000
MAX_QUERY_CHARS = 1200
MAX_QUERY_TERMS = 24
MAX_TEXT_CHARS = 600
MAX_RESOLVE_IDS = 128
MAX_GROWTH_ITEMS = 128
MAX_GROWTH_SOURCES = 24
_DAY = 86400
_STOP_TERMS = {"这个", "那个", "什么", "怎么", "一下", "一个", "我们", "你们", "他们", "可以", "请问"}
_CONFIDENTIAL = re.compile(
    r"保密|秘密|不要(?:告诉|公开|转告|透露)|别(?:告诉|说出去|公开|转告|透露)|"
    r"仅限.{0,8}(?:知道|查看)|\bconfidential\b|\bsecret\b|do not share|don't share", re.I)


@dataclass(frozen=True)
class _Source:
    store: Any
    name: str
    table: str
    columns: tuple[str, ...]
    search: tuple[str, ...]
    timestamp: str
    subject: str
    where: str = "1=1"
    parameters: tuple[Any, ...] = ()


def _terms(query: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", query[:MAX_QUERY_CHARS]).casefold()
    found: list[str] = []
    for part in re.findall(r"[\u3400-\u9fff]+|[a-z0-9_]+", normalized):
        pieces = ([part[i:i+2] for i in range(len(part)-1)]
                  if re.fullmatch(r"[\u3400-\u9fff]+", part) else [part])
        for token in pieces:
            if token not in _STOP_TERMS and token not in found:
                found.append(token)
            if len(found) >= MAX_QUERY_TERMS:
                return found
    return found


def _text(value: Any, limit: int = MAX_TEXT_CHARS) -> str:
    return " ".join(str(value or "").split())[:limit]


def _stamp(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return result if math.isfinite(result) and result > 0 else 0.0


def _sensitivity(scope: str, text: str) -> str:
    # Source metadata is not a semantic proof that sharing is safe.
    if _CONFIDENTIAL.search(text):
        return "confidential"
    if scope == "public" or scope.startswith("public:"):
        return "public"
    if scope.startswith(("private:", "group:")):
        return "personal"
    return "unknown"


def _sources(brain: Any, mind: Any, cognition: Any, stories: Any, now: float, tools: Any = None) -> list[_Source]:
    sources: list[_Source] = []
    if brain is not None:
        sources.extend((
            _Source(brain, "brain_fact", "brain_facts",
                    ("id", "scope", "subject_id", "attribute", "value", "updated"),
                    ("attribute", "value", "subject_id"), "updated", "subject_id",
                    "status='active' AND (valid_until IS NULL OR valid_until>?)", (now,)),
            _Source(brain, "brain_event", "brain_events",
                    ("id", "scope", "actor_id", "kind", "summary", "happened", "created"),
                    ("summary", "actor_id"), "CASE WHEN happened>0 THEN happened ELSE created END", "actor_id"),
            _Source(brain, "brain_goal", "brain_goals",
                    ("id", "scope", "owner_id", "title", "criterion", "next_action", "updated"),
                    ("title", "criterion", "owner_id"), "updated", "owner_id", "status='open'"),
        ))
    if mind is not None:
        sources.append(_Source(mind, "mind", "mind_notes",
            ("owner_id", "note_key", "kind", "text", "last_seen"),
            ("text", "owner_id"), "last_seen", "owner_id",
            "status='active' AND ((kind='open_topic' AND last_seen>=?) "
            "OR (kind<>'open_topic' AND last_seen>=?))", (now-30*_DAY, now-120*_DAY)))
    if cognition is not None:
        sources.append(_Source(cognition, "cognition", "cognitive_goals",
            ("goal_id", "topic", "question", "observation", "uncertainty", "next_action", "updated_at"),
            ("topic", "question", "observation"), "updated_at", "", "status='open'"))
    if stories is not None:
        sources.append(_Source(stories, "story", "stories",
            ("key", "source", "title", "summary", "url", "published_at", "first_seen"),
            ("title", "summary", "source"), "first_seen", "",
            "published_at IS NULL OR published_at<=?", (now+21600,)))
    if tools is not None:
        sources.append(_Source(tools, "tool", "tool_observations",
            ("id", "provider", "text", "scope", "subject_id", "sensitivity", "url", "origin_key", "timestamp", "active"),
            ("text", "subject_id", "provider"), "timestamp", "subject_id", "active=1"))
    return sources


def _read(source: _Source, terms: list[str], owner_id: str, limit: int) -> list[dict[str, Any]]:
    """Only SELECT, parameterized values, no connection or row-factory mutation."""
    table = source.table
    mysql_story = source.name == "story" and hasattr(source.store, "tables")
    if mysql_story:
        table = source.store.tables["stories"]
        if not isinstance(table, str) or not re.fullmatch(r"`[A-Za-z][A-Za-z0-9_]{0,63}`", table):
            raise ValueError("invalid story table name")
    columns = ",".join("`key`" if column == "key" else column for column in source.columns)
    parameters = list(source.parameters)
    weights: list[str] = []
    for term in terms:
        clause = " OR ".join(f"LOWER({column}) LIKE ? ESCAPE '~'" for column in source.search)
        weights.append(f"CASE WHEN ({clause}) THEN 1 ELSE 0 END")
        pattern = "%" + term.replace("~", "~~").replace("%", "~%").replace("_", "~_") + "%"
        parameters.extend([pattern] * len(source.search))
    if owner_id and source.subject:
        weights.append(f"CASE WHEN {source.subject}=? THEN 0.2 ELSE 0 END")
        parameters.append(owner_id)
    order = ("(" + "+".join(weights) + ") DESC," if weights else "")
    identity = "owner_id,note_key" if source.name == "mind" else (
        "`key`" if source.name == "story" else source.columns[0])
    sql = (f"SELECT {columns} FROM {table} WHERE ({source.where}) "
           f"ORDER BY {order}{source.timestamp} DESC,{identity} LIMIT ?")
    parameters.append(limit)
    if mysql_story:
        source.store._ping()
        with SQLMySQLCursor(source.store.con.cursor(), source.store._sql_audit) as cursor:
            cursor.execute(sql.replace("?", "%s"), tuple(parameters))
            rows = cursor.fetchall()
    elif isinstance(source.store.con, MySQLCompatConnection):
        # Dedicated autocommit reads see newly committed revisions without
        # committing or rolling back the ledger's independent writer transaction.
        rows = source.store.con.fetch_readonly(sql, tuple(parameters))
    else:
        cursor = source.store.con.execute(sql, tuple(parameters))
        try:
            rows = cursor.fetchall()
        finally:
            cursor.close()
    return [dict(row) if hasattr(row, "keys") else dict(zip(source.columns, row)) for row in rows]


def _record(source: str, row: dict[str, Any]) -> dict[str, Any] | None:
    if source == "tool":
        from .tool_store import observation_record
        return observation_record(row)
    scope = _text(row.get("scope"), 100) if source.startswith("brain_") else "public"
    subject = _text(row.get("subject_id") or row.get("actor_id") or row.get("owner_id"), 100)
    stamp = _stamp(row.get("updated") or row.get("updated_at"))
    if source == "brain_fact":
        identity, kind, status = "brain:fact:"+str(row["id"]), "fact", "active"
        text = _text(row["attribute"])+"："+_text(row["value"])
    elif source == "brain_event":
        identity, kind, status = "brain:event:"+str(row["id"]), "event", "recorded_event"
        text = _text(row["summary"])
        stamp = _stamp(row["happened"]) or _stamp(row["created"])
    elif source == "brain_goal":
        identity, kind, status = "brain:goal:"+str(row["id"]), "goal", "open_goal"
        text = _text(row["title"])+"；完成条件："+_text(row["criterion"])+"；下一步："+_text(row["next_action"])
    elif source == "mind":
        identity = "mind:note:"+str(row["owner_id"])+":"+str(row["note_key"])
        kind, status = _text(row["kind"]), "active"
        scope, text, stamp = "private:"+subject, _text(row["text"]), _stamp(row["last_seen"])
    elif source == "cognition":
        identity, kind, status = "cognition:goal:"+str(row["goal_id"]), "observation", "unverified_observation"
        text = (_text(row["topic"])+"："+_text(row["question"])+"；观察："+_text(row["observation"])
                +"；尚不确定："+_text(row["uncertainty"])+"；候选下一步："+_text(row["next_action"]))
    else:
        identity, kind, status = "story:"+str(row["key"]), "story", "source_metadata"
        text = _text(row["title"])+"；"+_text(row["summary"])
        stamp = _stamp(row["published_at"]) or _stamp(row["first_seen"])
    if not text.strip("：； "):
        return None
    record = {"id": identity, "source": source, "kind": kind, "scope": scope,
              "subject_id": subject, "text": text[:MAX_TEXT_CHARS], "timestamp": stamp,
              "sensitivity": _sensitivity(scope, text), "evidence_status": status}
    if source == "brain_event":
        record["event_kind"] = _text(row["kind"], 80)
    if source == "story":
        record["url"], record["source_name"] = _text(row["url"], 1000), _text(row["source"], 150)
    return record


def collect_memories(*, brain=None, mind=None, cognition=None, stories=None, tools=None,
                     owner_id: str = "", query: str = "", limit: int = 12,
                     scan_limit: int = 200, now: float | None = None) -> list[dict[str, Any]]:
    """Return global internal candidates, retaining provenance and review status.

    ``owner_id`` is a small relationship ranking weight, not a filter. ``scan_limit``
    caps total rows fetched across sources (hard maximum 1000); it does not cap
    rows examined by a database execution plan. SQL orders keyword matches before
    recency, so older relevant records can be found beyond the newest rows.
    No record or connection mode is changed. Database errors are propagated;
    callers must explicitly handle unavailable storage rather than trust silently
    incomplete results. These records are not automatically safe to publish.
    """
    if type(limit) is not int or type(scan_limit) is not int:
        raise ValueError("limit and scan_limit must be integers")
    if not isinstance(query, str) or not isinstance(owner_id, str):
        raise ValueError("query and owner_id must be strings")
    if limit <= 0 or scan_limit <= 0:
        return []
    current = time.time() if now is None else now
    if isinstance(current, bool) or not isinstance(current, (int, float)) or not math.isfinite(current):
        raise ValueError("now must be a finite timestamp")
    limit, remaining = min(limit, MAX_RESULTS), min(scan_limit, MAX_SCAN)
    terms = _terms(query)
    sources = _sources(brain, mind, cognition, stories, float(current), tools)
    records: dict[str, dict[str, Any]] = {}
    for index, source in enumerate(sources):
        if remaining <= 0:
            break
        budget = max(1, remaining // (len(sources)-index))
        rows = _read(source, terms, owner_id[:100], budget)
        remaining -= len(rows)
        for row in rows:
            record = _record(source.name, row)
            if record is None:
                continue
            searchable = unicodedata.normalize("NFKC", record["text"]+" "+record["subject_id"]).casefold()
            matches = sum(term in searchable for term in terms)
            relevance = matches / len(terms) if terms else 0.0
            age_days = max(0.0, float(current)-record["timestamp"]) / _DAY
            recency = 1 / (1+age_days/30) if record["timestamp"] else 0.0
            owner_weight = 0.2 if owner_id and record["subject_id"] == owner_id else 0.0
            record["score"] = round(10*relevance + recency + owner_weight, 6)
            records[record["id"]] = record
    return sorted(records.values(), key=lambda item: (-item["score"], -item["timestamp"], item["id"]))[:limit]


def _source_id(identity: Any) -> tuple[str, tuple[str, ...]]:
    """Parse only identifiers emitted for original records, never derived memory."""
    if not isinstance(identity, str) or len(identity) > 240:
        raise ValueError("invalid original memory id")
    if re.fullmatch(r"tool:observation:[0-9a-f]{64}", identity):
        return "tool", (identity,)
    token = r"([A-Za-z0-9][A-Za-z0-9_-]{0,127})"
    for prefix, source in (("brain:fact:", "brain_fact"),
                           ("brain:event:", "brain_event"),
                           ("brain:goal:", "brain_goal"),
                           ("cognition:goal:", "cognition"),
                           ("story:", "story")):
        match = re.fullmatch(re.escape(prefix) + token, identity)
        if match:
            return source, (match.group(1),)
    match = re.fullmatch(r"mind:note:([0-9]{5,20}):" + token, identity)
    if match:
        return "mind", (match.group(1), match.group(2))
    raise ValueError("invalid original memory id")


def resolve_memories(ids, *, brain=None, mind=None, cognition=None, stories=None, tools=None,
                     now: float | None = None) -> dict[str, dict[str, Any]]:
    """Re-read exact original IDs and their current validity, without a scope filter.

    Invalid IDs, derived IDs, unbounded iterables and batches over 128 are rejected.
    Well-formed IDs whose source was deleted, expired, closed or invalidated are
    absent from the result. Database failures propagate. This is the same internal
    shared memory as ``collect_memories``, not authorization to publish a record.
    """
    if not isinstance(ids, (list, tuple)) or len(ids) > MAX_RESOLVE_IDS:
        raise ValueError("ids must be a list or tuple of at most 128 original memory ids")
    parsed = {identity: _source_id(identity) for identity in ids}
    current = time.time() if now is None else now
    if isinstance(current, bool) or not isinstance(current, (int, float)) or not math.isfinite(current):
        raise ValueError("now must be a finite timestamp")
    found: dict[str, dict[str, Any]] = {}
    for source in _sources(brain, mind, cognition, stories, float(current), tools):
        identities = [parts for name, parts in parsed.values() if name == source.name]
        if not identities:
            continue
        if source.name == "mind":
            selection = " OR ".join("(owner_id=? AND note_key=?)" for _ in identities)
        else:
            column = "`key`" if source.name == "story" else source.columns[0]
            selection = column + " IN (" + ",".join("?" for _ in identities) + ")"
        parameters = tuple(value for parts in identities for value in parts)
        exact = replace(source, where=f"({source.where}) AND ({selection})",
                        parameters=source.parameters+parameters)
        for row in _read(exact, [], "", len(identities)):
            record = _record(source.name, row)
            if record is not None and record["id"] in parsed:
                record["score"] = 0.0
                found[record["id"]] = record
    return {identity: found[identity] for identity in parsed if identity in found}


def _derived_sources(refs: Any, resolved: dict, fingerprint) -> tuple[list[dict], list[dict]] | None:
    if not isinstance(refs, list) or not 1 <= len(refs) <= MAX_GROWTH_SOURCES:
        return None
    records, saved = [], []
    seen: set[str] = set()
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) != {"id", "digest", "scope", "sensitivity"}:
            return None
        identity, digest = ref["id"], ref["digest"]
        try:
            expected_source, _ = _source_id(identity)
        except ValueError:
            return None
        if identity in seen or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            return None
        seen.add(identity)
        source = resolved.get(identity)
        if (not isinstance(source, dict) or source.get("id") != identity
                or source.get("source") != expected_source
                or str(source.get("evidence_status", "")).startswith("growth")
                or not isinstance(ref["scope"], str) or not isinstance(ref["sensitivity"], str)
                or not isinstance(source.get("subject_id"), str)
                or source.get("scope") != ref["scope"]
                or source.get("sensitivity") != ref["sensitivity"]
                or ref["sensitivity"] not in {"public", "personal", "unknown", "confidential"}):
            return None
        try:
            if fingerprint(source) != digest:
                return None
        except (TypeError, ValueError, OverflowError):
            return None
        records.append(source)
        saved.append(dict(ref))
    return records, saved


def derived_memories(state, resolved_sources, *, fingerprint, query: str = "",
                     limit: int = 12) -> list[dict[str, Any]]:
    """Build inferred internal memories only while every original source still matches.

    The supplied fingerprint function is the growth store's single canonical
    implementation. A derived record can never serve as its own evidence. Source
    confidentiality is inherited even when the text is paraphrased; scope remains
    provenance, never a per-conversation retrieval filter. The outward expression
    layer must still review whether and how an inferred item may be expressed.
    """
    if type(limit) is not int or not isinstance(query, str):
        raise ValueError("limit must be an integer and query must be a string")
    if not callable(fingerprint):
        raise ValueError("fingerprint must be callable")
    if limit <= 0 or not isinstance(state, dict) or not isinstance(resolved_sources, dict):
        return []
    categories = (("interests", "interest", "自我兴趣", "i"),
                  ("beliefs", "belief", "主观看法", "b"),
                  ("knowledge", "knowledge", "待验证知识", "k"),
                  ("questions", "question", "待解问题", "q"))
    terms = _terms(query)
    ranked: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    sensitivity_rank = {"public": 0, "personal": 1, "unknown": 2, "confidential": 3}
    inspected = 0
    for category, kind, label, prefix in categories:
        items = state.get(category, [])
        if not isinstance(items, list):
            continue
        for item in items[:max(0, MAX_GROWTH_ITEMS-inspected)]:
            inspected += 1
            if not isinstance(item, dict):
                continue
            key, topic, revision = item.get("key"), item.get("topic"), item.get("revision")
            if (not isinstance(key, str) or not re.fullmatch(prefix+r":[0-9a-f]{24}", key)
                    or not isinstance(topic, str) or not topic.strip() or len(topic) > 180
                    or type(revision) is not int or revision <= 0):
                continue
            identity = "growth:"+kind+":"+key
            if identity in ranked:
                duplicates.add(identity)
                continue
            if kind != "interest" and item.get("status") != ("open" if kind == "question" else "active"):
                continue
            if kind == "interest" and item.get("status") not in (None, "active"):
                continue
            strength = item.get("weight" if kind == "interest" else "confidence")
            if (isinstance(strength, bool) or not isinstance(strength, (int, float))
                    or not math.isfinite(strength)
                    or not (-1 if kind == "interest" else 0) <= strength <= 1):
                continue
            text = item.get("text", "")
            if not isinstance(text, str) or len(text) > MAX_TEXT_CHARS or (kind != "interest" and not text.strip()):
                continue
            source_data = _derived_sources(item.get("sources"), resolved_sources, fingerprint)
            if source_data is None:
                continue
            sources, refs = source_data
            scopes = {source.get("scope", "") for source in sources}
            subjects = {source.get("subject_id", "") for source in sources}
            scope = next(iter(scopes)) if len(scopes) == 1 else "global:derived"
            subject = next(iter(subjects)) if len(subjects) == 1 else ""
            sensitivity = max((source["sensitivity"] for source in sources), key=sensitivity_rank.get)
            if sensitivity == "public":
                scope = "public:derived"
            content = label+"："+_text(topic, 180)
            if kind == "interest":
                content += f"（偏好强度 {strength:+.2f}，负值表示不喜欢）"
            if text:
                content += "；"+_text(text)
            if _CONFIDENTIAL.search(content):
                sensitivity = "confidential"
            stamp = max((_stamp(source.get("timestamp")) for source in sources), default=0.0)
            searchable = unicodedata.normalize("NFKC", topic+" "+text).casefold()
            relevance = sum(token in searchable for token in terms) / len(terms) if terms else 0.0
            ranked[identity] = {
                "id": identity, "source": "growth", "kind": kind,
                "scope": scope, "subject_id": subject, "text": content[:MAX_TEXT_CHARS],
                "timestamp": stamp, "sensitivity": sensitivity, "evidence_status": "inferred",
                "sources": refs, "revision": revision, "topic": _text(topic, 180),
                "score": round(10*relevance + abs(float(strength)), 6),
            }
            if kind == "interest":
                ranked[identity]["weight"] = float(strength)
    for identity in duplicates:
        ranked.pop(identity, None)
    return sorted(ranked.values(), key=lambda item: (-item["score"], -item["timestamp"], item["id"]))[:min(limit, MAX_RESULTS)]
