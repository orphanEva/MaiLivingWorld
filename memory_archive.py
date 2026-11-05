"""Bounded active windows and searchable history without new evidence or erasure.

Archive metadata lives in the existing canonical JSON payload. Semantic status,
revision, source references and receipt tombstones are never rewritten by aging.
All SQL identifiers are fixed; text searches use escaped, bound parameters.
"""
from __future__ import annotations

import json
import re

from .continuity_store import _int, _json, _owner, _text
from .unified_memory import _terms


TABLES = {"advice_items", "repair_items"}
ARCHIVE_MARKER = '"memory_tier":"archive"'
ACTIVE_LIMIT = 64
PAGE_SIZE = 20


def _table(table):
    if table not in TABLES:
        raise ValueError("invalid_archive_table")
    return table


def tier(row):
    if row.get("status") == "forgotten":
        return "tombstone"
    return "archive" if row.get("memory_tier") == "archive" else "active"


def make_room(con, table, owner, *, enabled, now):
    """Called only inside the store's owner-locked write transaction."""
    table, owner = _table(table), _owner(owner)
    if type(enabled) is not bool:
        raise ValueError("invalid_archive_mode")
    if not enabled:
        count = con.execute("SELECT COUNT(*) AS n FROM "+table+" WHERE owner_id=?", (owner,)).fetchone()["n"]
        if count >= ACTIVE_LIMIT:
            raise ValueError("memory_capacity")
        return []
    where = "owner_id=? AND status<>'forgotten' AND INSTR(payload,?)=0"
    count = con.execute("SELECT COUNT(*) AS n FROM "+table+" WHERE "+where, (owner, ARCHIVE_MARKER)).fetchone()["n"]
    needed = max(0, count-ACTIVE_LIMIT+1)
    if not needed:
        return []
    rows = con.execute("SELECT id,status,payload FROM "+table+" WHERE "+where+" ORDER BY updated,id LIMIT ?",
                       (owner, ARCHIVE_MARKER, ACTIVE_LIMIT)).fetchall()
    selected = []
    for row in rows:
        payload = json.loads(row["payload"])
        # A still-deliverable draft must remain visible to the real send hook.
        if row["status"] == "draft" and payload.get("expires", 0) > now:
            continue
        selected.append((row["id"], payload))
        if len(selected) == needed:
            break
    if len(selected) < needed:
        raise ValueError("memory_capacity")
    for ident, payload in selected:
        payload.update(memory_tier="archive", archived_at=now)
        con.execute("UPDATE "+table+" SET payload=? WHERE owner_id=? AND id=?", (_json(payload), owner, ident))
    return [ident for ident, _ in selected]


def counts(read, table, owner):
    table, owner = _table(table), _owner(owner)
    row = read("SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN status='forgotten' THEN 1 ELSE 0 END) AS tombstones, "
        "SUM(CASE WHEN status<>'forgotten' AND INSTR(payload,?)>0 THEN 1 ELSE 0 END) AS archived "
        "FROM "+table+" WHERE owner_id=?", (ARCHIVE_MARKER, owner))[0]
    total, tombstones, archived = (int(row[key] or 0) for key in ("total", "tombstones", "archived"))
    return {"total": total, "active": total-tombstones-archived, "archived": archived,
            "tombstones": tombstones, "active_limit": ACTIVE_LIMIT}


def _pattern(value):
    return "%"+value.replace("~", "~~").replace("%", "~%").replace("_", "~_")+"%"


def select(read, table, owner, *, query="", limit=PAGE_SIZE, offset=0, recall=False, archived=True):
    """An empty recall uses only the active window; search can page all history."""
    table, owner = _table(table), _owner(owner)
    _text(query, 1200, blank=True)
    _int(limit, 1, ACTIVE_LIMIT+1), _int(offset, 0, 1000000)
    terms = _terms(query)
    fields = ("topic", "advice", "feedback") if table == "advice_items" else ("old_quote", "correction_quote")
    clause = "("+" OR ".join("LOWER(JSON_EXTRACT(payload,'$."+field+"')) LIKE ? ESCAPE '~'" for field in fields)+")"
    clauses = [clause for _ in terms]
    patterns = [_pattern(term) for term in terms for _ in fields]
    ids = list(dict.fromkeys(re.findall(r"(?:advice|repair):item:[0-9a-f]{64}", query)))[:8]
    if ids:
        clauses.append("id IN ("+",".join("?" for _ in ids)+")")
        patterns += ids
    matches = " OR ".join(clauses)
    where, args = "owner_id=?", [owner]
    if recall:
        where += " AND status='active'"
        if archived and clauses:
            where += " AND (INSTR(payload,?)=0 OR ("+matches+"))"
            args += [ARCHIVE_MARKER, *patterns]
        else:
            where += " AND INSTR(payload,?)=0"
            args.append(ARCHIVE_MARKER)
    elif clauses:
        where += " AND ("+matches+")"
        args += patterns
    weights = "+".join("CASE WHEN "+part+" THEN 1 ELSE 0 END" for part in clauses)
    order = "("+weights+") DESC," if weights else ""
    args += [*patterns, limit, offset] if weights else [limit, offset]
    return read("SELECT * FROM "+table+" WHERE "+where+" ORDER BY "+order+"updated DESC,id LIMIT ? OFFSET ?", tuple(args))
