"""Resolve short follow-ups only from verified recent owner-private mentions."""
from __future__ import annotations

import asyncio
from difflib import SequenceMatcher
from pathlib import Path
import re
import time

from .delivery import verify_owner_delivery
from .growth_store import source_fingerprint
from .mcp_runtime import digest
from .owner import extract_sender, normalize_qq
from .reference_store import ReferenceStore
from .unified_expression import _can_say, _records


_FOLLOWUPS = {"那件事", "那件事呢", "那件事后来呢", "那件事后来怎么样了", "那件事怎么样了", "那个呢", "这个呢",
              "后来呢", "然后呢", "接着呢", "接着说", "继续说", "继续讲", "说下去", "刚才那个", "刚才那件事", "上次那件事",
              "那件事后来怎么样", "刚才说到哪了", "我们刚才说到哪了", "上次说到哪了", "上次聊到哪了",
              "继续", "接着说吧", "后来怎么样"}
_CLEAR = {"/话题 清除", "换个话题", "换个话题吧", "这件事先不聊了", "先不聊这个了", "别再提这件事了"}
_NEUTRAL = {"你好", "嗨", "在吗", "早上好", "晚上好", "早安", "晚安", "嗯", "嗯嗯", "好", "好的", "知道了",
            "谢谢", "谢谢你", "我回来了", "回来了", "我忙完了", "忙完了"}
_CLARIFY = "[MaiLivingWorld:reference]\n这句短指代暂不能唯一对应仍有效的话题。先简短问清指的是哪件事，不猜对象或编造后续。"
_CONTINUE = "[MaiLivingWorld:reference]\n这句短指代可承接最近实际提及且来源仍有效的同一话题。只依据本轮允许表达的记忆，不补猜新的进展。"
_BOILERPLATE = re.compile(r"MaiLivingWorld|主人(?:原话|更正|自述)|未经?独立(?:核实|验证)|作品元数据|未阅读或观看正文|记录于|收到于", re.I)


def _short(text):
    return text.strip().rstrip("。？！?!～~") if isinstance(text, str) and len(text) <= 80 else ""


def is_followup(text):
    return _short(text) in _FOLLOWUPS


def _ref(row):
    return {"id": row["id"], "digest": source_fingerprint(row), "scope": row["scope"], "sensitivity": row["sensitivity"]}


