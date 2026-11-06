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
from .owner import direct_message_text, extract_sender, normalize_qq
from .reference_store import ReferenceStore
from .social_discovery import interest_followup_question, reading_reaction_question
from .unified_expression import _can_say, _records
from .unified_memory import _source_id, derived_memories


_FOLLOWUPS = {"那件事", "那件事呢", "那件事后来呢", "那件事后来怎么样了", "那件事怎么样了", "那个呢", "这个呢",
              "后来呢", "然后呢", "接着呢", "接着说", "继续说", "继续讲", "说下去", "刚才那个", "刚才那件事", "上次那件事",
              "那件事后来怎么样", "刚才说到哪了", "我们刚才说到哪了", "上次说到哪了", "上次聊到哪了",
              "继续", "接着说吧", "后来怎么样"}
_CLEAR = {"/话题 清除", "换个话题", "换个话题吧", "这件事先不聊了", "先不聊这个了", "别再提这件事了"}
_NEUTRAL = {"你好", "嗨", "在吗", "早上好", "晚上好", "早安", "晚安", "嗯", "嗯嗯", "好", "好的", "知道了",
            "谢谢", "谢谢你", "我回来了", "回来了", "我忙完了", "忙完了"}
_QUESTION_PROGRESS = {"那个问题后来弄明白了吗", "之前那个问题后来弄明白了吗", "那个问题弄明白了吗",
                      "那个问题后来有答案了吗", "之前那个问题有答案了吗", "那个问题有答案了吗",
                      "刚才那个问题查明白了吗", "那个问题查得怎么样了", "之前那个问题查得怎么样了"}
_OPINION_FOLLOWUPS = {"你现在怎么看这件事", "你现在怎么看这个", "你现在还是这么想吗", "你现在还这么想吗",
                     "你的看法变了吗", "你对这件事的看法变了吗", "你对这个的看法变了吗",
                     "你之前不是这么想的吗", "你之前也是这么想的吗", "你为什么改变看法", "你为什么改变看法了"}
_CLARIFY = "[MaiLivingWorld:reference]\n这句短指代暂不能唯一对应仍有效的话题。先简短问清指的是哪件事，不猜对象或编造后续。"
_CONTINUE = "[MaiLivingWorld:reference]\n这句短指代可承接最近实际提及且来源仍有效的同一话题。只依据本轮允许表达的记忆，不补猜新的进展。"
_REACTION = ("主人在问你对刚才材料的看法。先说其中具体吸引你、让你犹豫或不认同的一点，"
             "把报道内容和自己的判断说清楚。只有本轮提供相关的既有兴趣、观点或感受时，"
             "才据此延续原来的态度；也可以说还没形成看法。问句预设的喜欢或不喜欢须以已有倾向为准，"
             "过去的偏好或前后变化只有本轮历史依据明确时才能确认。缺少历史时可以说明无法确定，"
             "形成态度的原因缺少依据时可以直接说还不确定。不补写亲身经历、阅读进度或长久偏好。")
_PROGRESS = ("主人在问先前提过的材料相关的问题进展。结合本轮问题状态和原始材料回应，保留来源中的限定。"
             "旧问题记录可能仍是原来的问句，标记解决本身不构成答案。按实际提供的材料解释具体内容，依据不足就保留疑问。"
             "先前提过材料不证明主人曾问过这个问题或你曾承诺研究，结题状态也不证明实际联网查阅；只按已有记录表达。")
_NO_PROGRESS = "[MaiLivingWorld:reference]\n这份材料目前缺少可核验的问题进展记录。简短说明还无法确认，先问清具体问题。"
_MANY_QUESTIONS = "[MaiLivingWorld:reference]\n这份材料关联多个仍有效的问题。先问清指的是哪个问题，再谈进展。"
_OPINION = ("主人在问你对刚才材料的当前看法或看法变化。只按本轮获准表达的自身观点回应，保留暂时倾向、"
            "分歧与待核对候选的限定。只有记录明确包含调整前后的看法时，才描述变化。"
            "问句中的改变预设需要核实，缺少历史时可以说目前无法确认以前是否这样想。"
            "原因只按获准表达的原始依据说明，材料存在本身不证明改变原因，不补写经历或学习过程。"
            "曾提过材料不证明曾向主人说过这条观点，也不证明主人认同。")
