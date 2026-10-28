"""RSS/Atom 外部见闻：仅抓用户明确配置的信息源，不访问文章内链接。"""
from __future__ import annotations

import hashlib
import html
import re
import socket
import ipaddress
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

MAX_BYTES = 1024 * 1024


@dataclass(frozen=True)
class Story:
    key: str
    source: str
    title: str
    url: str
    summary: str
    published_at: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def compact(text: str, limit: int = 300) -> str:
    """信息源都是不可信文本；只作为引用材料，不执行里面的指令。"""
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(text or "")))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def valid_https_url(url: str) -> bool:
    try:
        p = urlsplit(url)
        return p.scheme == "https" and bool(p.hostname) and not (p.username or p.password) and p.port in (None, 443)
    except ValueError:
        return False


def valid_feed_host(url: str, allowed_hosts: list[str]) -> bool:
    if not valid_https_url(url):
        return False
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host in {str(h).lower().rstrip(".") for h in allowed_hosts}


def _public_dns_only(host: str) -> None:
    """避免 DNS 指向私网 / 运营商保留地址。"""
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses:
        raise ValueError("host has no DNS records")
    for result in addresses:
        ip = ipaddress.ip_address(result[4][0])
        if not ip.is_global:
            raise ValueError(f"non-public resolved address: {ip}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("feed redirect disabled; configure final HTTPS URL")


def fetch_feed(url: str, allowed_hosts: list[str], timeout: int = 12) -> bytes:
    if not valid_feed_host(url, allowed_hosts):
        raise ValueError("RSS source not allowed: HTTPS hostname must be whitelisted")
    host = urlsplit(url).hostname
    assert host is not None
    _public_dns_only(host)
    opener = urllib.request.build_opener(_NoRedirect())
    request = urllib.request.Request(url, headers={"User-Agent": "MaiProactive-Explore/0.2 (+RSS)", "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"})
    with opener.open(request, timeout=max(3, min(timeout, 30))) as r:
        data = r.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("feed too large")
        return data


def _child_text(element: ET.Element, *tags: str) -> str:
    for tag in tags:
        child = element.find(tag)
        if child is not None:
            val = "".join(child.itertext()).strip()
            if val:
                return val
    return ""


def _pubtime(text: str) -> float | None:
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)
    except (ValueError, TypeError, IndexError):
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def parse_feed(data: bytes, feed_url: str, max_items: int = 30) -> list[Story]:
    """支持 RSS 2.0 和 Atom；严格限制字段长度，避免无限文本进模型。"""
    if len(data) > MAX_BYTES:
        raise ValueError("feed too large")
    root = ET.fromstring(data)
    items = []
    if root.tag.lower().endswith("rss"):
        channel = root.find("channel")
        if channel is None:
            return []
        source = compact(_child_text(channel, "title"), 80) or urlsplit(feed_url).hostname or "RSS"
        for element in channel.findall("item")[:max_items]:
            title = compact(_child_text(element, "title"), 180)
            link = _child_text(element, "link")
            summary = compact(_child_text(element, "description", "{http://purl.org/rss/1.0/modules/content/}encoded"))
            published = _pubtime(_child_text(element, "pubDate", "{http://purl.org/dc/elements/1.1/}date"))
            if title and valid_https_url(link):
                key = hashlib.sha256(link.split("#", 1)[0].encode()).hexdigest()[:24]
                items.append(Story(key, source, title, link, summary, published))
    elif root.tag == "{http://www.w3.org/2005/Atom}feed":
        ns = "{http://www.w3.org/2005/Atom}"
        source = compact(_child_text(root, ns + "title"), 80) or urlsplit(feed_url).hostname or "Atom"
        for element in root.findall(ns + "entry")[:max_items]:
            title = compact(_child_text(element, ns + "title"), 180)
            link = ""
            for link_node in element.findall(ns + "link"):
                if link_node.get("rel", "alternate") == "alternate" and link_node.get("href"):
                    link = str(link_node.get("href"))
                    break
            summary = compact(_child_text(element, ns + "summary", ns + "content"))
            published = _pubtime(_child_text(element, ns + "published", ns + "updated"))
            if title and valid_https_url(link):
                key = hashlib.sha256(link.split("#", 1)[0].encode()).hexdigest()[:24]
                items.append(Story(key, source, title, link, summary, published))
    else:
        raise ValueError("not an RSS2 or Atom feed")
    return items


def select_recent(stories: list[Story], now_ts: float, max_age_hours: int, max_candidates: int, delivered_keys: set[str]) -> list[Story]:
    result = []
    for story in stories:
        if story.key in delivered_keys:
            continue
        if story.published_at is not None:
            if story.published_at > now_ts + 6 * 3600 or now_ts - story.published_at > max_age_hours * 3600:
                continue
        result.append(story)
    return result[:max_candidates]


def choose_story(response: object, candidates: list[Story]) -> Story | None:
    """只接受 JSON，且只能选择真实候选中的 key。"""
    import json
    from .policy import extract_text
    if isinstance(response, dict) and response.get("success") is False:
        return None
    content = extract_text(response).strip()
    match = re.search(r"\{[\s\S]*?\}", content)
    if not match:
        return None
    try:
        decision = json.loads(match.group())
    except ValueError:
        return None
    if not isinstance(decision, dict) or decision.get("share") is not True:
        return None
    key = decision.get("key")
    for story in candidates:
        if story.key == key:
            return story
    return None