def _anchor(source, spoken):
    """A literal match is evidence of mention, not evidence of agreement or truth."""
    if not isinstance(source, str) or not isinstance(spoken, str):
        return False
    for block in SequenceMatcher(None, source[:1800], spoken[:1800], autojunk=False).get_matching_blocks():
        quote = source[block.a:block.a+block.size].strip(" \n\r\t，。！？：；、,.!?;:\"'“”‘’「」()（）")
        if (8 <= len(quote) <= 600 and source.count(quote) == 1 and spoken.count(quote) == 1
                and len(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", quote)) >= 8 and not _BOILERPLATE.search(quote)):
            return True
    return False


class ReferenceRuntime:
    def __init__(self, bot):
        self.bot = bot
        self.store = ReferenceStore(Path(bot.ctx.paths.data_dir)/"reference.sqlite3", **bot._ledger_storage_options())
        self._generation = 0
        self._staged = {}
        self._stageable = ""
        self._send_lock = asyncio.Lock()

    def reset(self):
        self._generation += 1
        self._staged.clear()
        self._stageable = ""

    def close(self):
        self.reset()
        self.store.close()

    def owner(self):
        return normalize_qq(self.bot.config.owner.qq) if self.bot.config.owner.enabled else ""

    def enabled(self):
        b, owner = self.bot, self.owner()
        return bool(b.config.plugin.enabled and b.config.reference.enabled and b._unified_active() and owner
                    and b._self_state and b._self_state.state()["owner_id"] == owner)

    def signature(self):
        b = self.bot
        return digest([self._generation, self.owner(), b.config.plugin.enabled, b.config.reference.model_dump(),
                       b.config.unified_mind.model_dump(), getattr(b, "_mcp_generation", 0),
                       self.store.state(self.owner())["revision"] if self.owner() else 0])

    def trusted(self, kwargs):
        if not self.enabled() or not isinstance(kwargs, dict) or not self.bot._unified:
            return None
        request = dict(kwargs)
        if "message" in request:
            sender = extract_sender(request["message"])
            if not sender or sender.kind != "private" or sender.user_id != self.owner():
                return None
            request.update(session_id=sender.stream_id, reply_message_id=sender.message_id)
        context = self.bot._unified.context(request)
        latest = self.bot._unified.context({"session_id": context["sid"]}) if context else None
        return context if (context and latest and latest["mid"] == context["mid"]
                           and context["scope"] == "private:"+self.owner() and context["speaker_id"] == self.owner()) else None

    def capture(self, message):
        if not self.enabled() or not isinstance(message, dict):
            return False
        sender = extract_sender(message)
        if (not sender or sender.kind != "private" or sender.user_id != self.owner() or not sender.stream_id or not sender.message_id
                or normalize_qq(message.get("account_id")) == self.owner()):
            return False
        parts, text = message.get("raw_message"), message.get("processed_plain_text")
        pure = not (isinstance(parts, list) and any(not isinstance(p, dict) or p.get("type") != "text" for p in parts)
                    or any(message.get(k) for k in ("is_picture", "is_voice", "is_emoji")))
        control = bool(message.get("is_command") or isinstance(text, str) and text.startswith("/"))
        keep = bool(_short(text) not in _CLEAR and (not pure or control or is_followup(text) or _short(text) in _NEUTRAL))
        receipt = digest(["input", self.owner(), sender.stream_id, sender.message_id])
        changed = self.store.capture(self.owner(), receipt, digest(text), keep_focus=keep, invalidate_focus=not pure)
        if changed:
            self._staged.clear()
            self._stageable = receipt if pure and not control and isinstance(text, str) else ""
        return changed

    def _allowed(self, records, context):
        normalized = _records(records)
        allowed = [r for r in records if isinstance(r, dict) and r.get("id") in normalized
                   and _can_say(normalized[r["id"]], context["scope"], self.owner()) and not r.get("derived")]
        return self.bot._expression_records(allowed, context=context)

    def _resolve(self, refs, context):
        try:
            found = self.bot._resolve_roots([row["id"] for row in refs])
            if any(row["id"] not in found or _ref(found[row["id"]]) != row for row in refs):
                return []
            records = [found[row["id"]] for row in refs]
            allowed = self._allowed(records, context)
            return allowed if len(allowed) == len(records) else []
        except (ValueError, KeyError, TypeError):
            return []

    def resolve(self, kwargs):
        context = self.trusted(kwargs)
        query = context["query"] if context else ""
        result = {"query": query, "records": [], "hint": "", "status": "normal" if context else "untrusted"}
        if not context or not is_followup(query):
            return result
        result.update(hint=_CLARIFY, status="unavailable")
        state = self.store.state(self.owner())
        if self._stageable != digest(["input", self.owner(), context["sid"], context["mid"]]):
            return result
        if not self._live(state):
            return result
        if len(state["focus_refs"]) != 1:
            result["status"] = "ambiguous"
            return result
        records = self._resolve(state["focus_refs"], context)
        if len(records) == 1:
            result.update(query=query+" "+records[0]["text"][:240], records=records, hint=_CONTINUE, status="resolved")
        return result

    def _live(self, state):
        now = time.time()
        return bool(state["active"] and 0 < state["mentioned_at"] <= now
                    and now < min(state["expires"], state["mentioned_at"]+self.bot.config.reference.ttl_hours*3600))

    def stage(self, kwargs, records):
        context = self.trusted(kwargs)
        if not context or not isinstance(records, list):
            return
        state = self.store.state(self.owner())
        receipt = digest(["input", self.owner(), context["sid"], context["mid"]])
        if state["last_input"] != receipt or self._stageable != receipt or _short(context["query"]) in _CLEAR:
            return
        refs = []
        for row in self._allowed(records[:12], context):
            try:
                ref = _ref(row)
                if self._resolve([ref], context):
                    refs.append(ref)
            except (ValueError, KeyError, TypeError):
                continue
        if refs:
            previous = self._staged.get(context["sid"])
            signature, now = self.signature(), time.monotonic()
            if (previous and previous["context"] == context and previous["signature"] == signature
                    and previous["revision"] == state["revision"] and previous["until"] > now):
                # A Host may run the reply hook again before its next chunk.
                # This must not erase mentions already confirmed for this reply.
                merged = {row["id"]: row for row in previous["mentioned"]+refs+previous["refs"]}
                previous["refs"] = list(merged.values())[:12]
            else:
                self._staged[context["sid"]] = {"context": dict(context), "signature": signature, "refs": refs,
                    "revision": state["revision"], "input_receipt": receipt, "mentioned": [], "until": now+90}
            while len(self._staged) > 16:
                self._staged.pop(next(iter(self._staged)))

    async def sent(self, kwargs):
        message = kwargs.get("message") if isinstance(kwargs, dict) else None
        sid = message.get("session_id") if isinstance(message, dict) else None
        expected = self._staged.get(sid) if isinstance(sid, str) else None
        if expected is None:
            return False
        async with self._send_lock:
            if self._staged.get(sid) is not expected:
                return False
            return await self._sent(kwargs)

    async def _sent(self, kwargs):
        if not self.enabled():
            return False
        signature, owner = self.signature(), self.owner()
        receipt = await verify_owner_delivery(self.bot, kwargs)
        if not receipt or not self.enabled() or signature != self.signature():
            return False
        staged = self._staged.get(receipt["sid"])
        if not staged or staged["signature"] != signature or staged["until"] <= time.monotonic():
            return False
        context = self.trusted({"session_id": receipt["sid"], "reply_message_id": staged["context"]["mid"]})
        if context != staged["context"]:
            return False
        records = self._resolve(staged["refs"], context)
        matched = [row for row in records if _anchor(row["text"], receipt["text"])]
        if not matched:
            return False
        # Host replies may be split into several successfully delivered chunks.
        # Keep every grounded topic seen in this bounded reply window so a later
        # short pronoun cannot silently select only the first chunk's topic.
        mentioned = {row["id"]: row for row in staged["mentioned"]}
        mentioned.update({row["id"]: _ref(row) for row in matched})
        refs = list(mentioned.values())[:3]
        applied = self.store.delivered(owner, digest(["send", owner, receipt["receipt_id"]]), receipt["text_digest"],
            refs, expected_revision=staged["revision"], input_receipt=staged["input_receipt"],
            ttl_seconds=self.bot.config.reference.ttl_hours*3600)
        if applied:
            state = self.store.state(owner)
            if (self._staged.get(receipt["sid"]) is staged and state["revision"] == staged["revision"]+1
                    and state["last_input"] == staged["input_receipt"]):
                staged.update(mentioned=refs, revision=state["revision"], signature=self.signature())
            else:
                self._staged.pop(receipt["sid"], None)
        return applied

    def inspect(self, kwargs):
        context = self.trusted(kwargs)
        if not context:
            return {"success": False, "code": "owner_private_required"}
        state, resolved = self.store.state(self.owner()), self.resolve(kwargs)
        records = self._resolve(state["focus_refs"], context) if self._live(state) else []
        return {"success": True, "data": {"active": bool(records), "source_count": len(records),
            "status": resolved["status"], "expires": state["expires"] if records else 0},
            "instructions": "仅记录可验证实际提及的话题引用，不代表双方认同或有了新进展。主人可完整私聊 /话题 清除；此命令只清当前承接，不删除原记忆。"}
