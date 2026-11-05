"""Owner-private learning and fixed communication defaults, subordinate to current intent."""
from __future__ import annotations

import asyncio
from pathlib import Path
import re
import time

from .habits_model import parse_habits_command, should_learn, situation_for, understand_habits
from .habits_store import HabitsStore
from .dialogue_policy import literal_mode
from .mcp_runtime import day_start, digest
from .mind import PRIVATE
from .owner import extract_sender, normalize_qq

_STYLES = {
    ("length", "short"): "默认简短作答，先说要点。",
    ("length", "balanced"): "默认用适中篇幅，按需要解释。",
    ("length", "detailed"): "默认给出较详细的解释与必要步骤。",
    ("support", "listen"): "默认先倾听和回应感受，不急着给建议。",
    ("support", "advice"): "默认在听懂情况后给具体建议，不替主人作决定。",
    ("support", "normal"): "默认自然回应，不固定扮演安慰者或建议者。",
    ("questions", "fewer"): "默认少追问，不用例行问题填满对话。",
    ("questions", "normal"): "有必要时自然提问，不强制每轮追问。",
}
_CURRENT = {
    "length": re.compile(r"简短|简洁|精简|详细|展开|字数|长一点|短一点|长点|短点|只说|直接说|先说结论|别啰嗦"),
    "support": re.compile(r"建议|办法|帮我|听我|倾诉|吐槽|安慰|怎么办|怎么做|如何处理"),
    "questions": re.compile(r"问我|提问|追问|少问|别问|不要问|不用问"),
}


