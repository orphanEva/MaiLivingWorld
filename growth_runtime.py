"""Bounded, recoverable learning. Persists conclusions/jobs, never hidden reasoning."""
from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .growth_model import build_growth_prompt, build_growth_review_prompt, parse_growth, review_growth
from .growth_store import source_fingerprint


class _Changed(Exception):
    pass


class _Budget(Exception):
    pass


class GrowthRuntime:
    def __init__(self, store, identity):
        self.store, self.identity = store, identity
        self._lock = asyncio.Lock()
        self._generation = 0
        self.last_status = "idle"
        self.last_counts = {}

    def reset(self):
        self._generation += 1

    async def tick(self, config, budget, *, collect, resolve, snapshot, generate,
                   allowed, task_name: str, timezone: str):
        """At most one leased task per tick; restarted processes reclaim expired leases."""
        if not config.enabled or not allowed() or self._lock.locked():
            return
        async with self._lock:
            generation, mode = self._generation, config.mode
            review_beliefs = getattr(config, "review_beliefs", False)
            emotional_episodes = getattr(config, "emotional_episodes", False)
            owner = self.identity.state()["owner_id"]
            if not owner:
                self.last_status = "owner_required"
                return
            now = time.time()
            job = self.store.claim(now=now, mode=mode)
            if not job:
                status = self.store.status()
                if now - status.get("last_enqueued", 0) < config.interval_seconds:
                    return
                self.store.enqueue(collect(), mode=mode, now=now)
                job = self.store.claim(now=now, mode=mode)
            if not job:
                self.last_status = "no_new_evidence"
                return
            job_id, token = job["id"], job["token"]

            def release(reason, delay=None):
                try:
                    if delay is None:
                        self.store.fail(job_id, token, reason)
                    else:
                        self.store.defer(job_id, token, reason, time.time() + delay)
                except ValueError as exc:
                    # A rollback/retirement or another worker can revoke this lease.
                    # Its new owner controls scheduling; never overwrite that state.
                    if str(exc) != "lease_lost":
                        raise

            def current():
                return (generation == self._generation and config.enabled and config.mode == mode
                        and getattr(config, "review_beliefs", False) == review_beliefs
                        and getattr(config, "emotional_episodes", False) == emotional_episodes
                        and allowed() and self.identity.state()["owner_id"] == owner)

            async def request(prompt, interval):
                if not current():
                    raise _Changed()
                stamp = time.time()
                day = datetime.fromtimestamp(stamp, ZoneInfo(timezone)).replace(
                    hour=0, minute=0, second=0, microsecond=0).timestamp()
                if not self.identity.reserve_thought(now=stamp, day_start=day,
                        min_interval_seconds=interval, daily_limit=budget.max_daily_model_calls,
                        reserve_for_reply=getattr(budget, "reserved_reply_calls", 0), purpose="background"):
                    raise _Budget()
                result = await asyncio.wait_for(generate(prompt, task_name=task_name,
                    temperature=0.1, max_tokens=1800), timeout=budget.model_timeout_seconds)
                if not current():
                    raise _Changed()
                return result

            try:
                source_ids = [ref["id"] for ref in job["sources"]]
                def sources():
                    resolved = resolve(source_ids)
                    if any(ref["id"] not in resolved or source_fingerprint(resolved[ref["id"]])
                           != ref["digest"] for ref in job["sources"]):
                        raise ValueError("source_changed")
                    return [resolved[ident] for ident in source_ids]

                records = sources()
                state = snapshot()
                revision = state["revision"]
                # Textual prior conclusions may enter this request only together with
                # all their root evidence. This prevents unseen private state from
                # being re-attributed to the new batch's public sources.
                available = {item["id"]: source_fingerprint(item) for item in records}
                state = dict(state)
                for section in ("interests", "beliefs", "knowledge", "questions"):
                    state[section] = [item for item in state[section] if item["sources"] and all(
                        available.get(ref["id"]) == ref["digest"] for ref in item["sources"])]
                if emotional_episodes:
                    state["emotions"] = [item for item in state.get("emotions", []) if item["sources"] and all(
                        available.get(ref["id"]) == ref["digest"] for ref in item["sources"])]
                result = await request(build_growth_prompt(records, state, review_beliefs=review_beliefs,
                    emotional_episodes=emotional_episodes), budget.min_interval_seconds)
                proposal = parse_growth(result, records, review_beliefs=review_beliefs, state=state,
                    emotional_episodes=emotional_episodes)
                if proposal is None:
                    raise ValueError("invalid_proposal")
                reviewed = await request(build_growth_review_prompt(proposal, records, review_beliefs=review_beliefs, state=state,
                    emotional_episodes=emotional_episodes), 0)
                proposal = review_growth(reviewed, proposal, review_beliefs=review_beliefs, emotional_episodes=emotional_episodes)
                if proposal is None:
                    raise ValueError("invalid_review")
                # A rollback, retraction or concurrent writer invalidates the planned update.
                if not current() or self.store.state()["revision"] != revision:
                    raise _Changed()
                records = sources()
                self.last_counts = self.store.complete(job_id, token, proposal, records,
                    expected_revision=revision, review_beliefs=review_beliefs, emotional_episodes=emotional_episodes,
                    skip_interest_overflow=True)
                self.last_status = "observed" if mode == "observe" else "learned"
                if mode == "active" and self.last_counts.get("skipped_interest_overflow") and not self.last_counts["changed"]:
                    self.last_status = "interest_source_capacity"
            except _Budget:
                release("budget_or_cooldown", max(60, budget.min_interval_seconds))
                self.last_status = "budget_or_cooldown"
            except _Changed:
                release("context_changed", 60)
                self.last_status = "context_changed"
            except asyncio.CancelledError:
                release("interrupted", 5)
                self.last_status = "interrupted"
                raise
            except Exception:
                # Providers can include source content in exceptions; persist only a fixed code.
                release("learning_failed")
                self.last_status = "learning_failed"


