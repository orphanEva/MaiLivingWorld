"""Follow a source-backed public question with a bounded read-only lookup."""
from __future__ import annotations

import unicodedata

from .growth_store import source_fingerprint
from .growth_model import MAX_RECORDS
from .mcp_runtime import digest
from .public_reference import public_topic
from .public_web_search import _public_url
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
    question_digest = digest(question)
    owner = bot._self_state.state()["owner_id"]
    store = bot._ensure_tool_store()
    context = {**context, "query": topic, "public_search_terms": [topic]}

    async def call(provider, tool, args):
        if not valid():
            return {}
        return await bot._mcp_bridge.execute(provider, tool, args, config=bot.config.mcp, context=context,
            owner=bot._self_state.state()["owner_id"], client=bot._mcp_client(provider), valid=valid,
            timezone=bot.config.general.timezone, record_observations=True, on_dispatch=on_dispatch,
            autonomous_read=tool in {"news_read", "web_read", "reference_read"})

    # Prefer current web results when the deployment has a search key. Without
    # it, publisher feeds can answer recent questions and Wikipedia supplies background.
    web = bool(bot._builtin_mcp_environment("news").get("MLW_BRAVE_SEARCH_KEY"))
    searches = [("news", "web_search" if web else "news_search"), ("reference", "reference_search")]
    original_urls = {record["url"] for record in checked["records"]}
    row = None
    retry_candidate = None
    complete_searches = True
    for provider, search in searches:
        found = await call(provider, search, {"query": topic, "limit": 3})
        if not valid():
            return {"success": False, "records": []}
        data = found.get("data")
        rows = data.get("items") if isinstance(data, dict) else None
        if not found.get("success") or not isinstance(rows, list):
            if retry_candidate is None:
                return {"success": False, "records": []}
            break
        if "unavailable_sources" in data and data["unavailable_sources"] != []:
            complete_searches = False
        candidates = []
        seen = set(original_urls)
        for item in rows[:3]:
            if not isinstance(item, dict):
                complete_searches = False
                continue
            url = _public_url(item.get("url"))
            fields = [item.get(key) for key in ("title", "summary", "published_at", "updated_at")]
            if not url or len(url) > 1000 or any(value is not None and not isinstance(value, str) for value in fields):
                complete_searches = False
                continue
            if url in seen:
                continue
            seen.add(url)
            # Search-service handles are ephemeral. Use the public page and listing
            # version so restarting the service does not reread the same result.
            candidates.append((item, url, digest([provider, *fields])))
        status = store.research_candidate_status(owner, question_digest,
                                                 {url: version for _, url, version in candidates})
        if status["deferred"]:
            complete_searches = False
        available = [item for item in candidates if item[1] in status["available"]]
        selected = next((item for item in available if item[1] not in status["retrying"]), None)
        row = selected[0] if selected else None
        if row is not None:
            break
        # Give fresh results from the next provider a chance before retrying a
        # page that has already failed. Once all have been tried, rotate oldest first.
        retry = min(available, key=lambda item: status["retrying"][item[1]], default=None)
        if retry is not None:
            attempted_at = status["retrying"][retry[1]]
            if retry_candidate is None or attempted_at < retry_candidate[0]:
                retry_candidate = (attempted_at, provider, retry)
    if row is None and retry_candidate is not None:
        _, provider, selected = retry_candidate
        row = selected[0]
    if row is None:
        result = {"success": True, "records": []}
        if complete_searches:
            result["outcome"] = "no_new_evidence"
        return result
    if provider == "reference":
        tool, args = "reference_read", {"page_id": row.get("page_id"), "language": row.get("language", "zh")}
    else:
        tool, args = ("web_read" if web else "news_read"), {"id": row.get("id")}
    detail = await call(provider, tool, args)
    if not valid():
        return {"success": False, "records": []}
    attempt = detail.get("read_attempt")
    if attempt in {"unreadable", "read_failed"}:
        store.mark_research_attempt(owner, question_digest, selected[1], selected[2],
            provider=provider, tool=tool, args_digest=digest(args), call_id=detail["call_id"], outcome=attempt)
    if not detail.get("success"):
        return {"success": False, "records": []}
    record = detail.get("observation")
    if not record or not _public_source_url(record):
        return {"success": True, "records": []}
    return {"success": True, "records": [record], "read_marker": {
        "question_digest": question_digest, "url": selected[1], "version": selected[2], "call_id": detail["call_id"]}}
