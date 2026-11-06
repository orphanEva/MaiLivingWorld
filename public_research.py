"""Follow a source-backed public question with a bounded read-only lookup."""
from __future__ import annotations

import unicodedata

from .growth_store import source_fingerprint
from .growth_model import MAX_RECORDS
from .public_reference import public_topic
from .unified_expression import _INSTRUCTION, _SECRET, _public_source_url


def public_question(question, resolve):
    """A public keyword must occur in the original public evidence, never just in a guess."""
    if not isinstance(question, dict) or question.get("status") != "open":
        return None
    topic, refs = question.get("topic"), question.get("sources")
    try:
        topic = public_topic(topic)
        # Leave one evidence slot for the source discovered by this lookup.
        if (_INSTRUCTION.search(topic) or _SECRET.search(topic) or not isinstance(refs, list)
                or not 1 <= len(refs) < MAX_RECORDS):
            return None
        roots = resolve([ref["id"] for ref in refs])
        records = [roots[ref["id"]] for ref in refs]
        if not all(source_fingerprint(record) == ref["digest"] and _public_source_url(record)
                   for ref, record in zip(refs, records)):
            return None
        folded = topic.casefold()
        if not any(folded in unicodedata.normalize("NFKC", row["text"]).casefold() for row in records):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return {"topic": topic, "records": records}


async def investigate(bot, question, context, valid, on_dispatch=None):
    checked = public_question(question, bot._resolve_roots)
    if not checked or not valid():
        return {"success": False, "records": []}
    topic = checked["topic"]
    context = {**context, "query": topic, "public_search_terms": [topic]}

    async def call(provider, tool, args):
        if not valid():
            return {}
        return await bot._mcp_bridge.execute(provider, tool, args, config=bot.config.mcp, context=context,
            owner=bot._self_state.state()["owner_id"], client=bot._mcp_client(provider), valid=valid,
            timezone=bot.config.general.timezone, record_observations=True, on_dispatch=on_dispatch,
            autonomous_read=tool in {"news_read", "web_read"})

    # Prefer current web results when the deployment has a search key. Without
    # it, publisher feeds can answer recent questions and Wikipedia supplies background.
    web = bool(bot._builtin_mcp_environment("news").get("MLW_BRAVE_SEARCH_KEY"))
    searches = [("news", "web_search")] if web else [("news", "news_search"), ("reference", "reference_search")]
    original_urls = {record["url"] for record in checked["records"]}
    row = None
    for provider, search in searches:
        found = await call(provider, search, {"query": topic, "limit": 3})
        if not found.get("success") or not valid():
            return {"success": False, "records": []}
        rows = found["data"].get("items", [])
        if not isinstance(rows, list):
            return {"success": False, "records": []}
        row = next((r for r in rows[:3] if isinstance(r, dict) and r.get("url") not in original_urls), None)
        if row is not None:
            break
    if row is None:
        return {"success": True, "records": []}
    if provider == "reference":
        tool, args = "reference_read", {"page_id": row.get("page_id"), "language": row.get("language", "zh")}
    else:
        tool, args = ("web_read" if web else "news_read"), {"id": row.get("id")}
    detail = await call(provider, tool, args)
    if not valid() or not detail.get("success"):
        return {"success": False, "records": []}
    record = detail.get("observation")
    return {"success": True, "records": [record] if record and _public_source_url(record) else []}