class HabitsRuntime:
    def __init__(self, bot):
        self.bot = bot
        self.store = HabitsStore(Path(bot.ctx.paths.data_dir)/"habits.sqlite3", **bot._ledger_storage_options())
        self._lock = asyncio.Lock()
        self._generation = 0
        self.last_status = "idle"

    def reset(self):
        self._generation += 1

    def close(self):
        self.reset()
        self.store.close()

    def owner(self):
        b = self.bot
        return normalize_qq(b.config.owner.qq) if b.config.owner.enabled else ""

    def enabled(self):
        b = self.bot
        return bool(b.config.plugin.enabled and b.config.habits.enabled and b._unified_active() and self.owner())

    def signature(self):
        b = self.bot
        return digest([self._generation, self.owner(), b.config.plugin.enabled, b.config.habits.model_dump(),
                       b.config.unified_mind.model_dump(), b.config.general.timezone])

    def trusted(self, kwargs):
        b = self.bot
        if not self.enabled() or not b._unified or b._self_state.state()["owner_id"] != self.owner():
            return None
        request = dict(kwargs)
        if "message" in request:
            sender = extract_sender(request["message"])
            if (not sender or sender.kind != "private" or sender.user_id != self.owner()
                    or not sender.stream_id or not sender.message_id):
                return None
            request.update(session_id=sender.stream_id, reply_message_id=sender.message_id)
        context = b._unified.context(request)
        if not context or context["scope"] != "private:"+self.owner() or context["speaker_id"] != self.owner():
            return None
        # Independently require the latest private message even if another
        # subsystem has not enabled the shared latest_private_only switch.
        latest = b._unified.context({"session_id": context["sid"]})
        return context if latest and latest["mid"] == context["mid"] else None

    def capture(self, message):
        if (not self.enabled() or not isinstance(message, dict)
                or self.bot._self_state.state()["owner_id"] != self.owner()):
            return False, False
        text = message.get("processed_plain_text")
        command = parse_habits_command(text)
        if command:
            changed = self.store.command(message, owner_id=self.owner(), **command)
            if changed:
                self.reset()
                self.last_status = "explicit_command"
            return changed, False
        if not self.bot.config.habits.learn_from_feedback or not should_learn(text) or PRIVATE.search(text):
            return False, False
        queued = self.store.enqueue(message, owner_id=self.owner())
        if queued is None:
            self.last_status = "input_queue_full"
        return False, bool(queued)

    async def process(self):
        b = self.bot
        if not self.enabled() or not b.config.habits.learn_from_feedback or self._lock.locked():
            return
        async with self._lock:
            owner, signature = self.owner(), self.signature()
            claim = self.store.claim(owner, lease_seconds=min(600, max(120, b.config.unified_mind.model_timeout_seconds*2+30)))
            if not claim:
                self.last_status = "idle"
                return
            revision = claim["base_revision"]
            def valid():
                state = self.store.state(owner)
                return (self.enabled() and self.signature() == signature and state["revision"] == revision
                        and state["lease_id"] == claim["id"] and state["lease_token"] == claim["token"]
                        and state["lease_until"] > time.time() and b._self_state.state()["owner_id"] == owner)
            def release(reason, failed=False):
                try:
                    if failed:
                        self.store.fail(claim["id"], claim["token"], owner_id=owner, reason=reason)
                    else:
                        self.store.defer(claim["id"], claim["token"], owner_id=owner, reason=reason,
                                         retry_at=time.time()+max(60, b.config.habits.interval_seconds))
                except ValueError:
                    pass
            try:
                if not self.store.reserve_cycle(owner, day_start=day_start(b.config.general.timezone),
                        interval_seconds=b.config.habits.interval_seconds, daily_limit=b.config.habits.max_daily_batches):
                    self.last_status = "cycle_budget_or_cooldown"
                    release(self.last_status)
                    return
                exhausted = False
                def reserve():
                    nonlocal exhausted
                    allowed = b._self_state.reserve_thought(now=time.time(), day_start=day_start(b.config.general.timezone),
                        min_interval_seconds=0, daily_limit=b.config.unified_mind.max_daily_model_calls,
                        reserve_for_reply=b.config.unified_mind.reserved_reply_calls, purpose="background")
                    exhausted = not allowed
                    return allowed
                source = {key: claim[key] for key in ("id", "text", "timestamp")}
                proposal = await understand_habits(source, generate=b.ctx.llm.generate, reserve=reserve, valid=valid,
                    task_name=b.config.brain.model_task, timeout=b.config.unified_mind.model_timeout_seconds)
                if proposal is None or not valid():
                    self.last_status = "model_budget" if exhausted else "context_changed" if not valid() else "review_failed"
                    release(self.last_status, failed=self.last_status == "review_failed")
                    return
                result = self.store.complete(claim["id"], claim["token"], proposal, owner_id=owner,
                                             expected_revision=revision)
                self.last_status = "updated" if result["applied"] else "no_supported_preference"
            except asyncio.CancelledError:
                release("interrupted")
                raise
            except Exception:
                self.last_status = "habit_learning_failed"
                release(self.last_status, failed=True)

    def hint(self, kwargs, *, current_mode="normal"):
        context = self.trusted(kwargs)
        if not context:
            return ""
        query = context["query"]
        situation = situation_for(query)
        current_literal = literal_mode(query) is not None or query.strip().rstrip("。.!！?？～~") in {"随便聊聊", "/近况 正常"}
        rows = self.store.preferences(self.owner(), include_inactive=False)
        selected = {row["dimension"]: row for row in rows if row["situation"] == "general"}
        selected.update({row["dimension"]: row for row in rows if row["situation"] == situation})
        hints = [_STYLES[(dimension, row["value"])] for dimension, row in sorted(selected.items())
                 if (dimension, row["value"]) in _STYLES and not _CURRENT[dimension].search(query)
                 and not (dimension == "support" and (current_mode != "normal" or current_literal))]
        if not hints:
            return ""
        return "主人长期沟通默认（本轮明确要求、必要解释/澄清及已有相处边界优先）："+"".join(hints)

    def inspect(self, kwargs):
        if not self.trusted(kwargs):
            return {"success": False, "code": "owner_private_required"}
        rows = self.store.preferences(self.owner())
        status = self.store.status(self.owner())
        return {"success": True, "data": {
            "processing": {key: status[key] for key in ("queued_inputs", "input_capacity", "queue_full", "active_preferences", "pending_preferences")},
            "preferences": [{key: row[key] for key in ("situation", "dimension", "value", "status", "evidence", "evidence_count", "revision", "updated")}
                            | {"pending_counts": {value: len(sources) for value, sources in row["pending"].items()}} for row in rows],
            "instructions": "固定命令：/习惯 设置 <默认/工作/技术/倾诉/兴趣> <长度/回应/追问> <值>；/习惯 清除 <情境> <维度>。"
                            "长度：简短/适中/详细；回应：倾听/建议/正常；追问：少问/正常。明确设置优先于普通反馈。"}}
