"""One bounded foreground/background focus; references are not new memories."""
from __future__ import annotations

import asyncio
from pathlib import Path
import time

from .attention_store import AttentionStore
from .activity_context import ACTIVITY_TTL, activity_question, activity_hint
from .dialogue_policy import (understand_dialogue, dialogue_hint, literal_mode,
                             should_classify, current_reaction, current_reaction_hint)
from .mcp_runtime import digest, day_start
from .mind import PRIVATE
from .owner import extract_sender, normalize_qq
from .unified_memory import _terms


class AttentionRuntime:
    def __init__(self, bot):
        self.bot = bot
        self.store = AttentionStore(Path(bot.ctx.paths.data_dir)/"attention.sqlite3",
                                    **bot._ledger_storage_options())
        self._pending = {}
        self._current_mode = None
        self._text_key = None
        self._reaction = None
        self._activity_question = False
        self._active_activity = None
        self._generation = 0
        self._dialogue_lock = asyncio.Lock()
        self._work_lock = asyncio.Lock()
        self.last_status = "idle"

    def reset(self):
        self._generation += 1
        self._pending.clear()
        self._current_mode = None
        self._text_key = None
        self._reaction = None
        self._activity_question = False
        self._active_activity = None

    def close(self):
        self.reset()
        self.store.close()

    def owner(self):
        return normalize_qq(self.bot.config.owner.qq) if self.bot.config.owner.enabled else ""

    def enabled(self):
        b = self.bot
        return bool(b.config.attention.enabled and b._unified_active() and self.owner())

    def signature(self):
        b = self.bot
        return digest([self._generation, self.owner(), b.config.attention.model_dump(),
                       b.config.unified_mind.model_dump(), b.config.plugin.enabled,
                       b.config.reading.model_dump(), b.config.growth.model_dump(), b.config.projects.model_dump(),
                       b._research_signature()])

    def capture(self, message):
        if not self.enabled():
            return False
        changed = self.store.capture_activity(message, owner_id=self.owner(),
                                              engagement_seconds=self.bot.config.attention.engagement_seconds)
        if changed:
            # Raw text stays in RAM, only for the current trusted reply. The ledger
            # keeps hashes/enum state and cannot become another private transcript.
            sender = extract_sender(message)
            self._pending.clear()
            self._current_mode = None
            self._text_key = None
            self._reaction = None
            self._activity_question = False
            text = message.get("processed_plain_text")
            parts = message.get("raw_message")
            if (isinstance(text, str) and 0 < len(text) <= 600 and not text.lstrip().startswith("/")
                    and not message.get("is_command")
                    and not any(message.get(k) for k in ("is_picture", "is_voice", "is_emoji"))
                    and not (isinstance(parts, list) and any(not isinstance(p, dict) or p.get("type") != "text" for p in parts))):
                self._pending[(sender.stream_id, sender.message_id)] = (time.monotonic(), dict(message))
                self._text_key = (sender.stream_id, sender.message_id)
                # Keep only the enum from original text. The normalized recall
                # query may have lost quote boundaries or invalid controls.
                self._reaction = current_reaction(text)
                self._activity_question = activity_question(text)
            self.last_status = "conversation"
        return changed

    def trusted(self, kwargs):
        if not self.enabled() or not self.bot._unified:
            return None
        request = dict(kwargs)
        if "message" in request:
            sender = extract_sender(request["message"])
            if (not sender or sender.kind != "private" or sender.user_id != self.owner()
                    or not sender.stream_id or not sender.message_id):
                return None
            request.update(session_id=sender.stream_id, reply_message_id=sender.message_id)
        context = self.bot._unified.context(request)
        if (not context or context["speaker_id"] != self.owner()
                or context["scope"] != "private:"+self.owner()):
            return None
        return context, request

    async def prepare_dialogue(self, kwargs):
        trusted = self.trusted(kwargs)
        if not trusted or not self.bot.config.attention.dialogue_enabled or self._dialogue_lock.locked():
            return
        context, request = trusted
        key = (context["sid"], context["mid"])
        pending = self._pending.get(key)
        if not pending:
            return
        async with self._dialogue_lock:
            self._pending.pop(key, None)  # One attempt, including failure/budget denial.
            if time.monotonic()-pending[0] > 90:
                return
            message = pending[1]
            text = message.get("processed_plain_text", "")
            if self.activity_enabled() and activity_question(text):
                return
            if current_reaction(text):
                # This finite current-turn reaction does not consume a model
                # call or overwrite the saved communication preference.
                return
            if PRIVATE.search(text) or not should_classify(text):
                return
            owner, signature = self.owner(), self.signature()
            state = self.store.state(owner)
            revision = state["revision"]

            def valid():
                return (self.enabled() and self.signature() == signature
                        and self.bot._unified.context(request) == context
                        and self.store.state(owner)["revision"] == revision)

            def reserve():
                b = self.bot
                return b._self_state.reserve_thought(now=time.time(), day_start=day_start(b.config.general.timezone),
                    min_interval_seconds=0, daily_limit=b.config.unified_mind.max_daily_model_calls,
                    reserve_for_reply=getattr(b.config.unified_mind, "reserved_reply_calls", 0), purpose="foreground")

            mode = state["conversation"]["mode"] if state["conversation"]["active"] else "normal"
            result = await understand_dialogue(text, previous_mode=mode, generate=self.bot.ctx.llm.generate,
                reserve=reserve, valid=valid, task_name=self.bot.config.brain.model_task,
                timeout=min(8, self.bot.config.unified_mind.model_timeout_seconds))
            if result is None or not valid():
                return
            # An uncertain fallback must not erase an explicit listening preference.
            if result["confidence"] == 0:
                return
            selected = result["mode"]
            applied = self.store.set_mode(owner, message=message, mode=selected,
                until=time.time()+(300 if selected in {"repair", "light"} else 7200) if selected != "normal" else 0,
                expected_revision=revision)
            if applied:
                self._current_mode = (context["sid"], context["mid"], selected, self.signature())

    def current_reaction(self, kwargs):
        trusted = self.trusted(kwargs)
        if not trusted or not self.bot.config.attention.dialogue_enabled:
            return None
        context = trusted[0]
        if self._text_key != (context["sid"], context["mid"]):
            return None
        return self._reaction

    def effective_mode(self, kwargs):
        trusted = self.trusted(kwargs)
        if not trusted:
            return None
        if self.current_activity_question(kwargs):
            return "explicit_normal"
        if not self.bot.config.attention.dialogue_enabled:
            return None
        if self.current_reaction(kwargs):
            # Use the existing per-turn override contract with Habits; keep the
            # stored listening/advice preference intact for following messages.
            return "explicit_normal"
        state = self.store.state(self.owner())
        mode = state["conversation"]["mode"] if state["conversation"]["active"] else "normal"
        # Explicit current words outrank temporary inferred preferences.
        explicit = (literal_mode(trusted[0]["query"])
                    if self._text_key == (trusted[0]["sid"], trusted[0]["mid"]) else None)
        current = self._current_mode
        classified = (current and current[:2] == (trusted[0]["sid"], trusted[0]["mid"])
                      and current[3] == self.signature())
        if classified:
            mode = current[2]
        if explicit:
            mode = explicit
        continuity = self.bot._ensure_continuity()
        support = continuity.store.state(self.owner())["support"] if continuity else None
        if not explicit and not classified and support and support["active"]:
            # Both modules use fixed-duration short preferences. Compare their
            # effective start times, so an older explicit setting cannot revive
            # over a newer classified preference after a restart.
            conversation = state["conversation"]
            selected_at = conversation["until"]-(300 if mode in {"repair", "light"} else 7200)
            if not conversation["active"] or support["until"]-7200 > selected_at:
                mode = support["effective_mode"]
        # Explicit current normal also overrides an older habitual support style.
        return "explicit_normal" if mode == "normal" and (explicit or classified) else mode

    def hint(self, kwargs):
        mode = self.effective_mode(kwargs)
        hint = ("[MaiLivingWorld:conversation-attention]\n"+dialogue_hint("normal" if mode == "explicit_normal" else mode)) if mode else ""
        reaction = current_reaction_hint(self.current_reaction(kwargs))
        activity = self.activity_hint(kwargs)
        return "\n\n".join(part for part in (hint, reaction, activity) if part)

    def activity_enabled(self):
        return self.enabled() and bool(getattr(self.bot.config.attention, "activity_awareness", False))

    def current_activity_question(self, kwargs):
        trusted = self.trusted(kwargs) if self.activity_enabled() else None
        return bool(trusted and self._activity_question and self._text_key == (trusted[0]["sid"], trusted[0]["mid"]))

    def _activity_signature(self):
        # Configuration provenance must survive restarts; process generations and
        # current conversation receipts are intentionally not part of this hash.
        b = self.bot
        return digest([self.owner(), b.config.plugin.enabled, b.config.attention.model_dump(),
                       b.config.unified_mind.model_dump(), b.config.reading.model_dump(),
                       b.config.growth.model_dump(), b.config.projects.model_dump(),
                       b._mcp_signature("pixiv"), b._research_source_signature()])

    def _activity_record(self, focus):
        """A content-free fingerprint of the still-valid original work and progress."""
        b, owner = self.bot, self.owner()
        try:
            if focus["kind"] == "reading":
                if not (b.config.reading.enabled and b.config.reading.auto_continue
                        and b.config.mcp.enabled and b.config.mcp_pixiv.enabled):
                    return None
                companion = b._ensure_companion()
                row = companion.store.reading(focus["key"], owner_id=owner)
                if (not row or row["status"] not in {"active", "completed"}
                        or row["provider"] != "pixiv" or not row["source_refs"]
                        or companion._current_refs(row) != row["source_refs"]):
                    return None
                progress = row["cursor"]
                proof = digest([row["id"], owner, row["version"], row["status"], row["source_refs"], progress])
            elif focus["kind"] == "project":
                projects = b._ensure_projects()
                if not projects:
                    return None
                row = projects.store.get(owner, focus["key"])
                if (not row or row["status"] not in {"active", "completed", "unresolved"}
                        or not projects._current(row)):
                    return None
                progress = digest([row["stage"], row["evidence_refs"], row["result"]])
                proof = digest([row["id"], owner, row["status"], row["seed"], progress])
            elif focus["kind"] == "research":
                if not (b.config.growth.enabled and b.config.growth.mode == "active"):
                    return None
                row = next((r for r in b._growth_snapshot()["questions"] if r["key"] == focus["key"]), None)
                if not row or not b._research_available(row):
                    return None
                progress, proof = None, digest(row)
            else:
                return None
            return {"proof": proof, "progress": progress}
        except (ValueError, KeyError, TypeError):
            return None

    def activity_hint(self, kwargs):
        if not self.current_activity_question(kwargs):
            return ""
        now, owner = time.time(), self.owner()
        state = self.store.state(owner, now=now)
        events = self.store.events(owner, limit=200)
        starts = [event for event in events if event["action"] == "activity_start"
                  and 0 <= now-event["created"] <= ACTIVITY_TTL]
        if not starts:
            return activity_hint()
        latest = max(starts, key=lambda event: (event["created"], event.get("budget_day", 0),
                                               event.get("cycle_count", 0), event["id"]))
        focus, proof, signature = latest.get("focus"), latest.get("proof"), latest.get("signature")
        if signature != self._activity_signature():
            return activity_hint()
        record = self._activity_record(focus)
        finishes = [e for e in events if e["action"] == "finish" and isinstance(e.get("activity"), dict)
                    and e["activity"].get("token_hash") == latest.get("token_hash")
                    and e["created"] >= latest["created"]]
        if finishes:
            finish = max(finishes, key=lambda event: (event["created"], event["id"]))
            proof = finish["activity"].get("proof")
            outcome = finish.get("outcome")
        else:
            lease = state["lease"]
            activity = lease.get("activity")
            if not isinstance(activity, dict) or activity.get("token_hash") != latest.get("token_hash"):
                return activity_hint()
            captures = [event for event in events if event["action"] == "capture"
                        and event["created"] >= latest["created"] and event.get("revision", -1) > latest["revision"]]
            if captures:
                outcome = "interrupted"
            elif self._active_activity == latest.get("token_hash") and self.store.valid(owner, lease["token"], now=now):
                outcome = "working"
            else:
                outcome = "unfinished"
        if not record or record["proof"] != proof:
            return activity_hint()
        return activity_hint(focus["kind"], outcome, recovering=state["activity"]["recovering"])

    def candidates(self):
        b, owner = self.bot, self.owner()
        rows = []
        if not self.enabled():
            return rows
        projects = b._ensure_projects()
        occupied_readings = projects.active_reading_ids() if projects else set()
        occupied_questions = projects.active_question_keys() if projects else set()
        if projects:
            rows.extend(projects.candidates())
        if (b.config.reading.enabled and b.config.reading.auto_continue and b.config.mcp.enabled
                and b.config.mcp_pixiv.enabled):
            companion = b._ensure_companion()
            for row in companion.store.readings(owner_id=owner, status="active", resource_type="novel", limit=100):
                if (row["id"] not in occupied_readings and row["provider"] == "pixiv" and row["cursor"] < row["total_units"]
                        and companion._current_refs(row) == row["source_refs"]):
                    rows.append({"kind": "reading", "key": row["id"], "version": digest([
                        row["version"], row["status"], row["source_refs"]])})
        if (b._research_available() and b.config.growth.enabled and b.config.growth.mode == "active"):
            state = b._growth_snapshot()
            interests = set().union(*(_terms(x.get("topic", "")) for x in state.get("interests", [])))
            questions = [q for q in state.get("questions", []) if q["status"] == "open"
                         and q["key"] not in occupied_questions and b._research_available(q)]
            questions.sort(key=lambda q: (-len(set(_terms(q.get("topic", ""))) & interests), q["key"]))
            rows += [{"kind": "research", "key": q["key"], "version": digest(q)} for q in questions[:100]]
            if state.get("traits", {}).get("curiosity", .5) >= .55:
                rows.sort(key=lambda r: r["kind"] != "research")
        return rows[:128]

    async def tick(self):
        if not self.enabled() or self._work_lock.locked():
            return
        async with self._work_lock:
            b, owner = self.bot, self.owner()
            cfg, signature = b.config.attention, self.signature()
            awareness = self.activity_enabled()
            claim = self.store.claim(owner, candidates=self.candidates(),
                day_start=day_start(b.config.general.timezone), daily_limit=cfg.max_daily_actions,
                interval_seconds=cfg.interval_seconds,
                lease_seconds=min(900, max(180, b.config.unified_mind.model_timeout_seconds*2+120)),
                activity_awareness=awareness)
            if not claim:
                self.last_status = "waiting"
                return
            focus, token = claim["focus"], claim["token"]

            def valid():
                return (self.enabled() and signature == self.signature()
                        and self.store.valid(owner, token) and focus in self.candidates())

            outcome = "blocked"
            before = self._activity_record(focus) if awareness else None
            def on_dispatch():
                if not awareness:
                    return True
                if not valid():
                    return False
                if self._active_activity is not None:
                    return True
                current = self._activity_record(focus)
                if (not before or current != before or not self.store.start_activity(owner, token,
                        proof=before["proof"], signature=self._activity_signature())):
                    return False
                self._active_activity = self.store.state(owner)["lease"]["activity"]["token_hash"]
                return True
            dispatch = {"on_dispatch": on_dispatch} if awareness else {}
            self.last_status = focus["kind"]
            try:
                if not valid():
                    outcome = "interrupted"
                if outcome == "interrupted":
                    return
                if focus["kind"] == "reading":
                    result = await b._ensure_companion().auto_read(reading_id=focus["key"], external_valid=valid, **dispatch)
                    if isinstance(result, dict) and result.get("success"):
                        outcome = "progress"
                elif focus["kind"] == "project":
                    projects = b._ensure_projects()
                    result = await projects.tick(project_id=focus["key"], external_valid=valid, **dispatch) if projects else None
                    if isinstance(result, dict) and result.get("success"):
                        outcome = "progress"
                else:
                    result = await b._research_tools(question_key=focus["key"], external_valid=valid, **dispatch)
                    if result is True:
                        outcome = "progress"
                # A completed reading or resolved question may leave candidates;
                # owner/config/lease still govern whether the attempt was valid.
                if signature != self.signature() or not self.store.valid(owner, token):
                    outcome = "interrupted"
                elif awareness and outcome == "progress" and focus["kind"] in {"reading", "project"}:
                    after = self._activity_record(focus)
                    if not after or not before or after["progress"] == before["progress"]:
                        outcome = "blocked"
            except asyncio.CancelledError:
                outcome = "interrupted"
                raise
            except Exception:
                outcome = "failed"
            finally:
                after = self._activity_record(focus) if awareness and signature == self.signature() else None
                self.store.finish(owner, token, outcome=outcome, activity_awareness=awareness and self.activity_enabled(),
                                  activity_proof=after["proof"] if after else None)
                self._active_activity = None
                self.last_status = outcome

    def proactive_allowed(self):
        return not self.enabled() or self.store.state(self.owner())["engaged_until"] <= time.time()

    def snapshot(self):
        if not self.enabled():
            return {"enabled": False}
        state = self.store.state(self.owner())
        focus = state.get("focus")
        available = focus is not None and focus in self.candidates()
        working = available and self.store.valid(self.owner(), state["lease"]["token"])
        if self.activity_enabled():
            activity = state["lease"].get("activity")
            working = (working and isinstance(activity, dict) and self._active_activity is not None
                       and activity.get("token_hash") == self._active_activity)
        result = {"enabled": True, "owner_id": self.owner(), "revision": state["revision"],
                "engaged": state["engaged_until"] > time.time(), "engaged_until": state["engaged_until"],
                "focus_kind": focus["kind"] if available else "", "focus_available": available,
                "working": bool(working),
                "conversation": state["conversation"], "last_outcome": state["last_outcome"],
                "last_finished": state["last_finished"], "cycle_count": state["cycle_count"]
                    if state["budget_day"] == day_start(self.bot.config.general.timezone) else 0}
        if self.activity_enabled():
            result["recovering"] = state["activity"]["recovering"]
        return result

    def inspect(self, kwargs):
        if not self.trusted(kwargs):
            return {"success": False, "code": "owner_private_attention_required"}
        data = self.snapshot()
        data.pop("owner_id", None)
        return {"success": True, "data": data,
                "instructions": "只读当前关注。阅读/研究只说明真实活动类型和状态；不代表阅读完成、发出关心或已作承诺。具体内容使用现有阅读/记忆工具并遵守表达权限。"}
