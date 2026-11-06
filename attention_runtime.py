"""One bounded foreground/background focus; references are not new memories."""
from __future__ import annotations

import asyncio
from pathlib import Path
import time

from .attention_store import AttentionStore
from .activity_context import ACTIVITY_TTL, activity_question, activity_hint
from .cognitive_workspace import assess, candidate_key, capability_view, working_hint
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
        self._assessment = {}
        self._workspace_message = None
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
        self._assessment = {}
        self._workspace_message = None

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
        if not self.enabled() or not isinstance(message, dict):
            return False
        parts = message.get("raw_message")
        media = (any(message.get(k) for k in ("is_picture", "is_voice", "is_emoji"))
                 or isinstance(parts, list) and any(isinstance(p, dict) and p.get("type")
                    in {"image", "video", "voice", "record"} for p in parts))
        intent = "media" if media else "conversation"
        changed = self.store.capture_activity(message, owner_id=self.owner(),
            engagement_seconds=self.bot.config.attention.engagement_seconds,
            workspace_intent=intent if self.workspace_enabled() else None)
        if changed:
            # Raw text stays in RAM, only for the current trusted reply. The ledger
            # keeps hashes/enum state and cannot become another private transcript.
            sender = extract_sender(message)
            self._workspace_message = (sender.stream_id, sender.message_id, intent)
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

    async def prepare_dialogue(self, kwargs, *, reserved=False, keep_reply_slot=False):
        """Return a real reservation; reply chains may preserve one expression call."""
        if type(reserved) is not bool or type(keep_reply_slot) is not bool:
            raise ValueError("invalid_turn_reservation")
        trusted = self.trusted(kwargs)
        if not trusted or not self.bot.config.attention.dialogue_enabled or self._dialogue_lock.locked():
            return False
        context, request = trusted
        key = (context["sid"], context["mid"])
        pending = self._pending.get(key)
        if not pending:
            return False
        async with self._dialogue_lock:
            self._pending.pop(key, None)  # One attempt, including failure/budget denial.
            if time.monotonic()-pending[0] > 90:
                return False
            message = pending[1]
            text = message.get("processed_plain_text", "")
            if self.activity_enabled() and activity_question(text):
                return False
            if current_reaction(text):
                # This finite current-turn reaction does not consume a model
                # call or overwrite the saved communication preference.
                return False
            if PRIVATE.search(text) or not should_classify(text):
                return False
            owner, signature = self.owner(), self.signature()
            state = self.store.state(owner)
            revision = state["revision"]
            charged = False

            def valid():
                return (self.enabled() and self.signature() == signature
                        and self.bot._unified.context(request) == context
                        and self.store.state(owner)["revision"] == revision)

            def reserve():
                nonlocal charged
                b = self.bot
                charged = b._self_state.reserve_thought(now=time.time(), day_start=day_start(b.config.general.timezone),
                    min_interval_seconds=0 if reserved else b.config.unified_mind.min_interval_seconds,
                    daily_limit=max(0, b.config.unified_mind.max_daily_model_calls-(1 if keep_reply_slot else 0)),
                    reserve_for_reply=getattr(b.config.unified_mind, "reserved_reply_calls", 0), purpose="foreground")
                return charged

            mode = state["conversation"]["mode"] if state["conversation"]["active"] else "normal"
            result = await understand_dialogue(text, previous_mode=mode, generate=self.bot.ctx.llm.generate,
                reserve=reserve, valid=valid, task_name=self.bot.config.brain.model_task,
                timeout=min(8, self.bot.config.unified_mind.model_timeout_seconds))
            if result is None or not valid():
                return charged
            # An uncertain fallback must not erase an explicit listening preference.
            if result["confidence"] == 0:
                return charged
            selected = result["mode"]
            applied = self.store.set_mode(owner, message=message, mode=selected,
                until=time.time()+(300 if selected in {"repair", "light"} else 7200) if selected != "normal" else 0,
                expected_revision=revision)
            if applied:
                self._current_mode = (context["sid"], context["mid"], selected, self.signature())
            return charged

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
        workspace = self.workspace_hint(kwargs)
        return "\n\n".join(part for part in (hint, reaction, activity, workspace) if part)

    def workspace_enabled(self):
        return self.enabled() and bool(getattr(self.bot.config.attention, "integrated_workspace", False))

    def adaptive_enabled(self):
        return self.enabled() and bool(getattr(self.bot.config.attention, "adaptive_motivation", False))

    def _validated_capabilities(self, state):
        """Keep outcomes usable only while the originating work is still valid."""
        signature, current = self._activity_signature(), {}
        result = {}
        for kind, rows in state.get("capabilities", {}).items():
            result[kind] = []
            for row in rows:
                focus = row.get("focus")
                if not isinstance(focus, dict) or row.get("signature") != signature:
                    continue
                key = candidate_key(focus)
                if key not in current:
                    current[key] = self._activity_record(focus)
                if current[key] is not None and (kind != "research" or current[key]["proof"] == row.get("start_proof")):
                    result[kind].append(row)
        return result

    def _pending_commitments(self):
        runtime = self.bot._ensure_followthrough()
        return ([row for row in runtime.store.items(self.owner())
                 if row["status"] in {"active", "ready"} and runtime._current(row)]
                if runtime and runtime.enabled() else [])

    def workspace(self, kwargs):
        """Reconcile the durable workspace against original task ledgers.

        The current query lives only in this response. Task content is represented
        by opaque references and must still pass normal memory expression review.
        """
        trusted = self.trusted(kwargs) if self.workspace_enabled() else None
        if not trusted:
            return None
        b, owner, context = self.bot, self.owner(), trusted[0]
        state = self.store.state(owner)
        candidates = self.candidates()
        saved = state["workspace"]
        previous_focus = saved.get("focus") or state.get("focus")
        focus = next((row for row in candidates if previous_focus
                      and candidate_key(row) == candidate_key(previous_focus)), None)
        pending_projects = []
        projects = b._ensure_projects()
        if projects:
            for row in projects.store.items(owner):
                if row["status"] not in {"active", "paused", "unresolved"} or not projects._current(row):
                    continue
                candidate = {"kind": "project", "key": row["id"],
                             "version": digest([row["stage"], row["seed"], row["evidence_refs"]])}
                plan = row.get("plan", {})
                pending_projects.append({"ref": candidate, "stage": row["stage"], "status": row["status"],
                    "rounds": len(plan.get("rounds", [])),
                    "pending_question": bool(plan.get("pending_question")),
                    "evidence_needed": bool(plan.get("evidence_needed"))})
        commitments = [{"id": row["id"], "status": row["status"]} for row in self._pending_commitments()]
        plans = []
        if b.config.shared_plans.enabled:
            companion = b._ensure_companion()
            plans = [{"id": row["id"], "revision": row["revision"]}
                     for row in companion.store.plans(owner_id=owner, status="active", limit=8)] if companion else []
        capabilities = capability_view(self._validated_capabilities(state)) if self.adaptive_enabled() else {}
        review = [focus["kind"]] if focus and capabilities.get(focus["kind"], {}).get("needs_review") else []
        pause = ("owner_message" if state["engaged_until"] > time.time() else
                 "recovering" if self.activity_enabled() and state["activity"]["recovering"] else
                 "blocked" if state["last_outcome"] in {"blocked", "failed"} else "")
        mode = self.effective_mode(kwargs)
        intent = mode if mode in {"listen", "advice", "repair", "light"} else "conversation"
        if self._workspace_message and self._workspace_message[:2] == (context["sid"], context["mid"]):
            if self._workspace_message[2] == "media":
                intent = "media"
        return {"current_goal": {"query": context["query"], "intent": intent,
                                 "session_id": context["sid"], "message_id": context["mid"]},
                "receipt": state["latest_receipt"], "focus": focus, "pending": candidates[:8],
                "pause_reason": pause, "reasons": saved["reasons"] if focus == saved.get("focus") else [],
                "projects": pending_projects[:8], "commitments": commitments[:8], "plans": plans,
                "pending_commitments": len(commitments),
                "open_questions": sum(row["kind"] == "research" for row in candidates)
                                  + sum(row["pending_question"] for row in pending_projects),
                "capabilities": capabilities, "review_kinds": review}

    def workspace_hint(self, kwargs):
        return working_hint(self.workspace(kwargs))

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
                progress = digest([row["stage"], row["evidence_refs"], row["result"], row.get("plan")])
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
        rows, details = [], {}
        self._assessment = {}
        if not self.enabled():
            return rows
        projects = b._ensure_projects()
        occupied_readings = projects.active_reading_ids() if projects else set()
        occupied_questions = projects.active_question_keys() if projects else set()
        if projects:
            rows.extend(projects.candidates())
            if self.adaptive_enabled():
                for candidate in rows:
                    row = projects.store.get(owner, candidate["key"])
                    if row:
                        details[candidate_key(candidate)] = {"topic": row["seed"]["topic"],
                            "stage": row["stage"], "in_progress": row["stage"] != "gather"}
        if (b.config.reading.enabled and b.config.reading.auto_continue and b.config.mcp.enabled
                and b.config.mcp_pixiv.enabled):
            companion = b._ensure_companion()
            for row in companion.store.readings(owner_id=owner, status="active", resource_type="novel", limit=100):
                if (row["id"] not in occupied_readings and row["provider"] == "pixiv" and row["cursor"] < row["total_units"]
                        and companion._current_refs(row) == row["source_refs"]):
                    rows.append({"kind": "reading", "key": row["id"], "version": digest([
                        row["version"], row["status"], row["source_refs"]])})
                    details[candidate_key(rows[-1])] = {"topic": row["title"], "in_progress": row["cursor"] > 0}
        state = {}
        if (b._research_available() and b.config.growth.enabled and b.config.growth.mode == "active"):
            state = b._growth_snapshot()
            interests = [(set(_terms(item.get("topic", ""))), item.get("weight", 0))
                         for item in state.get("interests", []) if item.get("status") in (None, "active")]
            questions = [q for q in state.get("questions", []) if q["status"] == "open"
                         and q["key"] not in occupied_questions and b._research_available(q)]
            def research_priority(question):
                topic = set(_terms(question.get("topic", "")))
                preference = sum(weight*len(topic & terms) for terms, weight in interests)
                return -round(preference, 8), question["key"]
            questions.sort(key=research_priority)
            rows += [{"kind": "research", "key": q["key"], "version": digest(q)} for q in questions[:100]]
            details.update({"research:"+q["key"]: {"topic": q["topic"]} for q in questions[:100]})
            if state.get("traits", {}).get("curiosity", .5) >= .55:
                rows.sort(key=lambda r: r["kind"] != "research")
        rows = rows[:128]
        if self.adaptive_enabled():
            if not state and b.config.growth.enabled and b._ensure_growth():
                state = b._growth_snapshot()
            attention = self.store.state(owner)
            attention["capabilities"] = self._validated_capabilities(attention)
            attention["pending_commitments"] = len(self._pending_commitments())
            self._assessment = assess(rows, details, state, attention)
            rows.sort(key=lambda row: -self._assessment[candidate_key(row)]["score"])
        return rows

    async def tick(self):
        if not self.enabled() or self._work_lock.locked():
            return
        async with self._work_lock:
            b, owner = self.bot, self.owner()
            cfg, signature = b.config.attention, self.signature()
            awareness = self.activity_enabled()
            adaptive, workspace = self.adaptive_enabled(), self.workspace_enabled()
            tracking = awareness or adaptive or workspace
            candidates = self.candidates()
            claim = self.store.claim(owner, candidates=candidates,
                day_start=day_start(b.config.general.timezone), daily_limit=cfg.max_daily_actions,
                interval_seconds=cfg.interval_seconds,
                lease_seconds=min(900, max(180, b.config.unified_mind.model_timeout_seconds*2+120)),
                activity_awareness=awareness, integrated_workspace=workspace,
                assessment=self._assessment if adaptive else None)
            if not claim:
                self.last_status = "waiting"
                return
            focus, token = claim["focus"], claim["token"]

            def valid():
                return (self.enabled() and signature == self.signature()
                        and self.store.valid(owner, token) and focus in self.candidates())

            outcome = "blocked"
            before = self._activity_record(focus) if tracking else None
            def on_dispatch():
                if not tracking:
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
            dispatch = {"on_dispatch": on_dispatch} if tracking else {}
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
                    result = await b._research_tools(question_key=focus["key"], external_valid=valid,
                        **({"with_evidence": True} if adaptive else {}), **dispatch)
                    if (adaptive and isinstance(result, dict) and result.get("success") is True and result.get("records")
                            or not adaptive and result is True):
                        outcome = "progress"
                # A completed reading or resolved question may leave candidates;
                # owner/config/lease still govern whether the attempt was valid.
                if signature != self.signature() or not self.store.valid(owner, token):
                    outcome = "interrupted"
                elif tracking and outcome == "progress" and focus["kind"] in {"reading", "project"}:
                    after = self._activity_record(focus)
                    if not after or not before or after["progress"] == before["progress"]:
                        outcome = "blocked"
            except asyncio.CancelledError:
                outcome = "interrupted"
                raise
            except Exception:
                outcome = "failed" if signature == self.signature() and self.store.valid(owner, token) else "interrupted"
            finally:
                after = self._activity_record(focus) if tracking and signature == self.signature() else None
                self.store.finish(owner, token, outcome=outcome, activity_awareness=awareness and self.activity_enabled(),
                    activity_proof=after["proof"] if after else None, learn_capabilities=adaptive and self.adaptive_enabled())
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
        if self.activity_enabled() or self.adaptive_enabled() or self.workspace_enabled():
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
        if self.adaptive_enabled():
            result["capabilities"] = capability_view(self._validated_capabilities(state))
        return result

    def inspect(self, kwargs):
        if not self.trusted(kwargs):
            return {"success": False, "code": "owner_private_attention_required"}
        data = self.snapshot()
        data.pop("owner_id", None)
        if self.workspace_enabled():
            data["workspace"] = self.workspace(kwargs)
        return {"success": True, "data": data,
                "instructions": "只读当前关注。阅读/研究只说明真实活动类型和状态；不代表阅读完成、发出关心或已作承诺。具体内容使用现有阅读/记忆工具并遵守表达权限。"}
