"""Finite reactions to original events; a reaction is not evidence about the world."""
from __future__ import annotations

import copy
import hashlib
import math
import re

from .belief_revision import identities, normalized


FEELINGS = {"curious": "好奇", "appreciative": "欣赏", "disappointed": "失望",
            "concerned": "牵挂", "relieved": "释然", "satisfied": "满足", "uncertain": "犹豫"}
FIELDS = {"topic", "feeling", "appraisal", "intensity", "evidence_ids", "quotes"}
HALF_LIFE = 24 * 3600
TTL = 7 * 86400
MAX_ITEMS = 16
MIN_INTENSITY = .03
_ABSENCE = re.compile(r"(?:不|没|没有|未)(?:再)?(?:回复|回消息|理我|理你|联系)|(?:主人|你|对方).{0,8}(?:离开|缺席|不在)|冷落|抛弃")
_BODY = re.compile(r"(?:我|自己).{0,4}(?:心跳|流泪|哭了|失眠|睡不着|肚子饿|身体|吃饭|睡觉)")


def finite(value, low, high):
    return type(value) in (int, float) and low <= value <= high and math.isfinite(value)


def receipt_keys(record):
    """Origin and normalized content protect against aliases and cosmetic replays."""
    origin, content = identities(record)
    return {"emotion:origin:" + origin, "emotion:content:" + content}


def normalize_candidate(item, records=None):
    # The same bounded text validation runs at both parser and transaction edges.
    from .growth_model import _safe_text
    if not isinstance(item, dict) or set(item) != FIELDS:
        return None
    topic = _safe_text(item["topic"], 40, records)
    appraisal = _safe_text(item["appraisal"], 120, records)
    feeling, intensity, ids, quotes = item["feeling"], item["intensity"], item["evidence_ids"], item["quotes"]
    if (not topic or not appraisal or not isinstance(feeling, str) or feeling not in FEELINGS
            or not finite(intensity, MIN_INTENSITY, .6) or _ABSENCE.search(appraisal) or _BODY.search(appraisal)
            or not isinstance(ids, list) or not 1 <= len(ids) <= 3
            or any(not isinstance(sid, str) or not 1 <= len(sid) <= 240 for sid in ids)
            or len(set(ids)) != len(ids) or not isinstance(quotes, list) or len(quotes) != len(ids)):
        return None
    seen = set()
    for quote in quotes:
        if (not isinstance(quote, dict) or set(quote) != {"source_id", "quote"}
                or not isinstance(quote["source_id"], str) or quote["source_id"] not in ids
                or quote["source_id"] in seen or not isinstance(quote["quote"], str)
                or not 4 <= len(quote["quote"]) <= 180 or any(ord(c) < 32 for c in quote["quote"])):
            return None
        seen.add(quote["source_id"])
        if records is not None:
            record = records.get(quote["source_id"])
            # An owner's reported mood or preferences are not this agent's own reaction.
            if (not record or record.get("source") not in {"brain_event", "cognition", "story", "tool"}
                    or record.get("kind") == "preference" or record.get("event_kind") == "preference"
                    or record.get("evidence_status") == "owner_reported"
                    or record.get("text_truncated") is True
                    or record.get("derived") is True or record["text"].count(quote["quote"]) != 1
                    or _ABSENCE.search(record["text"])):
                return None
    return {"topic": topic, "feeling": feeling, "appraisal": appraisal, "intensity": float(intensity),
            "evidence_ids": list(ids), "quotes": copy.deepcopy(quotes)}


def active_items(payload, now):
    """Read-only decay; no clock tick rewrites history or extends an episode."""
    rows = payload.get("emotions", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list) or not finite(now, 0, 1e15):
        return []
    result = []
    for row in rows[:MAX_ITEMS]:
        if (not isinstance(row, dict) or row.get("status") != "active"
                or not finite(row.get("created"), 0, now)
                or not finite(row.get("expires"), now, 1e15) or row["expires"] <= now
                or row["expires"] != row["created"] + TTL
                or not finite(row.get("intensity"), MIN_INTENSITY, .6)):
            continue
        intensity = round(row["intensity"] * 2 ** (-(now-row["created"])/HALF_LIFE), 8)
        if intensity >= MIN_INTENSITY:
            result.append({**copy.deepcopy(row), "intensity": intensity, "confidence": .6})
    return result


