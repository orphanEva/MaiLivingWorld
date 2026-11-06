"""Autonomous finite projects within existing read permissions and model budgets."""
from __future__ import annotations

import asyncio
from pathlib import Path
import re
import time

from .growth_store import source_fingerprint
from .mcp_runtime import day_start
from .owner import extract_sender, normalize_qq
from .projects_model import evidence_records, understand_project
from .projects_store import ProjectsStore, digest, references
from .tool_store import _digest as tool_digest
from .unified_memory import _terms


def _ref(row):
    return {"id": row["id"], "digest": source_fingerprint(row), "scope": row["scope"], "sensitivity": row["sensitivity"]}


class ProjectsRuntime:
    def __init__(self, bot):
        self.bot = bot
        self.store = ProjectsStore(Path(bot.ctx.paths.data_dir)/"projects.sqlite3", **bot._ledger_storage_options())
        self._generation = 0
        self._activity_token = 0
        self._engaged_until = 0.0
        self._activity_receipts = {}
        self._lock = asyncio.Lock()
        self.last_status = "idle"

    def reset(self):
        self._generation += 1

    def close(self):
        self.reset()
        self.store.close()

    def owner(self):
        return normalize_qq(self.bot.config.owner.qq) if self.bot.config.owner.enabled else ""

    def enabled(self):
        b, owner = self.bot, self.owner()
        return bool(b.config.plugin.enabled and b.config.projects.enabled and b._unified_active() and owner
                    and b.config.growth.enabled and b.config.growth.mode == "active"
                    and b._self_state and b._self_state.state()["owner_id"] == owner)

    def signature(self):
        b = self.bot
        return digest([self._generation, self._activity_token, self.owner(), b.config.plugin.enabled, b.config.projects.model_dump(),
                       b.config.growth.model_dump(), b.config.unified_mind.model_dump(), b.config.reading.model_dump(),
                       b._mcp_generation, b._research_signature()])

    def _activity(self):
        b, owner = self.bot, self.owner()
        attention = getattr(b, "_attention", None)
        active = bool(attention and attention.enabled())
        state = attention.store.state(owner) if active else {}
        unified = getattr(b, "_unified", None)
        contexts = getattr(unified, "_contexts", {})
        frontier = sorted((sid, mid) for sid, mid in getattr(unified, "_private", {}).items()
                          if contexts.get((sid, mid), {}).get("scope") == "private:"+owner)
        return (bool(self._engaged_until > time.monotonic() or state.get("engaged_until", 0) > time.time()),
                digest([state.get("latest_receipt", ""), state.get("engaged_until", 0),
                        self._activity_token, getattr(unified, "_generation", 0), frontier]))

    def engaged(self):
        """Read-only current-conversation priority, including media-only input."""
        return self._activity()[0]

    def trusted(self, kwargs):
        if not self.enabled() or not isinstance(kwargs, dict):
            return None
        request = dict(kwargs)
        if "message" in request:
            sender = extract_sender(request["message"])
            if (not sender or sender.kind != "private" or sender.user_id != self.owner()
                    or not sender.stream_id or not sender.message_id):
                return None
            request.update(session_id=sender.stream_id, reply_message_id=sender.message_id)
        b = self.bot
        context = b._unified.context(request) if b._unified else None
        latest = b._unified.context({"session_id": context["sid"]}) if context else None
        return context if (context and latest and latest["mid"] == context["mid"]
                           and context["scope"] == "private:"+self.owner() and context["speaker_id"] == self.owner()) else None

    def seed_candidates(self):
        if not self.enabled() or not self.bot._ensure_growth():
            return []
        cfg = self.bot.config
        reading_available = (cfg.mcp.enabled and cfg.mcp_pixiv.enabled and cfg.mcp.remember_results
                             and cfg.reading.enabled and cfg.reading.auto_continue)
        if not (self.bot._research_available() or reading_available):
            return []
        state = self.bot._growth_snapshot()
        interests = [row for row in state["interests"] if row.get("weight", 0) > 0 and row.get("status", "active") == "active"]
        seeds = []
        for question in state["questions"]:
            if (question.get("status") != "open"
                    or not (self.bot._research_available(question) or reading_available)):
                continue
            query = question["topic"]+" "+question["text"]
            matches = [(len(set(_terms(query)) & set(_terms(interest["topic"]))), interest) for interest in interests]
            matches = [item for item in matches if item[0] > 0]
            if not matches:
                continue
            score, interest = max(matches, key=lambda pair: (pair[0], pair[1]["weight"], pair[1]["key"]))
            try:
                refs = references(interest["sources"]+question["sources"])
                if len(refs) > 16:
                    continue
                seed = {"question_key": question["key"], "interest_key": interest["key"], "topic": question["topic"],
                        "question": question["text"], "seed_refs": refs,
                        "source_signature": self.bot._research_source_signature()}
                from .projects_store import seed_value
                seed_value(seed)
            except (ValueError, TypeError, KeyError):
                continue
            seeds.append((score, interest["weight"], seed))
        return [row[2] for row in sorted(seeds, key=lambda r: (-r[0], -r[1], r[2]["question_key"]))[:32]]

    def _resolve(self, refs):
        try:
            rows = self.bot._resolve_roots([ref["id"] for ref in refs])
            return rows if all(ref["id"] in rows and _ref(rows[ref["id"]]) == ref for ref in refs) else None
        except (ValueError, KeyError, TypeError):
            return None

    def _current(self, row, *, active=False):
        signature = self.bot._research_source_signature()
        if (not self.enabled() or row["owner_id"] != self.owner()
                or row["seed"]["source_signature"] not in {signature, self.bot._mcp_source_signature("pixiv")}):
            return False
        try:
            refs = references(row["seed"]["seed_refs"]+row["evidence_refs"])
        except ValueError:
            return False
        if self._resolve(refs) is None:
            return False
        # Older projects retain their Pixiv source hash in storage. Validate it
        # above, then compare the same seed under the current reader signature.
        seed = dict(row["seed"], source_signature=signature)
        return not active or seed in self.seed_candidates()

    def prepare(self):
        if not self.enabled() or self._activity()[0]:
            return None
        rows = self.store.items(self.owner())
        for row in rows:
            if row["status"] == "active" and not self._current(row, active=True):
                self.store.pause_invalid(self.owner(), row["id"], row["revision"])
        known = {row["seed"]["question_key"] for row in rows}
        for seed in self.seed_candidates():
            if seed["question_key"] not in known:
                return self.store.create(self.owner(), seed, max_active=self.bot.config.projects.max_active_projects)
        return None

    def candidates(self):
        if not self.enabled():
            return []
        # Lease/revision changes must not invalidate Attention's own running focus.
        return [{"kind": "project", "key": row["id"], "version": digest([row["stage"], row["seed"], row["evidence_refs"]])}
                for row in self.store.items(self.owner()) if row["status"] == "active" and self._current(row, active=True)]

    def active_question_keys(self):
        valid = {row["key"] for row in self.candidates()}
        return {row["seed"]["question_key"] for row in self.store.items(self.owner()) if row["id"] in valid} if self.enabled() else set()

    def _reading_for(self, seed):
        b = self.bot
        if not (b.config.reading.enabled and b.config.reading.auto_continue and b.config.mcp.enabled and b.config.mcp_pixiv.enabled):
            return None
        companion = b._ensure_companion()
        terms = set(_terms(seed["topic"]+" "+seed["question"]))
        rows = companion.store.readings(owner_id=self.owner(), status="active", resource_type="novel", limit=100)
        return next((row for row in rows if row["provider"] == "pixiv" and row["cursor"] < row["total_units"]
                     and terms & set(_terms(row["title"])) and companion._current_refs(row) == row["source_refs"]), None)

    def active_reading_ids(self):
        valid = {row["key"] for row in self.candidates()}
        return {reading["id"] for row in self.store.items(self.owner()) if row["id"] in valid and row["stage"] == "gather"
                for reading in [self._reading_for(row["seed"])] if reading} if self.enabled() else set()

    async def _gather(self, row, valid, on_dispatch=None):
        b = self.bot
        reading = self._reading_for(row["seed"])
        if reading:
            result = await b._ensure_companion().auto_read(reading_id=reading["id"], external_valid=valid,
                **({"on_dispatch": on_dispatch} if on_dispatch is not None else {}))
            if not valid():
                return None
            if not isinstance(result, dict) or result.get("success") is not True:
                return {"success": False, "records": []} if result is not None else None
            data = result.get("data", {})
            excerpt = data.get("excerpt", "")
            after = data.get("reading", {})
            if (not isinstance(excerpt, str) or after.get("id") != reading["id"]
                    or after.get("cursor", 0) <= reading["cursor"]):
                return {"success": True, "records": []}
            ids = ["tool:observation:"+tool_digest("pixiv", f"reading:novel:{reading['resource_id']}:{reading['version']}:{reading['cursor']}:{offset}")
                   for offset in range(0, min(len(excerpt), 4000), 500)]
            resolved = b._resolve_roots(ids)
            return {"success": True, "records": [resolved[ident] for ident in ids if ident in resolved]}
        return await b._research_tools(question_key=row["seed"]["question_key"], external_valid=valid, with_evidence=True,
            **({"on_dispatch": on_dispatch} if on_dispatch is not None else {}))

    async def tick(self, project_id=None, external_valid=None, on_dispatch=None):
        if not self.enabled() or self._lock.locked():
            return None
        async with self._lock:
            b, owner = self.bot, self.owner()
            engaged, activity = self._activity()
            if engaged or external_valid is not None and not external_valid():
                self.last_status = "conversation"
                return None
            if project_id is None:
                self.prepare()
            eligible = [row["key"] for row in self.candidates() if project_id is None or row["key"] == project_id]
            row = self.store.claim(owner, eligible, day_start=day_start(b.config.general.timezone),
                daily_limit=b.config.projects.max_daily_actions, interval_seconds=b.config.projects.interval_seconds,
                lease_seconds=min(3600, max(360, b.config.unified_mind.model_timeout_seconds*2+120)))
            if not row:
                self.last_status = "waiting"
                return None
            signature = self.signature()
            def valid():
                return (self.enabled() and self.signature() == signature and self._current(row, active=True)
                        and self._activity() == (False, activity)
                        and self.store.valid(owner, row["id"], row["lease_token"], row["revision"])
                        and (external_valid is None or external_valid()))
            action, evidence, result = "interrupted", None, None
            exhausted = False
            def reserve():
                nonlocal exhausted
                allowed = b._self_state.reserve_thought(now=time.time(), day_start=day_start(b.config.general.timezone),
                    min_interval_seconds=0, daily_limit=b.config.unified_mind.max_daily_model_calls,
                    reserve_for_reply=b.config.unified_mind.reserved_reply_calls, purpose="background")
                exhausted = not allowed
                return allowed
            try:
                if not valid():
                    return None
                if row["stage"] == "gather":
                    fetched = await self._gather(row, valid, on_dispatch=on_dispatch)
                    if not valid():
                        return None
                    if fetched is None:
                        action = "deferred"
                    elif not isinstance(fetched, dict) or fetched.get("success") is not True:
                        action = "failed"
                    else:
                        originals = fetched.get("records", [])
                        seeds = {ref["id"] for ref in row["seed"]["seed_refs"]}
                        originals = [r for r in originals if isinstance(r, dict) and r.get("id") not in seeds][:8]
                        if originals and evidence_records(originals):
                            evidence = references([_ref(r) for r in originals])
                            action = "gathered" if self._resolve(evidence) is not None else "interrupted"
                        else:
                            action = "no_evidence"
                else:
                    resolved = self._resolve(row["evidence_refs"])
                    originals = [resolved[ref["id"]] for ref in row["evidence_refs"]] if resolved else []
                    result = await understand_project(row["seed"]["question"], originals, generate=b.ctx.llm.generate,
                        reserve=reserve, valid=valid, task_name=b.config.brain.model_task,
                        timeout=b.config.unified_mind.model_timeout_seconds, on_dispatch=on_dispatch)
                    if not valid():
                        return None
                    action = "reviewed" if result else "deferred" if exhausted else "failed"
                if valid():
                    changed = self.store.finish(owner, row["id"], row["lease_token"], row["revision"], action=action,
                                                evidence_refs=evidence, result=result)
                    self.last_status = action
                    return {"success": bool(changed and action in {"gathered", "reviewed"}), "stage": action}
            except asyncio.CancelledError:
                self.last_status = "interrupted"
                raise
            except Exception:
                self.last_status = "project_attempt_failed"
                action = "failed"
            finally:
                # Release only our still-live lease; never overwrite a new worker or owner command.
                self.store.finish(owner, row["id"], row["lease_token"], row["revision"], action=action if action in {"failed", "deferred"} else "interrupted")
            return {"success": False, "stage": action}

    def capture(self, message):
        if not self.enabled() or not isinstance(message, dict):
            return False
        sender = extract_sender(message)
        if (not sender or sender.kind != "private" or sender.user_id != self.owner() or not sender.stream_id or not sender.message_id
                or normalize_qq(message.get("account_id")) == self.owner()):
            return False
        # Even media and commands occupy the current conversation. Keep only an
        # ephemeral receipt hash, never their content or a learned preference.
        receipt = digest([self.owner(), sender.stream_id, sender.message_id])
        if receipt not in self._activity_receipts:
            self._activity_receipts[receipt] = True
            if len(self._activity_receipts) > 256:
                self._activity_receipts.pop(next(iter(self._activity_receipts)))
            self._activity_token += 1
            self._engaged_until = time.monotonic()+self.bot.config.attention.engagement_seconds
        parts = message.get("raw_message")
        if ((isinstance(parts, list) and any(not isinstance(part, dict) or part.get("type") != "text" for part in parts))
                or any(message.get(key) for key in ("is_picture", "is_voice", "is_emoji"))):
            return False
        text = message.get("processed_plain_text")
        match = re.fullmatch(r"/项目 (暂停|继续|停止) (projects:item:[0-9a-f]{64}) ([1-9][0-9]{0,8})", text.strip()) if isinstance(text, str) else None
        if not match:
            return False
        row = self.store.get(self.owner(), match[2])
        if not row or match[1] == "继续" and not self._current(row, active=True):
            return False
        changed = self.store.command(self.owner(), row["id"], int(match[3]), {"暂停": "pause", "继续": "resume", "停止": "stop"}[match[1]],
            digest([self.owner(), sender.stream_id, sender.message_id]), digest(text), max_active=self.bot.config.projects.max_active_projects)
        if changed:
            self.reset()
        return changed

    def records(self, query=""):
        if not self.enabled():
            return []
        terms, result = _terms(query), []
        rank = {"public": 0, "personal": 1, "unknown": 2, "confidential": 3}
        for row in self.store.items(self.owner()):
            if row["status"] not in {"completed", "unresolved"} or not row["result"] or not self._current(row):
                continue
            refs = references(row["seed"]["seed_refs"]+row["evidence_refs"])
            originals = self._resolve(refs)
            if originals is None:
                continue
            sensitivity = max((r["sensitivity"] for r in refs), key=rank.get)
            scopes = {r["scope"] for r in refs}
            subjects = {r.get("subject_id", "") for r in originals.values()}
            text = "查阅小结（限本次证据，暂时推断）："+row["result"]["conclusion"]
            result.append({"id": "projects:result:"+row["id"].split(":")[-1], "source": "projects", "kind": "project", "derived": True,
                "scope": "public:derived" if sensitivity == "public" else next(iter(scopes)) if len(scopes) == 1 else "global:derived",
                "subject_id": next(iter(subjects)) if len(subjects) == 1 else "", "text": text, "timestamp": row["updated"],
                "sensitivity": sensitivity, "evidence_status": "inferred", "sources": refs, "revision": row["revision"],
                "topic": row["seed"]["topic"], "score": sum(term in (row["seed"]["topic"]+text).casefold() for term in terms)})
        return sorted(result, key=lambda row: (-row["score"], -row["timestamp"], row["id"]))[:8]

    def inspect(self, kwargs):
        context = self.trusted(kwargs)
        if not context:
            return {"success": False, "code": "owner_private_required"}
        items = []
        for row in self.store.items(self.owner()):
            current = self._current(row)
            refs = row["seed"]["seed_refs"]+row["evidence_refs"]
            # Tool metadata never becomes a bypass around audience/reading expression review.
            allowed = current and all(r["sensitivity"] == "public" or r["scope"] == "private:"+self.owner() and r["sensitivity"] == "personal" for r in refs)
            visible = bool(self.bot._expression_records([{"id": row["id"], "sources": refs}], context=context))
            allowed = allowed and visible
            item = {key: row[key] for key in ("id", "status", "stage", "revision", "reason", "attempts")}
            item.update(current=current, topic=row["seed"]["topic"] if allowed else "有来源或阅读边界限制的查阅项目")
            # Conclusions are retrieved through the unified expression layer, not raw tool output.
            items.append(item)
        return {"success": True, "data": items, "instructions": "这里只读项目状态，不代表完成原问题、读完整本书或已经分享。"
                "成果须经过统一记忆表达判断；主人私聊完整指令 /项目 暂停|继续|停止 <id> <revision>，不修改外部任务或成长事实。"}
