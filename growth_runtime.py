"""Bounded, recoverable learning. Persists conclusions/jobs, never hidden reasoning."""
from __future__ import annotations

import asyncio
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
                result = await request(build_growth_prompt(records, state, review_beliefs=review_beliefs), budget.min_interval_seconds)
                proposal = parse_growth(result, records, review_beliefs=review_beliefs, state=state if review_beliefs else None)
                if proposal is None:
                    raise ValueError("invalid_proposal")
                reviewed = await request(build_growth_review_prompt(proposal, records, review_beliefs=review_beliefs, state=state), 0)
                proposal = review_growth(reviewed, proposal, review_beliefs=review_beliefs)
                if proposal is None:
                    raise ValueError("invalid_review")
                # A rollback, retraction or concurrent writer invalidates the planned update.
                if not current() or self.store.state()["revision"] != revision:
                    raise _Changed()
                records = sources()
                self.last_counts = self.store.complete(job_id, token, proposal, records,
                                                       expected_revision=revision, review_beliefs=review_beliefs)
                self.last_status = "observed" if mode == "observe" else "learned"
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


def style_hint(state: dict) -> str:
    """Only fixed style phrases leave this boundary, never private source/trait reasons."""
    traits, mood = state.get("traits", {}), state.get("mood", {})
    phrases = []
    rules = {
        "curiosity": ("对新主题保持好奇，可适度追问。", "先理解当前话题，减少无关探索。"),
        "sociability": ("可以自然分享已获准表达的想法。", "偏好简洁交流，尊重对方节奏。"),
        "patience": ("给对方充分解释空间，耐心回应。", "优先说明重点，仍保持礼貌。"),
        "caution": ("对不确定内容谨慎措辞，区分观点和事实。", "可以直接表达判断，同时承认不确定性。"),
    }
    for key, (high, low) in rules.items():
        value = traits.get(key, .5)
        if value >= .55:
            phrases.append(high)
        elif value <= .45:
            phrases.append(low)
    if mood.get("valence", 0) >= .15:
        phrases.append("当前语气可略轻快。")
    elif mood.get("valence", 0) <= -.15:
        phrases.append("当前语气可平静克制，避免把情绪发泄给他人。")
    if mood.get("energy", .5) <= .35:
        phrases.append("当前倾向简短回应。")
    if not phrases:
        return ""
    return "[MaiLivingWorld:grown-style]\n" + "".join(phrases) + "这些是缓慢成长的表达倾向，不覆盖唯一主人关系和本轮表达边界。"