def apply_candidates(before, proposals, allowed, records, consumed, now):
    if not isinstance(proposals, list) or len(proposals) > 2:
        raise ValueError("invalid_emotions")
    # Drop faded episodes only when committing a real new reaction. History keeps
    # the previous snapshot; origin/content receipts remain durable independently.
    live = {row["key"] for row in active_items(before, now)}
    kept = [copy.deepcopy(row) for row in before.get("emotions", []) if row.get("key") in live]
    new, used, consumed_keys, seen = [], set(), set(), set()
    for proposed in proposals:
        item = normalize_candidate(proposed, records)
        if item is None or any(sid not in allowed for sid in item["evidence_ids"]):
            raise ValueError("invalid_emotion_evidence")
        pair = (normalized(item["topic"]), item["feeling"])
        if pair in seen:
            raise ValueError("duplicate_emotion")
        seen.add(pair)
        ids = item["evidence_ids"]
        fresh = [sid for sid in ids if sid not in consumed and not receipt_keys(records[sid]) & consumed
                 and finite(records[sid].get("timestamp", 0), max(0, now-TTL), now)]
        if not fresh:
            continue
        refs = [copy.deepcopy(allowed[sid]) for sid in sorted(ids)]
        identity = "|".join(sorted(ref["id"]+":"+ref["digest"] for ref in refs))
        key = "e:"+hashlib.sha256((identity+"|"+pair[0]+"|"+pair[1]).encode()).hexdigest()[:24]
        if any(row["key"] == key for row in kept):
            continue
        new.append({"key": key, "topic": item["topic"], "text": item["appraisal"], "feeling": item["feeling"],
                    "intensity": item["intensity"], "created": now, "expires": now+TTL,
                    "status": "active", "revision": 1, "sources": refs, "quotes": item["quotes"]})
        used.update(ids)
        consumed_keys.update(ids)
        for sid in ids:
            consumed_keys.update(receipt_keys(records[sid]))
    combined = sorted(kept+new, key=lambda row: (-row["created"], row["key"]))[:MAX_ITEMS]
    return combined, len(new), used, consumed_keys


def expression_detail(item):
    """Discrete wording prevents continuous decay from invalidating every await."""
    feeling, intensity = item.get("feeling"), item.get("intensity")
    if not isinstance(feeling, str) or feeling not in FEELINGS or not finite(intensity, MIN_INTENSITY, .6):
        return None
    band = .3 if intensity >= .3 else .1 if intensity >= .1 else .03
    strength = "仍有些" if band == .3 else "还有一点" if band == .1 else "已渐淡的"
    return strength+FEELINGS[feeling], band


PROMPT_RULES = (
    "本轮启用emotions，最多2项。每项是智能体面对某个具体已知事件/已读取材料的有限反应，"
    "字段topic、feeling、appraisal、intensity、evidence_ids、quotes。topic最多40字；"
    "appraisal最多120字，描述具体对象为何引起这种反应，保留材料/虚构作品/自述的归属，不编造经历；"
    "feeling只允许curious/appreciative/disappointed/concerned/relieved/satisfied/uncertain；"
    "intensity为0.03到0.6。可对同一事情同时欣赏和失望，最多两种感受，不强求单一正负心情。"
    "每项引用1至3条原始来源，每条quotes为source_id和4至180字唯一逐字quote，保留否定与限定。"
    "只根据实际互动、查阅材料或已记录观察形成反应；标有text_truncated的截断资料不能支持emotions。知道主人情绪不等于你的情绪，"
    "主人自述或喜好不能单独变成自身反应，不因主人没回复/离开而失落、责备或惩罚。"
    "不虚构身体感觉、已完成行动、道歉、和好或受到伤害，不从旧情绪自己生成新的情绪。"
    "无依据输出空emotions，不要为了像人而制造反应。"
)
REVIEW_RULES = (
    "emotions逐项核对具体事件对象、完整原始来源和逐字quotes：反应必须针对实际给出的经历或材料，"
    "保持自述与虚构材料归属，不能把主人自己的心情/偏好移植给智能体，不能因缺席或不回复制造负面反应。"
    "不得把猜测的执行结果、身体经历、他人心理或外部真相写成已发生；同一对象允许有限混合感受，"
    "不确定就不批准。批准仅表示有限的自我反应候选，不证明意识、外部事实或允许向任何听众公开。"
)
