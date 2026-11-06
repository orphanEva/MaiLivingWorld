"""Provenance for the companion's own public reading and questions about it."""
from __future__ import annotations

from datetime import datetime
import re
import unicodedata
from zoneinfo import ZoneInfo

from .growth_store import source_fingerprint
from .unified_expression import _public_source_url


_QUESTION = re.compile(
    r"(?:你|你自己)?(?P<period>最近|这几天|这两天|今天|刚刚|刚才)?(?:你|你自己)?(?:自己)?"
    r"(?:在网上|上网|在外面|在外站|网上)?(?:都|又)?(?:有没有|有没|有)?"
    r"(?:逛到|逛|刷到|刷|看到|看见|看过|看|读到|读过|读|发现|遇到)"
    r"(?:了|过)?(?:什么|啥|哪些|什么样的|点什么|点啥|好玩|有趣|有意思)"
    r"(?:新鲜|有意思|好玩|有趣|特别|有印象|感兴趣|喜欢|想分享|值得一聊|值得分享|值得看|新)?"
    r"(?:的)?(?:东西|事情|事儿|事|见闻|内容|帖子|新闻|报道|文章|网页|发现)?(?:吗|么|没|呢|呀|啊|嘛)?"
)
RECEIPT_PREFIX = "autobrowse:v1:"
_RECEIPT = re.compile(RECEIPT_PREFIX + r"(tool:observation:[0-9a-f]{64}):([0-9a-f]{64})")
_REACTION_QUESTIONS = {
    "你觉得它哪里有意思", "你觉得它怎么样", "你觉得刚才那篇怎么样",
    "你怎么看刚才读的", "你怎么看刚才那篇", "你对刚才那篇有什么看法",
    "刚才那篇你怎么看", "刚才那篇哪里让你感兴趣", "你为什么对它感兴趣",
    "你喜欢它什么", "你喜欢刚才那篇的什么", "你最喜欢它哪一点",
}


def reading_reaction_question(text):
    if not isinstance(text, str) or len(text) > 80:
        return False
    text = unicodedata.normalize("NFKC", text).strip().rstrip("?？!！。~～").strip()
    return text in _REACTION_QUESTIONS


def discovery_period(text):
    if not isinstance(text, str) or len(text) > 64:
        return ""
    text = unicodedata.normalize("NFKC", text).strip().rstrip("?？!！。~～").strip()
    match = _QUESTION.fullmatch(text)
    if not match:
        return ""
    # A bare “看什么” may be asking for a recommendation, not a recent experience.
    if not match["period"] and not any(word in text for word in ("逛到", "刷到", "看到", "读到", "发现", "遇到", "看过", "读过")):
        return ""
    return {"今天": "today", "刚刚": "hour", "刚才": "hour", "这两天": "two_days"}.get(match["period"], "week")


def browse_receipt(record):
    if (not record or record.get("source_name") != "news" or not _public_source_url(record)):
        return ""
    return RECEIPT_PREFIX + record["id"] + ":" + source_fingerprint(record)


def parse_receipt(value):
    match = _RECEIPT.fullmatch(value) if isinstance(value, str) else None
    return match.groups() if match else None


def dated_reads(records, period, timezone, now):
    zone = ZoneInfo(timezone)
    today = datetime.fromtimestamp(now, zone).replace(hour=0, minute=0, second=0, microsecond=0)
    since = {"hour": now - 3600, "today": today.timestamp(), "two_days": now - 2 * 86400,
             "week": now - 7 * 86400}[period]
    return [{**record, "browse_date": datetime.fromtimestamp(record["browse_time"], zone).date().isoformat()}
            for record in records if since <= record["browse_time"] <= now]
