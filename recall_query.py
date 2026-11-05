"""Bounded lexical recall helpers; ranking never changes facts or permissions.

Related words only broaden candidate retrieval. Time cues only rank records with
explicit event dates; a source's storage/update timestamp is never an event date.
These helpers do not resolve pronouns, finish tasks or grant expression permission.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import math
import re
import unicodedata
from zoneinfo import ZoneInfo


MAX_QUERY_CHARS = 1200
MAX_RECORDS = 1000
_RELATED = (
    ("面试", "应聘", "求职面谈"),
    ("考试", "测验", "考核"),
    ("难过", "心情低落", "伤心"),
    ("开心", "高兴", "愉快"),
    ("担心", "忧虑", "挂念"),
    ("旅行", "旅游", "出游"),
    ("见面", "碰面", "会面"),
    ("失眠", "睡不着", "难以入睡"),
)
_STOP = {"这个", "那个", "什么", "怎么", "一下", "一个", "我们", "你们", "他们", "可以", "请问", "记得"}
_RELATIVE = {"今天": 0, "昨天": -1, "前天": -2, "明天": 1, "后天": 2}
_DATE = re.compile(r"(?<!\d)(\d{4})[-年/](\d{1,2})[-月/](\d{1,2})(?:日)?(?!\d)")
_META_DATE = re.compile(r"(?:收到于|记录于|原消息时间|记录时间|更新时间|updated_at|updated|timestamp)"
                        r"\s*[:：]?\s*\d{4}[-年/]\d{1,2}[-月/]\d{1,2}(?:日)?"
                        r"(?:[ T]\d{1,2}:\d{2}(?::\d{2})?(?:\s*UTC|Z|[+-]\d{2}:?\d{2})?)?", re.I)


def _query(query) -> str:
    if not isinstance(query, str):
        return ""
    return " ".join(query.split())[:600]


def _norm(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _extras(query: str) -> list[str]:
    normalized = _norm(query)
    result = []
    for group in _RELATED:
        if any(term in normalized for term in group):
            result.extend(term for term in group if term not in normalized)
    return result[:16]


def expand_query(query: str) -> str:
    """Append a small fixed related-word vocabulary, never rewrite the source."""
    text = _query(query)
    return (text + (" " + " ".join(_extras(text)) if _extras(text) else ""))[:MAX_QUERY_CHARS] if text else ""


def _terms(value: str) -> set[str]:
    found = set()
    for part in re.findall(r"[\u3400-\u9fff]+|[a-z0-9_]+", _norm(value)):
        tokens = (part[i:i+2] for i in range(len(part)-1)) if re.fullmatch(r"[\u3400-\u9fff]+", part) else [part]
        for token in tokens:
            if token not in _STOP:
                found.add(token)
            if len(found) >= 64:
                return found
    return found


def _dates(text: str) -> set[date]:
    result = set()
    for match in _DATE.finditer(text):
        try:
            result.add(date(*(int(part) for part in match.groups())))
        except ValueError:
            continue
    return result


def _clock(now, timezone: str) -> datetime:
    try:
        tz = ZoneInfo(timezone)
        if isinstance(now, datetime):
            if now.tzinfo is None:
                raise ValueError("invalid_time")
            return now.astimezone(tz)
        if type(now) not in (int, float) or not math.isfinite(now) or not 0 <= now <= 1e12:
            raise ValueError("invalid_time")
        return datetime.fromtimestamp(now, tz)
    except (TypeError, KeyError, ValueError, OverflowError, OSError) as exc:
        raise ValueError("invalid_recall_time") from None


def _event_dates(record: dict, text: str, now: datetime) -> set[date]:
    # Generated receipt headers in continuity records are observation times.
    # The remaining text dates are lexical candidates, not verified event claims.
    result = _dates(_META_DATE.sub("", text))
    occurred = record.get("occurred_on")
    if isinstance(occurred, str) and len(occurred) <= 32:
        result.update(_dates(occurred))
    for field in ("event_timestamp", "happened"):
        stamp = record.get(field)
        try:
            if type(stamp) in (int, float) and math.isfinite(stamp) and 0 < stamp <= 1e12:
                result.add(datetime.fromtimestamp(stamp, now.tzinfo).date())
        except (OverflowError, OSError, ValueError):
            continue
    return result


def rank_records(records: list[dict], query: str, now, timezone: str) -> list[dict]:
    """Return the same record objects in relevance order; stable input-order ties.

    Accept at most 1000 dict records. Invalid collection/time raises ValueError;
    empty/invalid query preserves order. No field (including score, timestamps,
    source references or sensitivity) is added, normalized, removed or changed.
    Time can only break lexical relevance ties or boost already related records.
    """
    if not isinstance(records, (list, tuple)) or len(records) > MAX_RECORDS or any(not isinstance(row, dict) for row in records):
        raise ValueError("invalid_recall_records")
    current = _clock(now, timezone)
    clean = _query(query)
    if not clean:
        return list(records)
    # Do not create misleading Chinese bigrams across "今天/明天" and the
    # following topic; relative words are evaluated separately as time cues.
    lexical_query = re.sub(r"今天|昨天|前天|明天|后天|最近|这几天", " ", clean)
    direct = _terms(lexical_query)
    extra = _terms(" ".join(_extras(clean))) - direct
    normalized = _norm(clean)
    query_dates = _dates(normalized)
    for word, offset in _RELATIVE.items():
        if word in normalized:
            query_dates.add(current.date()+timedelta(days=offset))
    if "最近" in normalized or "这几天" in normalized:
        query_dates.update(current.date()-timedelta(days=i) for i in range(7))

    def score(row):
        text = row.get("text", "")
        text = text[:4000] if isinstance(text, str) else ""
        body = _norm(text)
        lexical = 4*sum(term in body for term in direct) + sum(term in body for term in extra)
        if 2 <= len(normalized) <= 80 and normalized in body:
            lexical += 8
        time_match = bool(lexical and query_dates & _event_dates(row, text, current))
        return lexical, time_match

    return sorted(records, key=score, reverse=True)
