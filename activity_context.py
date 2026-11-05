"""Finite questions and truthful activity wording, independent of model guesses."""
from __future__ import annotations

import math
import re


ACTIVITY_TTL = 30 * 60
LOAD_STEP = .25
RECOVERY_SECONDS = 3600
LOAD_LIMIT = .75
_QUESTIONS = {"你在做什么", "你现在在做什么", "你在忙什么", "你现在在忙什么",
              "你刚才在做什么", "你刚才在忙什么", "刚才在忙什么", "你的进展怎么样",
              "你那边进展怎么样"}
_KINDS = {"reading": "阅读资料", "research": "查找资料", "project": "推进自己的小项目"}


def activity_question(text):
    if (not isinstance(text, str) or not 0 < len(text) <= 60
            or any(ord(c) < 32 for c in text)):
        return False
    return re.sub(r"[。！？!?]+$", "", text.strip()) in _QUESTIONS


def load_view(saved, now):
    """Read-only linear recovery; backward wall clocks never create recovery."""
    if not isinstance(saved, dict):
        raise ValueError("invalid_activity_load")
    load, at = saved.get("load", 0.), saved.get("at", 0.)
    if (isinstance(load, bool) or not isinstance(load, (int, float)) or not 0 <= load <= 1
            or not math.isfinite(load) or isinstance(at, bool) or not isinstance(at, (int, float))
            or not 0 <= at <= 1e12 or not math.isfinite(at)):
        raise ValueError("invalid_activity_load")
    value = round(max(0., load-max(0., now-at)*LOAD_STEP/RECOVERY_SECONDS), 8)
    return {"load": value, "at": at, "recovering": value >= LOAD_LIMIT}


def activity_hint(kind=None, outcome="unknown", *, recovering=False):
    name = _KINDS.get(kind)
    descriptions = {
        "working": "正在进行这一步，尚无完成记录。",
        "interrupted": "这一步已暂停或中断，不能说已经完成。",
        "unfinished": "之前开始过这一步，但没有可核验的完成记录，不能说现在仍在进行。",
        "progress": "最近完成了一步处理，不代表整项工作完成、已经理解全文或问题已有答案。",
        "blocked": "最近尝试了这一步，但没有确认推进成功。",
        "failed": "最近这一步没有成功，不编造失败原因。",
    }
    status = name+"："+descriptions[outcome] if name and outcome in descriptions else "没有可核验的近期活动可供说明。"
    if recovering and name and outcome in descriptions:
        status += "近期实际处理过几步资料，新的后台探索暂缓，当前对话照常。"
    return ("[MaiLivingWorld:actual-activity]\n对方正在询问你的近期活动。仅依据以下程序实际记录自然回答，"
            "不要报内部状态字段，不补写作品名、正文、工具编号、已完成承诺或线下身体生活。"
            "记录不足就简短说明不确定；不把程序负荷称为身体疲惫，不责怪对方打断或要求对方等待、陪伴。"
            "这不是新的事实记忆或性格证据，也不覆盖本轮明确请求。\n"+status)
