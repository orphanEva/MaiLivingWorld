"""Bounded opinion changes supported by original observations, never self-evidence."""
from __future__ import annotations

import copy
import hashlib
import re
import unicodedata


FIELDS = {"relation", "previous_key", "previous_revision", "quotes"}
RELATIONS = {"new", "reinforce", "refine", "challenge"}


def normalized(value):
    return re.sub(r"\W+", "", unicodedata.normalize("NFKC", value).casefold())


def identities(record):
    content = hashlib.sha256(normalized(record["text"]).encode()).hexdigest()
    origin = record.get("origin_key") or record.get("source", "")+":"+record["id"]
    return hashlib.sha256(origin.encode()).hexdigest(), content


def validate_review(item, records=None, state=None):
    """Validate literal citations; state supplied by the caller is the comparison baseline."""
    if (not isinstance(item, dict) or not isinstance(item.get("relation"), str)
            or item["relation"] not in RELATIONS
            or not isinstance(item.get("previous_key"), str)
            or type(item.get("previous_revision")) is not int
            or not isinstance(item.get("quotes"), list) or not 1 <= len(item["quotes"]) <= 3):
        return False
    if item["relation"] == "new":
        if item["previous_key"] or item["previous_revision"] != 0:
            return False
    elif not re.fullmatch(r"b:[0-9a-f]{24}", item["previous_key"]) or item["previous_revision"] < 1:
        return False
    seen = set()
    for quote in item["quotes"]:
        if (not isinstance(quote, dict) or set(quote) != {"source_id", "quote"}
                or not isinstance(quote["source_id"], str) or quote["source_id"] not in item.get("evidence_ids", [])
                or not isinstance(quote["quote"], str) or not 3 <= len(quote["quote"]) <= 180
                or any(ord(c) < 32 for c in quote["quote"]) or quote["source_id"] in seen):
            return False
        if records is not None:
            source = records.get(quote["source_id"])
            if not source or source["text"].count(quote["quote"]) != 1:
                return False
        seen.add(quote["source_id"])
    if seen != set(item.get("evidence_ids", [])):
        return False
    if state is not None:
        old = next((row for row in state.get("beliefs", [])
                    if normalized(row["topic"]) == normalized(item.get("topic", ""))), None)
        if item["relation"] == "new":
            return old is None
        if (not old or old["key"] != item["previous_key"] or old["revision"] != item["previous_revision"]):
            return False
        if item["relation"] == "reinforce" and item.get("statement") != old["text"]:
            return False
    return True


def assessment_view(item):
    saved = item.get("assessment")
    if not isinstance(saved, dict):
        return {}
    return {"status": saved.get("status", "tentative"),
            "candidate_text": str(saved.get("candidate_text", ""))[:180],
            "candidate_relation": saved.get("candidate_relation", ""),
            "independent_evidence_count": min(3, len(saved.get("candidate_origins", [])))}


