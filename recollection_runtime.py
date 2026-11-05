"""Durable corrections and question receipts for one continuing relationship."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
import time

from .growth_store import source_fingerprint
from .mcp_runtime import day_start, digest
from .mind import PRIVATE
from .owner import extract_sender, normalize_qq
from .recollection_model import should_reconsider, understand_recollection
from .recollection_store import RecollectionStore
from .unified_memory import _terms

_SENSITIVITY = {"public": 0, "personal": 1, "unknown": 2, "confidential": 3}
_QUESTION = re.compile(r"(?:[?？]|怎么样|还好吗|如何|吗|呢)[。！!\s]*$")
_MENTIONED_QUESTION = re.compile(r"[\"'“”‘’「」『』]|(?:不要|不再|不用|别).{0,12}(?:问|追问)|你说|你问|问过|比如|例如|假如|假设")
_GENERIC = {"现在", "最近", "今天", "明天", "怎么", "么样", "如何", "事情", "还好", "好吗", "有没有", "有没", "没有",
            "主人", "原话", "收到", "自述", "更正", "记录", "你说", "说过", "之前", "上次", "那个", "一下"}


def _fingerprint(record):
    try:
        return source_fingerprint(record)
    except (ValueError, TypeError):
        return ""


def _unique_quote(text, quote):
    start = text.find(quote)
    return start >= 0 and text.find(quote, start+1) < 0


class RecollectionRuntime:
    def __init__(self, bot):
        self.bot = bot
        self.store = RecollectionStore(Path(bot.ctx.paths.data_dir)/"recollection.sqlite3", **bot._ledger_storage_options())
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
        return bool(self.bot.config.recollection.enabled and self.bot._unified_active() and self.owner())

    def signature(self):
        b = self.bot
        return digest([self._generation, self.owner(), b.config.plugin.enabled,
                       b.config.recollection.model_dump(), b.config.unified_mind.model_dump(), b.config.general.timezone])

    def trusted(self, kwargs):
        if not self.enabled() or not self.bot._unified:
            return None
        request = dict(kwargs)
        if "message" in request:
            sender = extract_sender(request["message"])
            if not sender or sender.kind != "private" or sender.user_id != self.owner() or not sender.stream_id or not sender.message_id:
                return None
            request.update(session_id=sender.stream_id, reply_message_id=sender.message_id)
        context = self.bot._unified.context(request)
        return context if context and context["scope"] == "private:"+self.owner() and context["speaker_id"] == self.owner() else None

    def capture(self, message, *, now=None):
        if not self.enabled() or not isinstance(message, dict):
            return False, False
        text = message.get("processed_plain_text")
        if not isinstance(text, str):
            return False, False
        command = re.fullmatch(r"/更正 撤回 (recollection:revision:[0-9a-f]{64}) ([1-9][0-9]{0,8})", text.strip())
        if command:
            # Store independently verifies sender, pure text and receipt identity.
            before = self.store.state(self.owner())["revision"]
            self.store.retract(message, owner_id=self.owner(), revision_id=command[1],
                               expected_revision=int(command[2]), now=now)
            return self.store.state(self.owner())["revision"] != before, False
        if not self.bot.config.recollection.auto_correct or not should_reconsider(text) or PRIVATE.search(text):
            return False, False
        queued = self.store.enqueue(message, owner_id=self.owner(), now=now)
        if queued is None:
            self.last_status = "input_queue_full"
        return False, bool(queued)

    def active(self):
        return self.store.active(self.owner()) if self.enabled() else []

    def overlay(self, records):
        """Apply only to the exact live original; a withdrawal never revives a predecessor."""
        active = {}
        for row in self.active():
            active.setdefault(row["target_id"], []).append(row)
        result = []
        for record in records:
            version = source_fingerprint(record)
            rows = [row for row in active.get(record["id"], []) if version == row["target_digest"]
                    and _unique_quote(record["text"], row["old_quote"])]
            if not rows:
                result.append(record)
                continue
            spans = sorted([(record["text"].find(row["old_quote"]), row) for row in rows], key=lambda pair: -pair[0])
            text, boundary, malformed = record["text"], len(record["text"]), len(rows) > 4
            for start, row in spans:
                end = start+len(row["old_quote"])
                if end > boundary:
                    malformed = True
                    break
                stamp = datetime.fromtimestamp(row["input_timestamp"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                replacement = "【主人更正原话（自述；收到于"+stamp+"，未独立核实）："+row["quote"]+"】"
                text = text[:start]+replacement+text[end:]
                boundary = start
            if malformed or len(text) > 1800:
                continue  # Never fall back to a known stale claim on malformed overlap.
            latest = max(rows, key=lambda row: (row["input_timestamp"], row["id"]))
            result.append({**record, "text": text,
                "timestamp": latest["input_timestamp"], "evidence_status": "owner_reported",
                "sensitivity": max([record["sensitivity"]]+[row["sensitivity"] for row in rows], key=_SENSITIVITY.get),
                "origin_key": latest["input_id"], "correction_id": latest["id"]})
        return result

    def candidates(self, input):
        rows = {row["id"]: row for row in self.bot._collect_roots_raw(input["text"], limit=48)}
        active = self.active()
        rows.update(self.bot._resolve_roots_raw(list(dict.fromkeys(row["target_id"] for row in active))))
        previous = {}
        for row in active:
            previous.setdefault(row["target_id"], []).append(row)
        candidates = []
        terms = _terms(input["text"])
        for record in rows.values():
            if (record["source"] not in {"brain_fact", "mind", "bond", "continuity"}
                    or record["scope"] != "private:"+self.owner() or record["subject_id"] != self.owner()
                    or record["timestamp"] >= input["timestamp"] or not source_fingerprint(record)):
                continue
            record = dict(record)
            old = [row for row in previous.get(record["id"], []) if row["target_digest"] == source_fingerprint(record)]
            searchable = record["text"]
            if old:
                if any(row["input_timestamp"] >= input["timestamp"] for row in old) or len(old) > 4:
                    continue
                record["previous_corrections"] = [{"quote": row["quote"], "timestamp": row["input_timestamp"]} for row in old]
                searchable += " "+" ".join(row["quote"] for row in old)
            score = sum(term in searchable.casefold() for term in terms)
            if score:
                candidates.append((score, record))
        candidates.sort(key=lambda row: (-row[0], -row[1]["timestamp"], row[1]["id"]))
        return [row[1] for row in candidates[:12]]

    async def process(self):
        b = self.bot
        if not self.enabled() or not b.config.recollection.auto_correct or self._lock.locked():
            return
        async with self._lock:
            owner, signature = self.owner(), self.signature()
            claim = self.store.claim(owner, lease_seconds=min(600, max(120, b.config.unified_mind.model_timeout_seconds*2+30)))
            if not claim:
                self.last_status = "idle"
                return
            revision = claim["base_revision"]
            targets = []
            def valid():
                state = self.store.state(owner)
                raw = b._resolve_roots_raw([row["id"] for row in targets])
                return (self.enabled() and self.signature() == signature and state["revision"] == revision
                        and state["lease_id"] == claim["id"] and state["lease_token"] == claim["token"]
                        and state["lease_until"] > time.time() and b._self_state.state()["owner_id"] == owner
                        and all(_fingerprint(raw.get(row["id"])) == _fingerprint(row) for row in targets))
            def release(reason, failed=False):
                try:
                    if failed:
                        self.store.fail(claim["id"], claim["token"], owner_id=owner, reason=reason)
                    else:
                        self.store.defer(claim["id"], claim["token"], owner_id=owner, reason=reason,
                                         retry_at=time.time()+max(60, b.config.recollection.interval_seconds))
                except ValueError:
                    pass
            try:
                if not self.store.reserve_cycle(owner, day_start=day_start(b.config.general.timezone),
                    interval_seconds=b.config.recollection.interval_seconds, daily_limit=b.config.recollection.max_daily_batches):
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
                input = {key: claim[key] for key in ("id", "text", "timestamp")}
                targets = self.candidates(input)
                proposal = await understand_recollection(input, targets, generate=b.ctx.llm.generate,
                    reserve=reserve, valid=valid, task_name=b.config.brain.model_task, timeout=b.config.unified_mind.model_timeout_seconds)
                if proposal is None or not valid():
                    self.last_status = "model_budget" if exhausted else "context_changed" if not valid() else "review_failed"
                    release(self.last_status, failed=self.last_status == "review_failed")
                    return
                result = self.store.complete(claim["id"], claim["token"], proposal, owner_id=owner,
                    expected_revision=revision, targets=b._resolve_roots_raw([row["id"] for row in targets]))
                self.last_status = "updated" if result["applied"] else "no_supported_correction"
            except asyncio.CancelledError:
                release("interrupted")
                raise
            except Exception:
                self.last_status = "correction_failed"
                release(self.last_status, failed=True)

    async def sent(self, kwargs):
        """Receipt observation only: never sends or changes message contents."""
        if not self.enabled() or kwargs.get("sent") is not True:
            return False
        message = kwargs.get("message")
        if not isinstance(message, dict) or message.get("platform") != "qq":
            return False
        info = message.get("message_info")
        if not isinstance(info, dict) or info.get("group_info"):
            return False
        user = info.get("user_info")
        account = normalize_qq(message.get("account_id"))
        if not account or account == self.owner() or not isinstance(user, dict) or normalize_qq(user.get("user_id")) != account:
            return False
        sid, mid, text = message.get("session_id"), message.get("message_id"), message.get("processed_plain_text")
        if (not isinstance(sid, str) or not sid or len(sid) > 200 or not isinstance(mid, str) or not mid or len(mid) > 200
                or not isinstance(text, str) or not 1 <= len(text) <= 4000):
            return False
        questions = " ".join(part for part in re.split(r"(?<=[。！？!?\n])", text)
                             if _QUESTION.search(part) and not _MENTIONED_QUESTION.search(part))
        if not questions:
            return False
        owner, signature = self.owner(), self.signature()
        stream = await asyncio.wait_for(self.bot.ctx.chat.get_stream_by_user_id(owner, platform="qq"), 3)
        found = (str(stream.get("session_id") or stream.get("stream_id") or stream.get("id") or "") if isinstance(stream, dict)
                 else str(getattr(stream, "session_id", None) or getattr(stream, "stream_id", None) or getattr(stream, "id", None) or ""))
        if not self.enabled() or signature != self.signature() or found != sid:
            return False
        terms = [term for term in _terms(questions) if term not in _GENERIC]
        # A question alone is not enough: require a concrete, shared topic token.
        refs = []
        for row in self.bot._collect_roots(text):
            matches = [term for term in terms if term in row["text"].casefold()]
            if row["scope"] == "private:"+owner and row["subject_id"] == owner and matches:
                version = source_fingerprint(row)
                if version:
                    refs.append({"id": row["id"], "digest": version})
        if not refs:
            return False
        return self.store.record_delivery(owner_id=owner, scope="private:"+owner,
            receipt_id=digest([sid, mid]), topic_refs=refs[:12], text_digest=hashlib.sha256(text.encode()).hexdigest())

    def asked(self):
        rows = self.store.recent_deliveries(self.owner(), within_seconds=self.bot.config.recollection.repeat_cooldown_hours*3600)
        refs = {ref["id"]: ref["digest"] for row in reversed(rows) for ref in row["topic_refs"]}
        ids = list(refs)
        current = {}
        for offset in range(0, len(ids), 128):
            current.update(self.bot._resolve_roots(ids[offset:offset+128]))
        return {ident for ident, version in refs.items() if _fingerprint(current.get(ident)) == version}

    def repeat_hint(self, kwargs):
        context = self.trusted(kwargs)
        if not context:
            return ""
        return self.repeat_style()

    def with_questions(self, records):
        if not self.enabled():
            return records
        asked = self.asked()
        return [{**row, "recently_asked": True} if row["id"] in asked else row for row in records]

    def repeat_style(self):
        if not self.enabled() or not self.asked():
            return ""
        # No private fact text bypasses the normal expression review here.
        return ("你近期确实向这位主人问过仍无新证据的近况；不要用例行追问填满对话。"
                "若本轮未主动提到相关事情，先接住眼前的话，不再次索要进展；本轮主动聊起时自然回应。"
                "‘终于结束了’等含糊话不能擅自匹配某件旧事或宣布完成；必要时只作简短澄清。")

    def inspect(self, kwargs, *, offset=0):
        if not self.trusted(kwargs):
            return {"success": False, "code": "owner_private_required"}
        if type(offset) is not int or not 0 <= offset <= 200:
            return {"success": False, "code": "invalid_offset"}
        active = self.active()
        raw = self.bot._resolve_roots_raw(list(dict.fromkeys(row["target_id"] for row in active)))
        return {"success": True, "data": {
            "processing": {key: value for key, value in self.store.status(self.owner()).items()
                           if key in {"queued_inputs", "input_capacity", "queue_full", "active_revisions", "revision_capacity"}},
            "corrections": [{key: row[key] for key in ("id", "target_id", "quote", "old_quote", "relation", "revision")}
                            | {"effective": _fingerprint(raw.get(row["target_id"])) == row["target_digest"]
                               and _unique_quote(raw[row["target_id"]]["text"], row["old_quote"])} for row in active[offset:offset+20]],
            "has_more": offset+20 < len(active), "total": len(active),
            "instructions": "这是经过原话复核的自述更正，不代表独立证实；原始记录保留。撤回需主人私聊 /更正 撤回 <id> <revision>；仅恢复该片段原文，其它独立更正保留，不复活同片段更早更正。"}}
