"""Search Wikipedia's article namespace and read bounded, attributable plaintext excerpts."""
from __future__ import annotations

from html import unescape
import json
import re
import unicodedata
from urllib.parse import quote, urlencode

from .media_http import MediaFetchError, fetch_public


_HOSTS = {"zh": "zh.wikipedia.org", "en": "en.wikipedia.org"}
_SOURCES = {"zh": "中文维基百科", "en": "English Wikipedia"}
_MAX_PAGE_ID = 2_147_483_647
_MAX_OFFSET = 12_000
_EXCERPT_SIZE = 1_200
_SENSITIVE = re.compile(
    r"\d{5,}|www\.|[\w.-]+\.(?:com|net|org|cn|io)\b|\bsk-[a-z\d-]{12,}|"
    r"我(?:家|的|叫|住|要|想|刚|在|今天)|你(?:家|的|帮我)|帮我|私聊|密码|验证码|家庭住址|"
    r"手机号|身份证号|(?:qq|微信)号|为什么|是谁|是什么|怎么回事|"
    r"\b(?:i|my|your|our|password|secret|token|cookie|address|phone number|who is|what is)\b", re.I)
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


def _host(language):
    if not isinstance(language, str) or language not in _HOSTS:
        raise ValueError("reference_invalid_language")
    return _HOSTS[language]


def _request(host, **parameters):
    query = urlencode({"action": "query", "format": "json", "formatversion": 2, "maxlag": 5, **parameters})
    try:
        raw = fetch_public("https://" + host + "/w/api.php?" + query, allowed_hosts=(host,),
                           max_bytes=1024 * 1024, timeout=8.0)
    except MediaFetchError:
        raise ValueError("reference_unavailable") from None
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("reference_invalid_response") from None
    if not isinstance(data, dict) or "error" in data or not isinstance(data.get("query"), dict):
        raise ValueError("reference_unavailable")
    return data["query"]


def public_topic(value):
    if not isinstance(value, str) or len(value) > 80:
        raise ValueError("reference_invalid_query")
    text = unicodedata.normalize("NFKC", value).strip(" ")
    if (not 1 <= len(text) <= 80 or not any(c.isalpha() for c in text) or _SENSITIVE.search(text)
            or any(not (c.isalnum() or c in " .·'’+#()-") for c in text)):
        raise ValueError("reference_invalid_query")
    return text


def _article(row, language):
    if not isinstance(row, dict):
        raise ValueError("reference_invalid_response")
    page_id, title = row.get("pageid"), row.get("title")
    if (type(row.get("ns")) is not int or row["ns"] != 0
            or type(page_id) is not int or not 1 <= page_id <= _MAX_PAGE_ID
            or not isinstance(title, str) or not 1 <= len(title) <= 255 or not title.strip()
            or title in (".", "..") or any(unicodedata.category(c).startswith("C") for c in title)):
        raise ValueError("reference_invalid_response")
    url = "https://" + _HOSTS[language] + "/wiki/" + quote(title.replace(" ", "_"), safe="")
    return {"page_id": page_id, "language": language, "title": title, "source": _SOURCES[language], "url": url}


def reference_search(query, language="zh", limit=5):
    """Only an explicit public topic is sent; no account, chat history, or user-supplied endpoint."""
    host = _host(language)
    if type(limit) is not int or not 1 <= limit <= 5:
        raise ValueError("reference_invalid_limit")
    data = _request(host, list="search", srsearch=public_topic(query), srnamespace=0, srlimit=limit,
                    srprop="snippet|timestamp")
    rows = data.get("search")
    if not isinstance(rows, list):
        raise ValueError("reference_invalid_response")
    items = []
    for row in rows[:limit]:
        item = _article(row, language)
        snippet = row.get("snippet")
        if not isinstance(snippet, str):
            raise ValueError("reference_invalid_response")
        plain = unescape(re.sub(r"<[^<>]{1,256}>", "", snippet[:4000]))
        stamp = row.get("timestamp")
        items.append({**item, "summary": " ".join(plain.split())[:240], "basis": "search_snippet",
                      "updated_at": stamp if isinstance(stamp, str) and _TIME.fullmatch(stamp) else None})
    return {"items": items}


def reference_read(page_id, language="zh", offset=0):
    """Read the current revision at this page ID. Character offsets refer to the returned revision."""
    host = _host(language)
    if type(page_id) is not int or not 1 <= page_id <= _MAX_PAGE_ID:
        raise ValueError("reference_invalid_page")
    if type(offset) is not int or not 0 <= offset <= _MAX_OFFSET:
        raise ValueError("reference_invalid_offset")
    data = _request(host, pageids=page_id, prop="info|extracts|revisions", rvprop="ids|timestamp",
                    rvlimit=1, explaintext=1, exsectionformat="plain")
    pages = data.get("pages")
    if not isinstance(pages, list) or len(pages) != 1 or not isinstance(pages[0], dict):
        raise ValueError("reference_invalid_response")
    page = pages[0]
    if page.get("missing") or page.get("invalid"):
        raise ValueError("reference_page_missing")
    if page.get("redirect") or data.get("redirects") or page.get("pageid") != page_id:
        raise ValueError("reference_page_changed")
    item = _article(page, language)
    versions = page.get("revisions")
    if not isinstance(versions, list) or len(versions) != 1 or not isinstance(versions[0], dict):
        raise ValueError("reference_invalid_response")
    revision, stamp = versions[0].get("revid"), versions[0].get("timestamp")
    if (type(revision) is not int or revision <= 0 or revision != page.get("lastrevid")
            or not isinstance(stamp, str) or not _TIME.fullmatch(stamp)):
        raise ValueError("reference_page_changed")
    extract = page.get("extract")
    if not isinstance(extract, str) or not extract.strip():
        raise ValueError("reference_no_excerpt")
    text = " ".join(extract.split())
    if offset >= len(text):
        raise ValueError("reference_invalid_offset")
    end = min(offset + _EXCERPT_SIZE, len(text))
    return {**item, "revision_id": revision, "updated_at": stamp, "offset": offset,
            "revision_url": "https://" + host + "/wiki/Special:PermanentLink/" + str(revision),
            "text": text[offset:end], "has_more_text": end < len(text),
            "next_offset": end if end < len(text) and end <= _MAX_OFFSET else None,
            "basis": "encyclopedia_excerpt"}
