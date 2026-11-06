"""Bounded, source-free working state and interpretable action valuation."""
from __future__ import annotations

import math

from .unified_memory import _terms


KINDS = {"reading": "继续阅读", "research": "查证问题", "project": "推进项目"}
REASONS = {
    "interest": "与已有兴趣相关", "emotion": "相关经历仍值得关注",
    "curiosity": "有尚待了解的内容", "patience": "适合持续梳理",
    "caution": "需要核查证据", "social": "适合形成可交流的理解",
    "continuity": "已有进行中的步骤", "capacity": "结合近期实际处理负荷",
    "experience": "参考同类尝试的实际结果", "alternative": "给其他任务一次机会",
    "commitment": "为已承诺的跟进留出处理余地",
}
INTENTS = {"conversation", "listen", "advice", "repair", "light", "media"}


def working_defaults():
    return {"receipt": "", "intent": "conversation", "focus": None, "pending": [],
            "pause_reason": "", "reasons": [], "updated": 0.}


def finite(value, fallback=0.):
    return float(value) if type(value) in (int, float) and math.isfinite(value) else fallback


def candidate_key(item):
    return item["kind"] + ":" + item["key"]


def capability_view(saved):
    """Report a smoothed observed progress rate, without inventing general skill."""
    result = {}
    for kind in KINDS:
        rows = saved.get(kind, []) if isinstance(saved, dict) else []
        rows = [row for row in rows if isinstance(row, dict)
                and row.get("outcome") in {"progress", "blocked", "failed"}][-24:]
        count = len(rows)
        progress = sum(row["outcome"] == "progress" for row in rows)
        failures = sum(row["outcome"] == "failed" for row in rows)
        recent = rows[-3:]
        result[kind] = {"attempts": count, "progress": progress, "failures": failures,
                        "progress_rate": round((progress+1)/(count+2), 4),
                        "recent_receipts": [row.get("receipt", "") for row in recent],
                        "needs_review": len(recent) == 3 and all(row["outcome"] != "progress" for row in recent)}
    return result


def assess(candidates, details, growth, state):
    """Value only admitted task candidates; these scores grant no permissions."""
    traits = growth.get("traits", {})
    learned = capability_view(state.get("capabilities", {}))
    load = max(0., min(1., finite(state.get("activity", {}).get("load"))))
    interests = [(set(_terms(row.get("topic", ""))), max(-1., min(1., finite(row.get("weight")))))
                 for row in growth.get("interests", []) if row.get("status") in (None, "active")]
    emotions = growth.get("emotions", [])
    values = {}
    for item in candidates:
        key, kind = candidate_key(item), item["kind"]
        detail = details.get(key, {})
        terms = set(_terms(detail.get("topic", "")))
        reasons = []
        score = {"reading": .15, "research": .1, "project": .3}[kind]
        affinity = sum(weight for words, weight in interests if terms & words)
        score += .4 * max(-1., min(1., affinity))
        if affinity > 0:
            reasons.append("interest")
        mood = 0.
        for feeling in emotions:
            if terms & set(_terms(feeling.get("topic", ""))):
                influence = {"curious": .25, "appreciative": .15, "concerned": .2,
                             "uncertain": .2, "disappointed": -.08, "relieved": -.05,
                             "satisfied": -.05}.get(feeling.get("feeling"), 0.)
                mood += influence * max(0., min(1., finite(feeling.get("intensity"))))
        score += max(-.15, min(.25, mood))
        if mood > 0:
            reasons.append("emotion")
        trait = "curiosity" if kind == "research" else "patience" if kind == "reading" else "sociability"
        strength = max(0., min(1., finite(traits.get(trait), .5)))
        score += .2*(strength-.5)
        if strength > .55:
            reasons.append({"sociability": "social"}.get(trait, trait))
        if detail.get("stage") == "review":
            score += .2*max(0., min(1., finite(traits.get("caution"), .5)))
            reasons.append("caution")
        if detail.get("in_progress"):
            score += .1
            reasons.append("continuity")
        cost = {"reading": .35, "research": .7, "project": .6}[kind]
        score -= .3*cost*load
        if load:
            reasons.append("capacity")
        if state.get("pending_commitments", 0):
            score -= .15*cost
            reasons.append("commitment")
        experience = learned[kind]
        if experience["attempts"] >= 3:
            score += .2*(experience["progress_rate"]-.5)
            reasons.append("experience")
        values[key] = {"score": round(score, 6), "reasons": reasons[-6:]}
    return values


def working_hint(snapshot):
    """Use only finite control facts here; detailed memories use expression review."""
    if not snapshot:
        return ""
    phrases = ["先完成当前可信消息的要求，结合本轮已获准的资料回答。"]
    focus = snapshot.get("focus")
    if focus:
        name = KINDS[focus["kind"]]
        if snapshot.get("pause_reason") == "owner_message":
            phrases.append("后台的"+name+"已让位于当前对话，保留了可核验的任务位置。")
        elif snapshot.get("pause_reason") == "recovering":
            phrases.append("最近实际处理负荷较高，后台"+name+"暂缓。")
        else:
            phrases.append("后台关注为"+name+"，其当前阶段以任务账本为准。")
    if snapshot.get("pending_commitments", 0):
        phrases.append("已有成功送达的任务跟进承诺仍待回告，具体内容需通过原任务记录核验。")
    if snapshot.get("open_questions", 0):
        phrases.append("存在尚待核实的问题，证据不足时保留疑问。")
    if snapshot.get("plans"):
        phrases.append("有主人已确认且仍有效的共同计划，可按当前话题接回原计划。")
    if snapshot.get("review_kinds"):
        names = "、".join(KINDS[kind] for kind in snapshot["review_kinds"])
        phrases.append("最近三次"+names+"都未取得确认进展，可换方法或在相关时向主人询问缺少的条件。")
    phrases.append("只在当前话题相关时提及这些工作状态，具体任务内容、能力判断与完成情况仍需原始记录支持。")
    return "[MaiLivingWorld:working-memory]\n" + "".join(phrases)