def assess(current, item, refs, records, *, key, merge_sources):
    """Return one persistent item, or None when repetition supplies no new evidence."""
    baseline = {"beliefs": [current] if current else []}
    if not validate_review(item, records, baseline):
        raise ValueError("invalid_belief_review")
    previous_refs = {r["id"]: r for r in current["sources"]} if current else {}
    # Comparison is meaningful only when every old dependency is present in this
    # exact job. Hidden/private old evidence may never be attributed to new roots.
    if current and any(r["id"] not in records for r in current["sources"]):
        raise ValueError("belief_context_missing")
    from .growth_store import source_fingerprint
    if current and any(source_fingerprint(records[r["id"]]) != r["digest"] for r in current["sources"]):
        raise ValueError("belief_context_changed")
    known_origins, known_contents = set(), set()
    if current:
        for ref in current["sources"]:
            origin, content = identities(records[ref["id"]])
            known_origins.add(origin)
            known_contents.add(content)
    independent = []
    for ref in refs:
        if ref["id"] in previous_refs:
            continue
        origin, content = identities(records[ref["id"]])
        if origin in known_origins or content in known_contents:
            continue
        independent.append((ref, origin, content))
        known_origins.add(origin)
        known_contents.add(content)
    if not independent:
        return None
    previous = current.get("assessment", {}) if current else {}
    assessment = {"status": "tentative", "candidate_text": "", "candidate_relation": "", "candidate_sources": [],
                  "candidate_origins": [], "candidate_contents": [],
                  "last_previous_text": previous.get("last_previous_text", "")}
    if current is None:
        return {"key": key, "topic": item["topic"], "text": item["statement"],
                "confidence": min(.6, item["confidence"]), "status": "active", "sources": refs,
                "revision": 1, "assessment": assessment}
    changed = copy.deepcopy(current)
    changed["revision"] += 1
    changed["sources"] = merge_sources(current["sources"], refs)
    confidence = float(current["confidence"])
    requested = float(item["confidence"])
    bounded = round(max(0., min(1., max(confidence-.1, min(confidence+.1, requested)))), 8)
    if item["relation"] == "reinforce":
        # Support for the held position does not erase an unresolved counterview.
        if previous.get("candidate_text"):
            changed["assessment"] = copy.deepcopy(previous)
            changed["confidence"] = min(confidence, bounded)
        else:
            assessment["status"] = "considered"
            changed.update(confidence=bounded, assessment=assessment)
        return changed
    if item["statement"] == current["text"]:
        raise ValueError("belief_candidate_unchanged")
    continuing = (previous.get("candidate_text") == item["statement"]
                  and previous.get("candidate_relation") == item["relation"])
    candidate_refs = copy.deepcopy(previous.get("candidate_sources", [])) if continuing else []
    origins = list(previous.get("candidate_origins", [])) if continuing else []
    contents = list(previous.get("candidate_contents", [])) if continuing else []
    for ref, origin, content in independent:
        if origin not in origins and content not in contents:
            candidate_refs.append(ref)
            origins.append(origin)
            contents.append(content)
    # Always persist a first candidate before adopting a different statement.
    # A later reviewed batch must bring another independent original observation.
    if continuing and len(origins) >= 2 and len(contents) >= 2:
        # Legacy rows can contain 280 characters. Never drop a trailing qualifier
        # merely to produce the shorter old/new phrase used by expression.
        assessment.update(status="considered", last_previous_text=current["text"] if len(current["text"]) <= 180 else "")
        changed.update(text=item["statement"], confidence=bounded, assessment=assessment)
    else:
        assessment.update(status="contested" if item["relation"] == "challenge" else "tentative",
                          candidate_text=item["statement"], candidate_relation=item["relation"],
                          candidate_sources=candidate_refs, candidate_origins=origins, candidate_contents=contents)
        changed.update(confidence=round(max(0., confidence-.1), 8) if item["relation"] == "challenge" else confidence,
                       assessment=assessment)
    return changed


def select_belief_context(records, state, resolve, limit=12):
    """Select at most one related prior view together with all its original roots."""
    from .growth_store import source_fingerprint
    from .unified_memory import _terms
    if not isinstance(records, list) or not isinstance(state, dict) or type(limit) is not int or not 1 <= limit <= 12:
        raise ValueError("invalid_belief_context")
    fresh = [r for r in records if isinstance(r, dict) and isinstance(r.get("id"), str) and not r.get("derived")][:limit]
    body = " ".join(r.get("text", "") for r in fresh)
    scored = []
    for old in state.get("beliefs", []):
        terms = _terms(old.get("topic", ""))
        score = sum(term in body.casefold() for term in terms)
        if score and old.get("sources") and len(old["sources"]) < limit:
            scored.append((score, old))
    for _, old in sorted(scored, key=lambda pair: (-pair[0], pair[1]["key"])):
        refs = old["sources"]
        try:
            roots = resolve([r["id"] for r in refs])
            if any(r["id"] not in roots or source_fingerprint(roots[r["id"]]) != r["digest"] for r in refs):
                continue
            extra = [r for r in fresh if r["id"] not in roots]
            if not extra:
                continue
            return [roots[r["id"]] for r in refs]+extra[:limit-len(refs)]
        except (KeyError, TypeError, ValueError):
            continue
    return fresh
