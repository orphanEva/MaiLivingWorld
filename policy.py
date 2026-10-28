"""纯 Python 策略函数，和 MaiBot SDK 解耦，方便离线单测。"""
from __future__ import annotations

import json
import re
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo


def parse_targets(items: list[str]) -> list[tuple[str, str]]:
    """显式白名单：只允许合法 platform:user_id，不猜测目标。"""
    result: list[tuple[str, str]] = []
    seen = set()
    for item in items:
        if not isinstance(item, str):
            continue
        value = item.strip()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}:[A-Za-z0-9_-]{1,80}", value):
            continue
        platform, user_id = value.split(":", 1)
        if platform != "qq":
            continue  # 第一版只准给明确白名单里的 QQ 私聊发起唤醒
        if (platform, user_id) not in seen:
            result.append((platform, user_id))
            seen.add((platform, user_id))
    return result


def is_quiet(now: datetime, start: str, end: str) -> bool:
    """支持跨午夜，开始时间等于结束时间代表关闭免打扰。"""
    try:
        start_t = time.fromisoformat(start)
        end_t = time.fromisoformat(end)
    except ValueError:
        return True  # 非法时间保守地不打扰
    if start_t == end_t:
        return False
    current = now.time().replace(tzinfo=None)
    if start_t < end_t:
        return start_t <= current < end_t
    return current >= start_t or current < end_t


def local_day(timestamp: float, timezone: str) -> str:
    return datetime.fromtimestamp(timestamp, ZoneInfo(timezone)).date().isoformat()


def can_attempt(entry: dict[str, Any], now_ts: float, timezone: str,
                min_gap_hours: float, daily_limit: int) -> tuple[bool, str]:
    if now_ts - float(entry.get("last_attempt_at") or 0) < max(0.0, min_gap_hours) * 3600:
        return False, "cooldown"
    today = local_day(now_ts, timezone)
    n = int(entry.get("daily_count") or 0) if entry.get("day") == today else 0
    if n >= max(0, daily_limit):
        return False, "daily_limit"
    return True, "ok"


def record_attempt(entry: dict[str, Any], now_ts: float, timezone: str) -> dict[str, Any]:
    today = local_day(now_ts, timezone)
    return {
        "day": today,
        "daily_count": (int(entry.get("daily_count") or 0) if entry.get("day") == today else 0) + 1,
        "last_attempt_at": now_ts,
    }


def extract_text(response: Any) -> str:
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        for key in ("content", "text", "response", "output", "result"):
            value = response.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, dict):
                nested = extract_text(value)
                if nested:
                    return nested
        choices = response.get("choices")
        if isinstance(choices, list) and choices:
            return extract_text(choices[0])
        if isinstance(response.get("message"), dict):
            return extract_text(response["message"])
    return ""


def parse_decision(response: Any) -> tuple[bool, str]:
    """仅接受明确 JSON；模型输出不确定时绝不主动打扰。"""
    if isinstance(response, dict) and response.get("success") is False:
        return False, "gate_api_error"
    content = extract_text(response).strip()
    if not content:
        return False, "empty_gate_response"
    match = re.search(r"\{[\s\S]*\}", content)
    if not match:
        return False, "invalid_gate_json"
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return False, "invalid_gate_json"
    if not isinstance(obj, dict) or obj.get("reach_out") is not True:
        return False, "model_declined"
    topic = obj.get("topic", "")
    if not isinstance(topic, str) or not (3 <= len(topic.strip()) <= 150):
        return False, "missing_topic"
    return True, topic.strip()
