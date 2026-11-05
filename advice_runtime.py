"""Owner-private suggestion delivery and finite feedback learning, with no automatic sender."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time

from .advice_model import target_for, understand_feedback
from .advice_store import AdviceStore, LABELS, digest, parse_command
from .delivery import verify_owner_delivery
from .growth_model import _safe_text
from .growth_store import source_fingerprint
from .mcp_runtime import day_start
from .mind import PRIVATE
from .memory_archive import tier
from .owner import extract_sender, normalize_qq
from .unified_expression import parse_expression
from .unified_memory import _source_id, _terms


def _ref(row):
    return {"id": row["id"], "digest": source_fingerprint(row), "scope": row["scope"], "sensitivity": row["sensitivity"]}


class AdviceRuntime:
    def __init__(self, bot):
        self.bot = bot
        self.store = AdviceStore(Path(bot.ctx.paths.data_dir)/"advice.sqlite3", **bot._ledger_storage_options())
        self._generation = 0
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
        return bool(b.config.plugin.enabled and b.config.advice.enabled and b._unified_active() and owner
                    and b._self_state and b._self_state.state()["owner_id"] == owner)

    def signature(self):
        b = self.bot
        return digest([self._generation, self.owner(), b.config.plugin.enabled, b.config.advice.model_dump(), b.config.unified_mind.model_dump()])

    def trusted(self, kwargs):
        if not self.enabled() or not isinstance(kwargs, dict) or not self.bot._unified:
            return None
        request = dict(kwargs)
        if "message" in request:
            sender = extract_sender(request["message"])
            if not sender or sender.kind != "private" or sender.user_id != self.owner() or not sender.stream_id or not sender.message_id:
                return None
            request.update(session_id=sender.stream_id, reply_message_id=sender.message_id)
        context = self.bot._unified.context(request)
        latest = self.bot._unified.context({"session_id": context["sid"]}) if context else None
        return context if context and latest and latest["mid"] == context["mid"] and context["scope"] == "private:"+self.owner() and context["speaker_id"] == self.owner() else None

    def _resolve(self, row):
        refs = row.get("source_refs", [])
        try:
            sources = self.bot._resolve_roots([r["id"] for r in refs])
            return [sources[r["id"]] for r in refs] if refs and all(r["id"] in sources and _ref(sources[r["id"]]) == r for r in refs) else None
        except (ValueError, TypeError, KeyError):
            return None

    def _allowed(self, sources, context=None):
        if not sources:
            return False
        owner = self.owner()
        context = context or {"scope": "private:"+owner, "speaker_id": owner}
        filtered = self.bot._expression_records(sources, context)
        if len(filtered) != len(sources):
            return False
        response = {"response": json.dumps({"behavior": "neutral", "decisions": [{"id": r["id"], "mode": "say"} for r in sources]})}
        result = parse_expression(response, sources, audience_scope="private:"+owner, speaker_id=owner, is_owner=True)
        return bool(result and len(result["memories"]) == len(sources))

    async def tool(self, action="list", topic="", advice="", source_ids=None, kwargs=None, *, offset=0):
        context = self.trusted(kwargs or {})
        if not context:
            return {"success": False, "code": "owner_private_required"}
        if action == "sources":
            query = _safe_text(topic, 60, None) if topic else context["query"]
            if not query:
                return {"success": False, "code": "invalid_advice_query"}
            rows = []
            for record in self.bot._collect_roots(query):
                try:
                    _source_id(record["id"])
                except (KeyError, ValueError, TypeError):
                    continue
                if self._allowed([record], context):
                    rows.append({key: record[key] for key in ("id", "text", "evidence_status")})
                if len(rows) == 8:
                    break
            return {"success": True, "data": rows, "instructions": "这些是当前允许表达的原始讨论依据，"
                    "可选1到4个id用于prepare，不代表任何建议已经证实有效；不要编造缺失来源。"}
        if action in {"list", "search"}:
            try:
                # Search/forget remains available even after automatic archiving
                # is disabled; the owner never loses control of old records.
                rows = self.store.search(self.owner(), topic if action == "search" else "", limit=21, offset=offset)
            except (ValueError, TypeError):
                return {"success": False, "code": "invalid_archive_query"}
            items = []
            for row in rows[:20]:
                allowed = row["status"] != "forgotten" and self._allowed(self._resolve(row), context)
                items.append({"id": row["id"], "revision": row["revision"], "status": row["status"],
                    "memory_tier": tier(row), "topic": row["topic"] if allowed else "已遗忘或来源受限的建议", "outcome": row["outcome"] if allowed else "unknown",
                    "advice": row["advice"] if allowed and action == "search" else "",
                    "feedback": row["feedback"] if allowed and action == "search" else ""})
            return {"success": True, "data": items, "next_offset": offset+20 if len(rows) > 20 else None,
                    "storage": self.store.archive_status(self.owner()),
                    "instructions": "效果仅是主人本人自述，不是客观验证；未反馈不等于有效。"
                    "search用topic检索历史，offset分页；归档只减少默认想起，不是遗忘或新的证据。"
                    "主人完整私聊 /建议 尝试过|没尝试|有用|没用|停止|忘记 <id> <revision>。停止不再参与建议，忘记清除正文。"}
        if action != "prepare":
            return {"success": False, "code": "invalid_advice_action"}
        try:
            topic, advice = _safe_text(topic, 60, None), _safe_text(advice, 240, None)
            if not topic or not advice or PRIVATE.search(topic+advice) or not isinstance(source_ids, list) or not 1 <= len(source_ids) <= 4 or len(set(source_ids)) != len(source_ids):
                raise ValueError("invalid_advice")
            sources = self.bot._resolve_roots(source_ids)
            if len(sources) != len(source_ids) or not self._allowed(list(sources.values()), context):
                return {"success": False, "code": "advice_source_not_expressible"}
            row = self.store.prepare(self.owner(), context["sid"], context["mid"], topic, advice, [_ref(sources[i]) for i in source_ids],
                                     archive_enabled=getattr(self.bot.config.advice, "archive_enabled", False))
            return {"success": True, "data": {"id": row["id"], "revision": row["revision"], "status": row["status"]},
                    "delivery_text": row["delivery_text"], "instructions": "这是供主人选择的建议，不是已经验证的事实或已经执行的行动。"
                    "如决定建议，delivery_text原样独立整条发送才登记送达；不要声称主人会尝试或有效。草稿十分钟过期。"}
        except (ValueError, TypeError, KeyError):
            return {"success": False, "code": "advice_unavailable"}

    async def sent(self, kwargs):
        if not self.enabled():
            return False
        signature = self.signature()
        receipt = await verify_owner_delivery(self.bot, kwargs)
        if not receipt or not self.enabled() or signature != self.signature():
            return False
        rows = [row for row in self.store.drafts(self.owner()) if row.get("delivery_text") == receipt["text"] and row.get("sid") == receipt["sid"]]
        if len(rows) != 1:
            return False
        row = rows[0]
        context = self.trusted({"session_id": row["sid"], "reply_message_id": row["mid"]})
        if not context or not self._allowed(self._resolve(row), context):
            return False
        return self.store.record_delivery(self.owner(), row["id"], row["revision"], receipt["sid"], receipt["text"], receipt["receipt_id"])

    def capture(self, message):
        if not self.enabled() or not isinstance(message, dict):
            return False, False
        try:
            body = message.get("processed_plain_text")
            if parse_command(body):
                return self.store.command(message, self.owner()), False
            if not self.bot.config.advice.auto_feedback or not isinstance(body, str) or PRIVATE.search(body):
                return False, False
            target = target_for(body, [row for row in self.store.feedback_items(self.owner(), body,
                archive_enabled=getattr(self.bot.config.advice, "archive_enabled", False)) if self._resolve(row)])
            if target is None:
                return False, False
            return False, bool(self.store.enqueue(message, self.owner(), target["id"], target["revision"]))
        except (ValueError, TypeError, KeyError):
            return False, False

    async def process(self):
        if not self.enabled() or not self.bot.config.advice.auto_feedback or self._lock.locked():
            return
        async with self._lock:
            b, owner, cfg = self.bot, self.owner(), self.bot.config.advice
            claim = self.store.claim(owner, day_start=day_start(b.config.general.timezone), interval_seconds=cfg.interval_seconds,
                                     daily_limit=cfg.max_daily_batches, lease_seconds=min(600, max(120, int(b.config.unified_mind.model_timeout_seconds*2)+30)))
            if not claim:
                self.last_status = "waiting"
                return
            signature = self.signature()
            def valid():
                return bool(self.enabled() and self.bot.config.advice.auto_feedback and self.signature() == signature
                            and self.store.valid(owner, claim) and self._resolve(claim["item"]))
            exhausted = False
            def reserve():
                nonlocal exhausted
                allowed = b._self_state.reserve_thought(now=time.time(), day_start=day_start(b.config.general.timezone),
                    min_interval_seconds=0, daily_limit=b.config.unified_mind.max_daily_model_calls,
                    reserve_for_reply=b.config.unified_mind.reserved_reply_calls, purpose="background")
                exhausted = not allowed
                return allowed
            try:
                result = await understand_feedback(claim["text"], claim["item"], generate=b.ctx.llm.generate,
                    reserve=reserve, valid=valid, task_name=b.config.brain.model_task, timeout=b.config.unified_mind.model_timeout_seconds)
                if result and valid():
                    self.store.finish(owner, claim, result["outcome"], result["quote"])
                    self.last_status = "updated"
                else:
                    self.store.finish(owner, claim, retry=exhausted)
                    self.last_status = "budget" if exhausted else "unconfirmed"
            except asyncio.CancelledError:
                self.store.finish(owner, claim, retry=True)
                raise
            except Exception:
                self.store.finish(owner, claim, retry=True)
                self.last_status = "feedback_failed"

    def records(self, query=""):
        if not self.enabled():
            return []
        labels = {"tried": "本人报告尝试过，尚未确认效果", "not_tried": "本人报告尚未尝试", "helpful": "本人报告尝试后有帮助", "unhelpful": "本人报告尝试后没有帮助"}
        records = []
        terms = _terms(query)
        for row in self.store.recall(self.owner(), query, archive_enabled=getattr(self.bot.config.advice, "archive_enabled", False)):
            sources = self._resolve(row)
            if row["status"] != "active" or row["outcome"] not in labels or not sources:
                continue
            sensitivity = max(["personal"]+[r["sensitivity"] for r in sources], key={"public": 0, "personal": 1, "unknown": 2, "confidential": 3}.get)
            feedback = row["feedback"][:160] if row.get("feedback_kind") == "literal_owner_report" else "本人完整指令确认"
            text = "先前实际送达的建议（"+row["topic"]+"）："+row["advice"]+"；"+labels[row["outcome"]]+"。反馈依据："+feedback+"。仅本次本人自述，不能泛化为客观疗效或普遍结论。"
            records.append({"id": "advice:result:"+digest(row["id"]), "source": "advice", "kind": "suggestion_outcome", "derived": True,
                "scope": "private:"+self.owner(), "subject_id": self.owner(), "sensitivity": sensitivity, "evidence_status": "owner_reported",
                "text": text, "sources": row["source_refs"], "feedback_kind": row.get("feedback_kind", ""), "revision": row["revision"],
                "timestamp": row["updated"], "score": sum(term in text.casefold() for term in terms)})
        return sorted(records, key=lambda r: (-r["score"], -r["timestamp"], r["id"]))[:12]

    def hint(self, kwargs):
        if not self.trusted(kwargs):
            return ""
        return ("[MaiLivingWorld:advice]\n建议效果以本人明确反馈为准，沉默不代表尝试或有效；"
                "相关记录若表明先前方法无用，应承认并换个思路，不无视反馈反复劝同一办法；本人明确想重试时按当前意愿讨论。"
                "需要登记建议时用living_world_advice，先sources取允许表达的讨论依据，再prepare；来源不保证建议必有效。"
                "不替对方接受、执行或确认结果。")
