"""Optional Brave web search. A public page can only be opened with a recent server-issued ID."""
from __future__ import annotations

from collections import OrderedDict
from html import unescape
from html.parser import HTMLParser
import json
import os
import re
import secrets
from threading import Lock
import time
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from .media_http import MediaFetchError, fetch_brave_api, fetch_public
from .public_reference import public_topic


_TTL = 900
_CAPACITY = 128
_SEEN = OrderedDict()
_LOCK = Lock()
_DOMAIN = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:[a-z]{2,63}|xn--[a-z0-9-]{2,59})")
_HIDDEN = {"head", "script", "style", "nav", "aside", "footer", "header", "form", "noscript", "template",
           "iframe", "button", "dialog", "svg"}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_CHARSET = re.compile(rb"<meta\s[^>]{0,500}?charset\s*=\s*[\"']?([\w-]{1,24})", re.I)


def _key():
    token = os.environ.get("MLW_BRAVE_SEARCH_KEY")
    if not token:
        raise ValueError("web_search_key_missing")
    return token


def _public_url(url):
    if not isinstance(url, str) or len(url) > 2000 or "\\" in url or any(ord(ch) < 33 or ord(ch) == 127 for ch in url):
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if (parts.scheme != "https" or parts.username is not None or parts.password is not None
                or parts.port not in (None, 443) or not _DOMAIN.fullmatch(host)
                or host.endswith((".test", ".invalid", ".internal", ".local", ".lan", ".localhost", ".onion", ".arpa"))):
            return None
        # Never put signed links or session information into replies or our handle cache.
        # Keep page identities with clean paths; removing arbitrary parameters could change which article is read.
        for key, _ in parse_qsl(parts.query, keep_blank_values=True, max_num_fields=32):
            if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}:
                return None
        path = quote(parts.path or "/", safe="/:%!$&'()*+,;=@-._~")
        return urlunsplit(("https", host, path, "", ""))
    except (ValueError, UnicodeError):
        return None


def _clean_label(value, limit):
    if not isinstance(value, str):
        return ""
    return " ".join(unescape(re.sub(r"<[^<>]{1,256}>", "", value[:4000])).split())[:limit]


def _remember(url, title):
    key = "web_" + secrets.token_hex(16)
    with _LOCK:
        _SEEN[key] = (time.monotonic() + _TTL, url, title)
        while len(_SEEN) > _CAPACITY:
            _SEEN.popitem(last=False)
    return key


def web_search(query, limit=5):
    token = _key()
    if type(limit) is not int or not 1 <= limit <= 5:
        raise ValueError("web_invalid_limit")
    try:
        topic = public_topic(query)
    except ValueError:
        raise ValueError("web_invalid_query") from None
    parameters = urlencode({"q": topic, "count": limit, "result_filter": "web", "text_decorations": "false"})
    try:
        raw = fetch_brave_api("https://api.search.brave.com/res/v1/web/search?" + parameters,
                              token=token, max_bytes=512 * 1024, timeout=8)
    except MediaFetchError:
        raise ValueError("web_search_unavailable") from None
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("web_invalid_response") from None
    if not isinstance(data, dict) or data.get("type") != "search" or data.get("error"):
        raise ValueError("web_invalid_response")
    web = data.get("web")
    if web is None:
        return {"items": []}
    if not isinstance(web, dict) or not isinstance(web.get("results"), list):
        raise ValueError("web_invalid_response")
    items, urls = [], set()
    for row in web["results"][:limit]:
        if not isinstance(row, dict):
            continue
        url, title = _public_url(row.get("url")), _clean_label(row.get("title"), 180)
        if not url or not title or url in urls:
            continue
        urls.add(url)
        items.append({"id": _remember(url, title), "title": title, "url": url, "source": urlsplit(url).hostname,
                      "summary": _clean_label(row.get("description"), 350), "basis": "search_snippet"})
    return {"items": items}


class _Paragraphs(HTMLParser):
    """Prefer visible paragraphs in article/main over general page paragraphs."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.primary = []
        self.other = []
        self.title = []
        self.og_title = ""

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "meta" and attributes.get("property") == "og:title":
            self.og_title = _clean_label(attributes.get("content"), 180)
        if tag in _VOID:
            if tag == "br":
                self.handle_data(" ")
            return
        if len(self.stack) >= 128:
            raise ValueError("web_unreadable")
        _, hidden, main, paragraph = self.stack[-1] if self.stack else ("", False, False, None)
        hidden = (hidden or tag in _HIDDEN or "hidden" in attributes or attributes.get("aria-hidden") == "true"
                  or attributes.get("role") in {"navigation", "banner", "contentinfo"})
        main = main or tag in {"main", "article"} or attributes.get("role") == "main"
        if tag == "p" and not hidden:
            if paragraph is not None:
                self.handle_endtag("p")  # HTML allows omitted closing paragraph tags.
            paragraph = []
            (self.primary if main else self.other).append(paragraph)
        self.stack.append((tag, hidden, main, paragraph))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        if self.stack:
            tag, hidden, _, paragraph = self.stack[-1]
            if tag == "title":
                self.title.append(data)
            if not hidden and paragraph is not None:
                paragraph.append(data)

    def excerpt(self):
        for group in (self.primary, self.other):
            paragraphs = [" ".join("".join(parts).split()) for parts in group]
            text = "\n".join(dict.fromkeys(line for line in paragraphs if line))
            if len(text) >= 80:
                return text[:1600]
        raise ValueError("web_unreadable")


def web_read(id):
    _key()
    if not isinstance(id, str) or not re.fullmatch(r"web_[0-9a-f]{32}", id):
        raise ValueError("web_invalid_id")
    with _LOCK:
        entry = _SEEN.get(id)
        if not entry or entry[0] <= time.monotonic():
            _SEEN.pop(id, None)
            raise ValueError("web_result_expired")
    _, url, title = entry
    host = urlsplit(url).hostname
    try:
        raw = fetch_public(url, allowed_hosts=(host,), max_bytes=768 * 1024, timeout=8, allow_redirects=False)
    except MediaFetchError:
        raise ValueError("web_read_unavailable") from None
    charset = _CHARSET.search(raw[:4096])
    encoding = charset.group(1).decode().lower() if charset else "utf-8-sig"
    if encoding not in {"utf-8", "utf-8-sig", "utf8", "gbk", "gb2312", "gb18030", "big5", "iso-8859-1", "windows-1252"}:
        raise ValueError("web_unreadable")
    parser = _Paragraphs()
    try:
        parser.feed(raw.decode(encoding))
        parser.close()
        page_title = parser.og_title or _clean_label("".join(parser.title), 180)
        if re.search(r"access denied|verify you are human|just a moment|captcha|^sign in|^log in|访问受限|人机验证|页面不存在|请先登录", page_title, re.I):
            raise ValueError("web_unreadable")
        excerpt = parser.excerpt()
    except (UnicodeError, ValueError, AssertionError):
        raise ValueError("web_unreadable") from None
    return {"id": id, "title": page_title or title, "url": url, "source": host, "text": excerpt, "basis": "web_excerpt"}