_TENSIONS = {
    "curiosity": (
        "好奇时更容易被新细节吸引，也容易展开得太远；相关时可以轻提一个想继续了解的方向，先接住眼前的话题。",
        "偏爱熟悉的方向，接触新话题时可能需要多一点铺垫；可以保留偏好，也给眼前的新内容一次了解的机会。"),
    "sociability": (
        "更愿意分享自己的反应，有时会想多说几句；给对方接话的空间，不把交流变成连续自述。",
        "表达更内敛，有时需要对方问到才展开；可以简短真诚，明确被问到时仍把意思说清。"),
    "patience": (
        "愿意慢慢理解和展开细节，也容易解释过多；对方只要重点时先给重点，确有需要再展开。",
        "倾向先抓重点，有时容易略过铺垫；可以直接说当前看法，遇到复杂感受时先听完整再下结论。"),
    "caution": (
        "习惯先看限定条件，有时不容易马上拿定主意；确实拿不准时可以承认犹豫，并说明当前能判断的部分。",
        "较愿意先给出判断，有时需要再核对细节；保留直接表达的风格，依据不足处仍说清不确定。"),
}


def _number(mapping, key, default, low=0., high=1.):
    value = mapping.get(key, default) if isinstance(mapping, dict) else default
    return value if type(value) in (int, float) and low <= value <= high and math.isfinite(value) else default


def style_hint(state: dict, *, personality_tensions=False) -> str:
    """Only fixed style phrases leave this boundary, never private source/trait reasons."""
    if not isinstance(state, dict):
        return ""
    traits, mood = state.get("traits", {}), state.get("mood", {})
    phrases = []
    rules = {
        "curiosity": ("对新主题保持好奇，可适度追问。", "先理解当前话题，减少无关探索。"),
        "sociability": ("可以自然分享已获准表达的想法。", "偏好简洁交流，尊重对方节奏。"),
        "patience": ("给对方充分解释空间，耐心回应。", "优先说明重点，仍保持礼貌。"),
        "caution": ("对不确定内容谨慎措辞，区分观点和事实。", "可以直接表达判断，同时承认不确定性。"),
    }
    for key, (high, low) in rules.items():
        value = _number(traits, key, .5)
        if value >= .55:
            phrases.append(high)
        elif value <= .45:
            phrases.append(low)
    if _number(mood, "valence", 0, -1., 1.) >= .15:
        phrases.append("当前语气可略轻快。")
    elif _number(mood, "valence", 0, -1., 1.) <= -.15:
        phrases.append("当前语气可平静克制，避免把情绪发泄给他人。")
    if _number(mood, "energy", .5) <= .35:
        phrases.append("当前倾向简短回应。")
    if personality_tensions is True:
        # Stable, bounded choice from existing traits; no random flaws or new state.
        ranked = sorted(enumerate(_TENSIONS), key=lambda pair: (-abs(_number(traits, pair[1], .5)-.5), pair[0]))
        selected = []
        for _, key in ranked:
            value = _number(traits, key, .5)
            if value >= .55 or value <= .45:
                selected.append(_TENSIONS[key][0 if value >= .55 else 1])
        if selected:
            phrases.append("[MaiLivingWorld:personality-tensions]\n"+"".join(selected[:2])+
                "这些是已有性格的表达取舍，只在当前内容相关时自然体现，不必每轮展示或自报性格。"
                "当前明确请求、纠正、倾诉与相处边界优先；不为表现缺点而故意犯错、遗漏关键事实、制造争执或冷落对方。"
                "犹豫和没把握须来自实际信息不足，不能编造错误、身体感受、共同经历或把责任推给对方。")
    if not phrases:
        return ""
    return "[MaiLivingWorld:grown-style]\n" + "".join(phrases) + "这些是缓慢成长的表达倾向，不覆盖唯一主人关系和本轮表达边界。"