_NO_OPINION = "[MaiLivingWorld:reference]\n这份材料目前缺少可核验的自身观点记录。可以说明还没有明确的看法，先问清具体想讨论哪一点。"
_MANY_OPINIONS = "[MaiLivingWorld:reference]\n这份材料关联多个仍有效的自身观点。先问清指的是哪一条看法，再谈是否改变。"
_INTEREST = ("主人在问刚才材料相关的自身兴趣。按本轮获准表达的兴趣回答当前倾向。只有记录明确包含前后变化时，"
             "才谈以前的喜好和变化方向。历史不足时可以说明以前的倾向还无法确认。形成原因另需原始材料支持。")
_NO_INTEREST = "[MaiLivingWorld:reference]\n目前无法确认对这部分内容的喜好，可以直接说明。不要按问句的预设补写以前的态度。"
_WHICH_INTEREST = "[MaiLivingWorld:reference]\n先简短问清对方指刚才材料里的哪一部分，等明确后再聊喜好。"
_BOILERPLATE = re.compile(r"MaiLivingWorld|主人(?:原话|更正|自述)|未经?独立(?:核实|验证)|作品元数据|未阅读或观看正文|记录于|收到于", re.I)
_END_PUNCTUATION = " \t\n\r。！？.!?"


def _short(text):
    return text.strip().rstrip("。？！?!～~") if isinstance(text, str) and len(text) <= 80 else ""


def is_followup(text):
    return (_short(text) in _FOLLOWUPS or reading_reaction_question(text)
            or question_progress(text) or opinion_followup(text))


def question_progress(text):
    return _short(text) in _QUESTION_PROGRESS


def opinion_followup(text):
    return _short(text) in _OPINION_FOLLOWUPS


def _ref(row):
    source, _ = _source_id(row["id"])
    if source != row["source"]:
        raise ValueError("invalid_reference_source")
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


