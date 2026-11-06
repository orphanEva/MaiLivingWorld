"""Read-only growth projection and HTML export. Never expose raw source records."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import time

FORMAT = "mailivingworld.dashboard.v1"
TRAITS = {"curiosity": "好奇", "sociability": "社交", "patience": "耐心", "caution": "谨慎"}
CATEGORIES = ("interests", "beliefs", "knowledge", "questions")
STATUSES = {"active", "open", "resolved", "proposed", "accepted", "planned", "reading", "complete",
            "completed", "paused", "cancelled", "canceled", "failed", "stale", "pending", "running", "done", "queued"}
_PRIVATE = re.compile(r"(?i)(?:password|api.?key|access.?token|bearer\s+|密码|验证码|密钥|身份证|银行卡|保密|秘密|"
                      r"不要(?:公开|告诉|转告|透露)|\b(?:private|group):|https?://|[a-z]:[\\/]|"
                      r"[\w.+-]+@[\w.-]+\.[a-z]{2,}|\d{5,})")


class DashboardError(ValueError):
    pass


def _dict(value):
    return value if isinstance(value, dict) else {}


def _rows(value, limit=50):
    return [item for item in value[:limit] if isinstance(item, dict)] if isinstance(value, list) else []


def _count(value):
    return value if type(value) is int and 0 <= value <= 10**12 else 0


def _number(value, low=0, high=1):
    return value if type(value) in {int, float} and low <= value <= high and math.isfinite(value) else None


def _text(value, limit=240):
    if not isinstance(value, str) or _PRIVATE.search(value):
        return ""
    text = " ".join(value.split())
    return text[:limit] if not any(ord(c) < 32 or ord(c) == 127 for c in text) else ""


def _date(value):
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).astimezone(timezone.utc).isoformat(timespec="seconds")
        except (ValueError, OverflowError):
            return ""
    if _number(value, 0, 253402300799) is None:
        return ""
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")
    except (OSError, ValueError, OverflowError):
        return ""


def _status(value):
    return value if isinstance(value, str) and value in STATUSES else "unknown"


def _calendar_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return ""
    try:
        return datetime.fromisoformat(value).date().isoformat()
    except ValueError:
        return ""


def _bond_view(value):
    """Whitelist an already owner-filtered projection, including offline exports."""
    value = _dict(value)
    result = {"enabled": False, "preferred_address": "", "blocked_addresses": [],
              "affection": "default", "boundaries": [], "moments": []}
    if value.get("enabled") is not True:
        return result
    result["enabled"] = True
    result["preferred_address"] = _text(value.get("preferred_address"), 24)
    result["affection"] = value.get("affection") if value.get("affection") in ("default", "allow", "avoid") else "default"
    addresses = value.get("blocked_addresses")
    if isinstance(addresses, list):
        result["blocked_addresses"] = [text for item in addresses[:32] if (text := _text(item, 24))]
    result["boundaries"] = [{"text": text} for item in _rows(value.get("boundaries"), 32)
                            if (text := _text(item.get("text"), 120))]
    for item in _rows(value.get("moments"), 200):
        summary, kind = _text(item.get("summary"), 400), item.get("kind")
        occurred_on = _calendar_date(item.get("occurred_on"))
        if not summary or kind not in ("memory", "anniversary") or (kind == "anniversary" and not occurred_on):
            continue
        result["moments"].append({"summary": summary, "kind": kind, "occurred_on": occurred_on,
                                  "updated_at": _date(item.get("updated_at"))})
    return result


def _phase_view(value, allowed, now):
    """Expose the effective state only; an elapsed pause is never still busy."""
    value = _dict(value)
    normal = {"mode": "normal", "active": False, "until": ""}
    mode, until = value.get("mode"), value.get("until", 0)
    if isinstance(until, str):
        if not until:
            until = 0
        else:
            try:
                date = datetime.fromisoformat(until)
                until = date.timestamp() if date.tzinfo is not None else None
            except (ValueError, OSError, OverflowError):
                until = None
    until = _number(until, 0, 253402300799)
    if (mode not in allowed or value.get("active") is not True or until is None
            or _number(now, 0, 253402300799) is None or (until and until <= now)
            or (mode in ("busy", "rest") and not until)):
        return normal
    return {"mode": mode, "active": True, "until": _date(until) if until else ""}


def _continuity_view(value, *, now):
    """Whitelist owner-filtered summaries; no quotes, identities or source IDs."""
    value = _dict(value)
    result = {"enabled": False, "contact": _phase_view(None, (), now),
              "support": _phase_view(None, (), now), "threads": []}
    if value.get("enabled") is not True:
        return result
    result["enabled"] = True
    result["contact"] = _phase_view(value.get("contact"), ("busy", "rest", "manual"), now)
    result["support"] = _phase_view(value.get("support"), ("listen", "advice"), now)
    for item in _rows(value.get("threads"), 20):
        title, summary = _text(item.get("title"), 60), _text(item.get("summary"), 180)
        count = _count(item.get("source_count"))
        if (not title or not summary or item.get("kind") not in ("event", "concern", "wish")
                or item.get("status") != "open" or not 1 <= count <= 12):
            continue
        result["threads"].append({"title": title, "summary": summary, "kind": item["kind"], "status": "open",
                                  "updated_at": _date(item.get("updated_at")), "source_count": count})
    return result


def _attention_view(value, *, now):
    value = _dict(value)
    if value.get("enabled") is not True:
        return {"enabled": False}
    until = value.get("engaged_until", 0)
    if isinstance(until, str):
        try:
            until = datetime.fromisoformat(until).timestamp()
        except (ValueError, OverflowError):
            until = 0
    engaged = value.get("engaged") is True and _number(until, 0, 253402300799) is not None and until > now
    return {"enabled": True, "engaged": engaged, "engaged_until": _date(until) if engaged else "",
            "focus_kind": value.get("focus_kind") if value.get("focus_available") is True
                and value.get("focus_kind") in {"reading", "research"} else "",
            "focus_available": value.get("focus_available") is True,
            "working": value.get("working") is True and not engaged and value.get("focus_available") is True,
            "conversation": _phase_view(value.get("conversation"), ("listen", "advice", "repair", "light"), now),
            "last_outcome": value.get("last_outcome") if value.get("last_outcome") in
                {"progress", "blocked", "interrupted", "failed"} else "",
            "last_finished": _date(value.get("last_finished")), "cycle_count": _count(value.get("cycle_count"))}


def build_dashboard(*, identity: dict, growth: dict, resolved_sources: dict, fingerprint,
                    budgets=None, companion=None, bond=None, continuity=None, attention=None, authorized=False, now=None) -> dict:
    """Caller must authenticate the Host WebUI administrator or the private owner.

    ``authorized`` is a server decision, never a user/model argument. Invalid or
    third-party dependencies yield a placeholder, not hidden original content.
    ``growth`` should include invalid items so the panel can count invalidation.
    ``continuity.threads`` must come from ContinuityStore.threads(status="open"),
    which validates live source digests; the panel additionally filters audience
    and fields, and never treats an input queue or raw SQL row as verified.
    """
    if authorized is not True:
        raise DashboardError("dashboard_access_denied")
    if not all(isinstance(item, dict) for item in (identity, growth, resolved_sources)) or not callable(fingerprint):
        raise DashboardError("invalid_dashboard_state")
    if __package__:
        from .growth_limits import MAX_INTEREST_SOURCES, MAX_RECORDS
    else:
        from growth_limits import MAX_INTEREST_SOURCES, MAX_RECORDS

    now = time.time() if now is None else now
    owner = identity.get("owner_id")
    owner = owner if isinstance(owner, str) and re.fullmatch(r"[0-9]{5,20}", owner) else ""
    result = {"format": FORMAT, "generated_at": _date(now),
              "identity": {"owner_bound": bool(owner), "binding_revision": _count(identity.get("revision")), "agent_label": "持续自我"},
              "revision": _count(growth.get("revision")), "traits": [], "mood": {}, "growth": {},
              "source_counts": {"valid": 0, "invalid": 0, "protected": 0}, "budgets": {}, "plans": [], "reading": []}
    if isinstance(identity.get("agent_id"), str) and identity["agent_id"]:
        result["identity"]["agent_label"] = "自我·"+hashlib.sha256(identity["agent_id"].encode()).hexdigest()[:8]
    result["traits"] = [{"label": label, "value": _number(_dict(growth.get("traits")).get(key))} for key, label in TRAITS.items()]
    result["mood"] = {"valence": _number(_dict(growth.get("mood")).get("valence"), -1, 1),
                      "energy": _number(_dict(growth.get("mood")).get("energy"))}

    def source_state(item, category):
        refs = item.get("sources")
        limit = MAX_INTEREST_SOURCES if category == "interests" else MAX_RECORDS
        if not isinstance(refs, list) or not 1 <= len(refs) <= limit:
            return "invalid", 0
        records = []
        for ref in refs:
            if not isinstance(ref, dict) or not isinstance(ref.get("id"), str):
                return "invalid", len(refs)
            record = resolved_sources.get(ref["id"])
            try:
                if (not isinstance(record, dict) or record.get("id") != ref["id"]
                        or fingerprint(record) != ref.get("digest")):
                    return "invalid", len(refs)
            except Exception:
                return "invalid", len(refs)
            records.append(record)
        for record in records:
            scope = record.get("scope", "")
            if record.get("sensitivity") == "public" and isinstance(scope, str) and (scope == "public" or scope.startswith("public:")):
                continue
            if (owner and record.get("sensitivity") == "personal" and record.get("subject_id") == owner
                    and isinstance(scope, str) and (scope == "private:"+owner or scope.startswith("group:"))):
                continue
            return "protected", len(refs)
        return "valid", len(refs)

    for key in CATEGORIES:
        result["growth"][key] = []
        for item in _rows(growth.get(key), 32):
            state, count = source_state(item, key)
            topic = _text(item.get("topic")) if state == "valid" else ""
            text = _text(item.get("text"), 360) if state == "valid" else ""
            if state == "valid" and (not topic or (item.get("text") and not text)):
                state, topic, text = "protected", "", ""
            result["source_counts"][state] += 1
            result["growth"][key].append({"topic": topic, "text": text,
                "status": "active" if key == "interests" else _status(item.get("status")),
                "value": _number(item.get("weight") if state == "valid" else None, -1, 1)
                         if key == "interests" else _number(item.get("confidence")),
                "source_status": state, "source_count": count, "revision": _count(item.get("revision"))})
    for key in ("model", "tools", "research", "reading"):
        row = _dict(_dict(budgets).get(key))
        result["budgets"][key] = {"used": _count(row.get("used")), "limit": _count(row.get("limit"))}
    for key in ("plans", "reading"):
        for item in _rows(_dict(companion).get(key)):
            if not owner or item.get("owner_id") != owner or item.get("scope") != "private:"+owner:
                continue
            title = _text(item.get("title"))
            if not title:
                continue
            row = {"title": title, "status": _status(item.get("status")), "updated_at": _date(item.get("updated")),
                   "revision": _count(item.get("revision"))}
            if key == "plans":
                row.update(next_step=_text(item.get("next_step")), due_at=_date(item.get("due_at")))
            else:
                row.update(current=_count(item.get("cursor")), total=_count(item.get("total_units")),
                           unit="字符" if item.get("resource_type") == "novel" else "页 / 图",
                           provider=item.get("provider") if item.get("provider") in ("cap", "pixiv") else "local")
            result[key].append(row)
    profile = _dict(_dict(bond).get("profile"))
    result["bond"] = _bond_view(None)
    if owner and profile.get("owner_id") == owner:
        moments = [{"summary": item.get("summary"), "kind": item.get("kind"), "occurred_on": item.get("occurred_on"),
                    "updated_at": item.get("updated")}
                   for item in _rows(_dict(bond).get("moments"), 200)
                   if item.get("owner_id") == owner and item.get("scope") == "private:"+owner and item.get("status") == "active"]
        result["bond"] = _bond_view({"enabled": True, "preferred_address": profile.get("preferred_address"),
            "blocked_addresses": profile.get("blocked_addresses"), "affection": profile.get("affection"),
            "boundaries": profile.get("boundaries"), "moments": moments})
    current = _dict(_dict(continuity).get("state"))
    result["continuity"] = _continuity_view(None, now=now)
    if owner and current.get("owner_id") == owner:
        threads = []
        for item in _rows(_dict(continuity).get("threads"), 20):
            if item.get("owner_id") != owner or item.get("scope") != "private:"+owner or item.get("status") != "open":
                continue
            refs = item.get("source_refs")
            if (not isinstance(refs, list) or not 1 <= len(refs) <= 12
                    or any(not isinstance(ref, dict) or ref.get("scope") != "private:"+owner
                           or ref.get("sensitivity") != "personal" for ref in refs)):
                continue
            threads.append({"title": item.get("title"), "summary": item.get("summary"), "kind": item.get("kind"),
                            "status": "open", "updated_at": item.get("updated"), "source_count": len(refs)})
        result["continuity"] = _continuity_view({"enabled": True, "contact": current.get("contact"),
            "support": current.get("support"), "threads": threads}, now=now)
    result["attention"] = _attention_view(attention if owner and _dict(attention).get("owner_id") == owner else None, now=now)
    return result


def _export(snapshot):
    """Reconstruct whitelisted view fields, including for explicitly imported JSON."""
    if not isinstance(snapshot, dict) or snapshot.get("format") != FORMAT:
        raise DashboardError("invalid_dashboard_export")
    identity = _dict(snapshot.get("identity"))
    label = identity.get("agent_label", "")
    label = label if isinstance(label, str) and re.fullmatch(r"自我·[a-f0-9]{8}|持续自我", label) else "持续自我"
    result = {"format": FORMAT, "generated_at": _date(snapshot.get("generated_at")),
        "identity": {"owner_bound": identity.get("owner_bound") is True, "binding_revision": _count(identity.get("binding_revision")), "agent_label": label},
        "revision": _count(snapshot.get("revision")), "growth": {}, "traits": [], "mood": {}, "source_counts": {}, "budgets": {}, "plans": [], "reading": []}
    result["traits"] = [{"label": item["label"], "value": _number(item.get("value"))}
                        for item in _rows(snapshot.get("traits"), 4) if item.get("label") in TRAITS.values()]
    result["mood"] = {"valence": _number(_dict(snapshot.get("mood")).get("valence"), -1, 1),
                      "energy": _number(_dict(snapshot.get("mood")).get("energy"))}
    for key in CATEGORIES:
        result["growth"][key] = []
        for item in _rows(_dict(snapshot.get("growth")).get(key), 32):
            state = item.get("source_status")
            state = state if state in ("valid", "invalid", "protected") else "protected"
            value = item.get("value") if key != "interests" or state == "valid" else None
            result["growth"][key].append({"topic": _text(item.get("topic")) if state == "valid" else "",
                "text": _text(item.get("text"), 360) if state == "valid" else "", "status": _status(item.get("status")),
                "value": _number(value, -1 if key == "interests" else 0, 1), "source_status": state,
                "source_count": _count(item.get("source_count")), "revision": _count(item.get("revision"))})
    for key in ("valid", "invalid", "protected"):
        result["source_counts"][key] = _count(_dict(snapshot.get("source_counts")).get(key))
    for key in ("model", "tools", "research", "reading"):
        row = _dict(_dict(snapshot.get("budgets")).get(key))
        result["budgets"][key] = {"used": _count(row.get("used")), "limit": _count(row.get("limit"))}
    for key in ("plans", "reading"):
        for item in _rows(snapshot.get(key)):
            row = {"title": _text(item.get("title")), "status": _status(item.get("status")),
                   "updated_at": _date(item.get("updated_at")), "revision": _count(item.get("revision"))}
            if key == "plans":
                row.update(next_step=_text(item.get("next_step")), due_at=_date(item.get("due_at")))
            else:
                row.update(current=_count(item.get("current")), total=_count(item.get("total")),
                           unit="字符" if item.get("unit") == "字符" else "页 / 图",
                           provider=item.get("provider") if item.get("provider") in ("cap", "pixiv") else "local")
            result[key].append(row)
    result["bond"] = _bond_view(snapshot.get("bond"))
    result["continuity"] = _continuity_view(snapshot.get("continuity"), now=time.time())
    result["attention"] = _attention_view(snapshot.get("attention"), now=time.time())
    return result


def render_dashboard(snapshot=None):
    document = (Path(__file__).parent/"webui"/"index.html").read_text(encoding="utf-8")
    if snapshot is None:
        return document
    data = json.dumps(_export(snapshot), ensure_ascii=False, allow_nan=False)
    for char, escaped in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"), ("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
        data = data.replace(char, escaped)
    return document.replace('<script id="snapshot" type="application/json">null</script>', '<script id="snapshot" type="application/json">'+data+'</script>')


def main(argv=None):
    parser = argparse.ArgumentParser(description="把管理员导出的成长面板 JSON 转为直接可读的 HTML 快照")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.input.stat().st_size > 512*1024:
            raise DashboardError("export_too_large")
        html = render_dashboard(json.loads(args.input.read_text(encoding="utf-8")))
        with args.output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(html)
    except (ValueError, OSError, TypeError, KeyError):
        parser.error("导出格式无效或目标不可写；不会覆盖已有文件")
    print("成长面板 HTML 已生成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
