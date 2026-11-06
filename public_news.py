"""Read recent Chinese publisher feeds and bounded, attributable article excerpts."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import re
from urllib.parse import urlsplit
from xml.etree.ElementTree import ParseError

from .explore import MAX_BYTES, parse_feed
from .media_http import MediaFetchError, fetch_public


_SOURCES = {
    "un": ("联合国新闻", "news.un.org", "https://news.un.org/feed/subscribe/zh/news/all/rss.xml"),
    "ithome": ("IT之家", "www.ithome.com", "https://www.ithome.com/rss/"),
}
_UN_PATH = re.compile(r"/(?:feed/view/)?zh/story/([0-9]{4})/(0[1-9]|1[0-2])/([0-9]{5,10})")
_IT_PATH = re.compile(r"/([0-9]{1,2})/([0-9]{3})/([0-9]{3})\.htm")
_VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
_HIDDEN = frozenset({"script", "style", "nav", "aside", "footer", "template", "noscript", "figure", "button"})
_EXCERPT = 1000


def _story_id(source, url):
    if not isinstance(url, str) or not url.isascii() or len(url) > 180:
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if (parsed.scheme != "https" or parsed.netloc != _SOURCES[source][1] or parsed.query or parsed.fragment
            or any(ord(ch) < 33 or ord(ch) == 127 for ch in url)):
        return None
    pattern = _UN_PATH if source == "un" else _IT_PATH
    match = pattern.fullmatch(parsed.path)
    return ":".join((source, *match.groups())) if match else None


def _article_url(key):
    if not isinstance(key, str) or len(key) > 32:
        raise ValueError("invalid_news_id")
    fields = key.split(":")
    if len(fields) != 4 or fields[0] not in _SOURCES:
        raise ValueError("invalid_news_id")
    source, a, b, c = fields
    path = f"/zh/story/{a}/{b}/{c}" if source == "un" else f"/{a}/{b}/{c}.htm"
    url = "https://" + _SOURCES[source][1] + path
    if _story_id(source, url) != key:
        raise ValueError("invalid_news_id")
    return source, url


def _items(source):
    name, host, url = _SOURCES[source]
    data = fetch_public(url, allowed_hosts=(host,), max_bytes=MAX_BYTES, timeout=4)
    # Both feeds are UTF-8. Reject DTDs before handing feed bytes to the XML parser.
    if b"<!DOCTYPE" in data.upper() or b"\x00" in data:
        raise ValueError("invalid_news_feed")
    items = {}
    for story in parse_feed(data, url, max_items=80):
        key = _story_id(source, story.url)
        if key and key not in items:
            published = datetime.fromtimestamp(story.published_at, timezone.utc) if story.published_at is not None else None
            items[key] = {"id": key, "source": name, "title": story.title, "url": _article_url(key)[1],
                          "published_at": published.isoformat() if published else None, "summary": story.summary}
    return items


def news_search(query: str = "", limit: int = 5) -> dict:
    """Search recent feed headlines/descriptions; list failed sources even on partial success."""
    if not isinstance(query, str) or len(query) > 80 or any(ord(ch) < 32 for ch in query):
        raise ValueError("invalid_news_query")
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError("invalid_news_limit")
    words = query.casefold().split()
    found = []
    unavailable = []
    for source, (name, _, _) in _SOURCES.items():
        try:
            entries = _items(source)
        except (MediaFetchError, ValueError, ParseError, OverflowError, OSError):
            unavailable.append(name)
            continue
        for item in entries.values():
            searchable = " ".join((name, item["title"], item["summary"])).casefold()
            if all(word in searchable for word in words):
                found.append(item)
    found.sort(key=lambda item: item["published_at"] or "", reverse=True)
    if not words and limit >= len(_SOURCES):
        # A minute-by-minute tech feed should not obscure the day's international report.
        latest = {}
        for item in found:
            latest.setdefault(item["source"], item["id"])
        selected = set(latest.values())
        for item in found:
            if len(selected) == limit:
                break
            selected.add(item["id"])
        found = [item for item in found if item["id"] in selected]
    return {"coverage": "recent_feeds", "items": found[:limit], "unavailable_sources": unavailable}


class _Article(HTMLParser):
    """Select the two publishers' actual body containers, never generic page text."""
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.source = source
        self.stack = []
        self.data = {"title": [], "date": [], "body": []}
        self.meta = {}
        self.canonical = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "link" and "canonical" in (attrs.get("rel") or "").lower().split():
            self.canonical = attrs.get("href") or ""
        elif tag == "meta":
            name = attrs.get("property") or ""
            if name in {"og:title", "og:url", "article:published_time"}:
                self.meta[name] = attrs.get("content") or ""
        if tag in _VOID:
            return
        if len(self.stack) >= 128:
            raise ValueError("invalid_article")
        _, hidden, body, field = self.stack[-1] if self.stack else ("", False, False, "")
        classes = (attrs.get("class") or "").split()
        hidden = hidden or tag in _HIDDEN or "hidden" in attrs or attrs.get("aria-hidden") == "true"
        if self.source == "un":
            body = body or tag == "div" and "field--name-field-text-column" in classes
        else:
            body = body or tag == "div" and attrs.get("id") == "paragraph" and "post_content" in classes
        if body and tag in {"p", "h2", "h3", "h4"}:
            field = "body"
            if not hidden:
                self.data["body"].append("\n")
        elif tag == "h1" and not self.data["title"]:
            field = "title"
        elif self.source == "ithome" and attrs.get("id") == "pubtime_baidu":
            field = "date"
        self.stack.append((tag, hidden, body, field))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.stack:
            _, hidden, _, field = self.stack[-1]
            if field and not hidden:
                self.data[field].append(data)

    def published_at(self):
        try:
            if self.source == "ithome":
                local = datetime.strptime("".join(self.data["date"]).strip(), "%Y/%m/%d %H:%M:%S")
                stamp = local.replace(tzinfo=timezone(timedelta(hours=8)))
            else:
                stamp = datetime.fromisoformat(self.meta.get("article:published_time", "").replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    return None
            return stamp.astimezone(timezone.utc).isoformat()
        except (ValueError, OverflowError):
            return None


def news_read(id: str) -> dict:
    """Reopen a stable publisher article ID across restarts; fall back to a matching RSS item."""
    source, url = _article_url(id)
    name, host, _ = _SOURCES[source]
    result = {"id": id, "source": name, "url": url, "title": "", "published_at": None,
              "text": "", "basis": "unavailable"}
    try:
        page = fetch_public(url, allowed_hosts=(host,), max_bytes=MAX_BYTES, timeout=5)
        parser = _Article(source)
        parser.feed(page.decode("utf-8-sig"))
        parser.close()
        identity = parser.canonical or parser.meta.get("og:url")
        title = parser.meta.get("og:title") or "".join(parser.data["title"])
        title = " ".join(title.split())[:180]
        paragraphs = [" ".join(p.split()) for p in "".join(parser.data["body"]).splitlines()]
        body = "\n".join(p for p in paragraphs if p and not p.startswith(("广告声明", "投诉水文")))
        if _story_id(source, identity) == id and title and body:
            return {**result, "title": title, "published_at": parser.published_at(),
                    "text": body[:_EXCERPT], "basis": "article"}
    except (ValueError, OSError, AssertionError):
        pass
    try:
        item = _items(source).get(id)
    except (MediaFetchError, ValueError, ParseError, OverflowError, OSError):
        item = None
    if item:
        return {**result, "title": item["title"], "published_at": item["published_at"],
                "text": item["summary"], "basis": "feed_summary"}
    return result