def _confirmed_mentions(records, matched, spoken):
    """A long-lived mention needs the whole source, unique among eligible candidates."""
    result = []
    for row in matched:
        quote = row["text"].strip().rstrip(_END_PUNCTUATION)
        if (len(quote) <= 600 and len(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", quote)) >= 8
                and not _BOILERPLATE.search(quote) and spoken.count(quote) == 1
                and all(quote not in other["text"] for other in records if other["id"] != row["id"])):
            result.append(_ref(row))
    return result


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
        if reading_reaction_question(text) or question_progress(text) or opinion_followup(text):
            pure = pure and direct_message_text(message, self.owner()) == text
        control = bool(message.get("is_command") or isinstance(text, str) and text.startswith("/"))
        keep = bool(_short(text) not in _CLEAR and (not pure or control or is_followup(text) or _short(text) in _NEUTRAL))
        receipt = digest(["input", self.owner(), sender.stream_id, sender.message_id])
        changed = self.store.capture(self.owner(), receipt, digest(text), keep_focus=keep, invalidate_focus=not pure,
                                     clear_mentions=pure and isinstance(text, str) and text.strip() == "/话题 清除")
        if changed:
            self._staged.clear()
            self._stageable = receipt if pure and not control and isinstance(text, str) else ""
        return changed

    def mark_prior_mentions(self, records, context):
        """Annotate pre-filtered foreground candidates only within the verified current input."""
        cleaned = []
        for row in records:
            if isinstance(row, dict):
                row = dict(row)
                row.pop("previously_mentioned", None)
            cleaned.append(row)
        if not cleaned or not isinstance(context, dict) or not context.get("sid") or not context.get("mid"):
            return cleaned
        if self.trusted({"session_id": context["sid"], "reply_message_id": context["mid"]}) != context:
            return cleaned
        owner = self.owner()
        input_id = digest(["input", owner, context["sid"], context["mid"]])
        if self._stageable != input_id:
            return cleaned
        state = self.store.state(owner)
        if state["last_input"] != input_id or not state["prior_mentions"]:
            return cleaned
        normalized = _records(cleaned)
        candidates = {}
        for row in cleaned:
            if not isinstance(row, dict) or row.get("id") not in normalized or row.get("derived"):
                continue
            if not _can_say(normalized[row["id"]], context["scope"], owner):
                continue
            try:
                fingerprint = source_fingerprint(row)
            except ValueError:
                continue
            if fingerprint in state["prior_mentions"]:
                candidates[row["id"]] = (row, fingerprint)
        if candidates:
            current = self.bot._resolve_roots(list(candidates))
            for ident, (row, fingerprint) in candidates.items():
                try:
                    if ident in current and source_fingerprint(current[ident]) == fingerprint:
                        row["previously_mentioned"] = True
                except ValueError:
                    continue
        return cleaned

    def _allowed(self, records, context):
        normalized = _records(records)
        allowed = [r for r in records if isinstance(r, dict) and r.get("id") in normalized
                   and _can_say(normalized[r["id"]], context["scope"], self.owner()) and not r.get("derived")]
        return self.bot._expression_records(allowed, context=context)

    def _resolve(self, refs, context, *, require_all=True):
        if not refs:
            return []
        try:
            found = self.bot._resolve_roots([row["id"] for row in refs])
            records = []
            for ref in refs:
                try:
                    record = found[ref["id"]]
                    if _ref(record) == ref:
                        records.append(record)
                except (ValueError, KeyError, TypeError):
                    continue
            allowed = self._allowed(records, context)
            return allowed if not require_all or len(allowed) == len(refs) else []
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
            hint = _CONTINUE + ("\n" + _REACTION if reading_reaction_question(query) else "")
            result.update(query=query+" "+records[0]["text"][:240], records=records, hint=hint, status="resolved")
            if question_progress(query):
                return self._question_progress(result, records[0])
            if opinion_followup(query):
                return self._opinion_followup(result, records[0])
            if interest_followup_question(query):
                return self._interest_followup(result, records[0])
        return result

    def _interest_followup(self, result, focus):
        b = self.bot
        result = {**result, "interest_followup": True, "records": [], "hint": _NO_INTEREST, "status": "unavailable"}
        focus_ref = _ref(focus)
        interests = [item for item in b._interest_memories() if focus_ref in item["sources"]]
        if len(interests) > 1:
            # A hidden interest can make the referent ambiguous. Ask without listing alternatives.
            return {**result, "hint": _WHICH_INTEREST, "status": "ambiguous"}
        if not interests:
            return result
        refs = interests[0]["sources"]
        roots = b._resolve_roots([ref["id"] for ref in refs])
        if any(ref["id"] not in roots or _ref(roots[ref["id"]]) != ref for ref in refs):
            return result
        others = sorted((row for row in roots.values() if row["id"] != focus["id"]),
                        key=lambda row: (-row["timestamp"], row["id"]))
        selected = ([focus]+others)[:b.config.unified_mind.recall_limit-1]
        hint = _CONTINUE+"\n"+_INTEREST
        if not selected:
            hint += "本轮未提供原文，具体原因需要保留不确定。"
        return {**result, "records": selected+interests, "hint": hint, "status": "resolved"}

    def _opinion_followup(self, result, focus):
        b = self.bot
        result = {**result, "opinion_followup": True, "records": [], "hint": _NO_OPINION, "status": "unavailable"}
        if not (b.config.growth.enabled and b.config.growth.mode == "active" and b._ensure_growth()):
            return result
        # Check the full valid set before applying the reply limit. One visible
        # source may support several opinions even when only one fits the prompt.
        beliefs = [item for item in b._growth_snapshot().get("beliefs", [])
                   if item.get("status") == "active" and any(ref["id"] == focus["id"] for ref in item["sources"])]
        if len(beliefs) > 1:
            return {**result, "hint": _MANY_OPINIONS, "status": "ambiguous"}
        if not beliefs:
            return result
        roots = b._resolve_roots([ref["id"] for ref in beliefs[0]["sources"]])
        opinions = derived_memories({"beliefs": beliefs}, roots, fingerprint=source_fingerprint,
                                    query=result["query"], limit=1)
        if len(opinions) != 1:
            return result
        others = sorted((row for row in roots.values() if row["id"] != focus["id"]),
                        key=lambda row: (-row["timestamp"], row["id"]))
        selected = ([focus]+others)[:b.config.unified_mind.recall_limit-1]
        hint = _CONTINUE+"\n"+_OPINION
        if not selected:
            hint += "本轮未提供原文，形成或改变看法的具体原因需要保留不确定。"
        elif len(selected) < len(roots):
            hint += "本轮只提供有限原始材料，未提供的细节不能补猜。"
        return {**result, "records": selected+opinions, "hint": hint, "status": "resolved"}

    def _question_progress(self, result, focus):
        b = self.bot
        result = {**result, "question_progress": True, "records": [], "hint": _NO_PROGRESS, "status": "unavailable"}
        if not (b.config.growth.enabled and b.config.growth.mode == "active" and b._ensure_growth()):
            return result
        # Inspect the full validated question set before ranking, so truncation
        # cannot turn several questions about one source into a unique match.
        questions = [q for q in b._growth_snapshot().get("questions", [])
                     if q.get("status") in {"open", "resolved"}
                     and any(ref["id"] == focus["id"] for ref in q["sources"])]
        if len(questions) > 1:
            return {**result, "hint": _MANY_QUESTIONS, "status": "ambiguous"}
        if not questions:
            return result
        roots = b._resolve_roots([ref["id"] for ref in questions[0]["sources"]])
        progress = derived_memories({"questions": questions}, roots, fingerprint=source_fingerprint,
                                    query=result["query"], limit=1)
        if len(progress) != 1:
            return result
        others = sorted((row for row in roots.values() if row["id"] != focus["id"]),
                        key=lambda row: (-row["timestamp"], row["id"]))
        selected = ([focus]+others)[:b.config.unified_mind.recall_limit-1]
        hint = _CONTINUE+"\n"+_PROGRESS
        if not selected:
            hint += "本轮未提供原文，不能只凭已解决状态解释具体答案。"
        elif len(selected) < len(roots):
            hint += "本轮只提供有限原始材料，未提供的细节不能补猜。"
        return {**result, "records": selected+progress, "hint": hint, "status": "resolved"}

    def _live(self, state):
        now = time.time()
        return bool(state["active"] and 0 < state["mentioned_at"] <= now
                    and now < min(state["expires"], state["mentioned_at"]+self.bot.config.reference.ttl_hours*3600))

    def stage(self, kwargs, records):
        context = self.trusted(kwargs)
        if not context or not isinstance(records, list):
            return
        previous = self._staged.get(context["sid"])
        if not records and not previous:
            return
        state = self.store.state(self.owner())
        receipt = digest(["input", self.owner(), context["sid"], context["mid"]])
        if state["last_input"] != receipt or self._stageable != receipt or _short(context["query"]) in _CLEAR:
            return
        candidates = []
        for row in self._allowed(records[:12], context):
            try:
                candidates.append(_ref(row))
            except (ValueError, KeyError, TypeError):
                continue
        refs = [_ref(row) for row in self._resolve(candidates, context, require_all=False)]
        if not refs and not previous:
            return
        signature, now = self.signature(), time.monotonic()
        if (previous and previous["context"] == context and previous["signature"] == signature
                and previous["revision"] == state["revision"] and previous["until"] > now):
            # A repeated hook replaces unsent candidates while retaining topics
            # actually delivered in prior chunks of the same reply.
            self._staged[context["sid"]] = {**previous, "refs": refs}
        elif refs:
            self._staged[context["sid"]] = {"context": dict(context), "signature": signature, "refs": refs,
                "revision": state["revision"], "input_receipt": receipt, "mentioned": [], "until": now+90}
        else:
            self._staged.pop(context["sid"], None)
        while len(self._staged) > 16:
            self._staged.pop(next(iter(self._staged)))

    async def sent(self, kwargs):
        message = kwargs.get("message") if isinstance(kwargs, dict) else None
        sid = message.get("session_id") if isinstance(message, dict) else None
        expected = self._staged.get(sid) if isinstance(sid, str) else None
        if not expected or not expected["refs"]:
            return False
        async with self._send_lock:
            if self._staged.get(sid) is not expected:
                return False
            return await self._sent(kwargs, expected)

    async def _sent(self, kwargs, expected):
        if not self.enabled():
            return False
        signature, owner = self.signature(), self.owner()
        receipt = await verify_owner_delivery(self.bot, kwargs)
        if not receipt or not self.enabled() or signature != self.signature():
            return False
        staged = self._staged.get(receipt["sid"])
        if staged is not expected or staged["signature"] != signature or staged["until"] <= time.monotonic():
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
            ttl_seconds=self.bot.config.reference.ttl_hours*3600,
            confirmed_refs=_confirmed_mentions(records, matched, receipt["text"]))
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
            "instructions": "投递记录只支持曾在私聊提过相关内容。主人可完整私聊 /话题 清除，同时清空当前承接和历史提及标记，保留原记忆。"}
