"""Natural owner conversation continuity; background learning never sends a message."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import re
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from .continuity_store import ContinuityStore
from .continuity_model import understand_continuity, continuity_style_hint
from .mcp_runtime import day_start, digest
from .owner import extract_sender, normalize_qq
from .mind import PRIVATE
from .unified_memory import _terms, _sensitivity

_RETURNS = {"我回来了", "回来了", "我忙完了", "忙完了", "我睡醒了", "睡醒了"}
_TOPIC = re.compile(r"我|明天|后天|最近|下周|这周|今天|考试|面试|担心|难过|不想.{0,8}提|已经|终于|完了|结束|解决了|没事了|取消")


def _short(text):
    if not isinstance(text, str) or len(text) > 160 or any(ord(c) < 32 for c in text):
        return ""
    return text.strip().rstrip("。.!！～~")


def contact_command(text, *, now, zone, busy_hours=3, rest_hours=8):
    """Only complete direct statements; a quoted fragment never becomes consent."""
    text = _short(text)
    if not text:
        return None
    if text in _RETURNS:
        return {"action": "contact_resume", "passive": True}
    if text in {"/近况 恢复联系", "可以主动联系我了", "可以主动找我了"}:
        return {"action": "contact_resume", "passive": False}
    if text in {"/近况 暂停联系", "暂时不要主动联系我", "先别主动联系我", "暂时别主动找我", "不要再主动联系我"}:
        return {"action": "contact_pause", "value": "manual", "until": 0}
    if text in {"今天不要主动联系我", "今天别主动找我"}:
        local = datetime.fromtimestamp(now, ZoneInfo(zone))
        until = (local+timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return {"action": "contact_pause", "value": "manual", "until": until}
    match = re.fullmatch(r"/近况 暂停联系 ([1-9][0-9]?)小时", text)
    if match and int(match[1]) <= 72:
        return {"action": "contact_pause", "value": "manual", "until": now+int(match[1])*3600}
    if text in {"先去忙", "我先去忙", "我要去忙", "我先去忙了", "我要去忙了", "我去忙了", "先忙了", "我在忙，晚点聊", "我先忙一会儿"}:
        return {"action": "contact_pause", "value": "busy", "until": now+busy_hours*3600}
    if text in {"我先去睡了", "我要睡了", "我去睡了", "睡觉了", "晚安"}:
        return {"action": "contact_pause", "value": "rest", "until": now+rest_hours*3600}
    modes = {
        "listen": {"/近况 倾听", "先听我说就好", "我只想吐槽一下", "先别给建议", "别急着给建议", "只要听我说就好",
                   "我有点难过", "我今天有点难过", "我心情不太好", "我有点烦", "我想吐槽一下"},
        "advice": {"/近况 建议", "帮我想想办法", "给我一些建议", "帮我想个办法"},
        "normal": {"/近况 正常", "正常聊天吧", "随便聊聊", "不用一直安慰我"},
    }
    for mode, phrases in modes.items():
        if text in phrases:
            return {"action": "support", "value": mode, "until": now+7200 if mode != "normal" else 0}
    match = re.fullmatch(r"/近况 忘记 (continuity:thread:[0-9a-f]{64}) ([1-9][0-9]{0,8})", text)
    if match:
        return {"action": "forget", "thread_id": match[1], "expected_revision": int(match[2])}
    return None


class ContinuityRuntime:
    def __init__(self, bot):
        self.bot = bot
        self.store = ContinuityStore(Path(bot.ctx.paths.data_dir)/"continuity.sqlite3", **bot._ledger_storage_options())
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
        return bool(b.config.plugin.enabled and b.config.continuity.enabled and self.owner())

    def memory_enabled(self):
        return bool(self.enabled() and self.bot._unified_active())

    def learning_enabled(self):
        return bool(self.memory_enabled() and self.bot.config.continuity.auto_topics)

    def signature(self):
        b = self.bot
        return digest([self._generation, self.owner(), b.config.plugin.enabled,
                       b.config.continuity.model_dump(), b.config.unified_mind.model_dump(), b.config.general.timezone])

    def capture(self, message, *, now=None):
        if not self.enabled() or not isinstance(message, dict):
            return False, False
        owner, sender = self.owner(), extract_sender(message)
        if (not sender or sender.kind != "private" or sender.user_id != owner
                or not sender.stream_id or not sender.message_id or str(message.get("account_id") or "") == owner):
            return False, False
        parts = message.get("raw_message")
        if (isinstance(parts, list) and any(not isinstance(p, dict) or p.get("type") != "text" for p in parts)
                or any(message.get(k) for k in ("is_picture", "is_voice", "is_emoji"))):
            return False, False
        now = time.time() if now is None else now
        text = message.get("processed_plain_text")
        cfg = self.bot.config.continuity
        command = contact_command(text, now=now, zone=self.bot.config.general.timezone,
                                  busy_hours=cfg.busy_hours, rest_hours=cfg.rest_hours)
        before = self.store.state(owner, now=now)
        if command:
            command = dict(command)
            action = command.pop("action")
            passive = command.pop("passive", False)
            if action == "forget":
                self.store.forget_thread(message, owner_id=owner, now=now, **command)
            elif action != "contact_resume" or not passive or before["contact"]["mode"] in {"normal", "busy", "rest"}:
                receipt = self.store.command(message, owner_id=owner, action=action, now=now, with_receipt=True, **command)
                if action == "contact_resume" and not passive and receipt["new_receipt"]:
                    experience = self.bot._ensure_experience()
                    if experience:
                        experience.resume(owner)
            return self.store.state(owner, now=now)["revision"] != before["revision"], False
        if (not self.learning_enabled() or message.get("is_command") or not isinstance(text, str)
                or not 3 <= len(text) <= 600 or text.startswith("/") or PRIVATE.search(text)
                or not _TOPIC.search(text)):
            return False, False
        ref = {"id": "qq-receipt:"+digest([sender.stream_id, sender.message_id]),
               "digest": hashlib.sha256(text.encode()).hexdigest(), "scope": "private:"+owner,
               "sensitivity": _sensitivity("private:"+owner, text)}
        queued = self.store.enqueue(message, owner_id=owner, source_ref=ref, now=now)
        if queued is None:
            self.last_status = "input_queue_full"
        return False, bool(queued)

    def contact_allowed(self, *, now=None):
        return not self.enabled() or not self.store.state(self.owner(), now=now)["contact"]["active"]

    def style(self, query="", *, support_override=None):
        if not self.enabled():
            return ""
        state = self.store.state(self.owner())
        hint = continuity_style_hint(support_override or state["support"]["effective_mode"], returning=_short(query) in _RETURNS)
        if state["contact"]["active"]:
            hint += "\n对方已明确希望暂缓主动联系；当前是对方发起的对话，照常回应，不追问去向或要求解释。"
        return hint

    async def process(self):
        if not self.learning_enabled() or self._lock.locked():
            return
        async with self._lock:
            b, owner = self.bot, self.owner()
            if not b._ensure_unified():
                return
            lease = min(600, max(120, int(b.config.unified_mind.model_timeout_seconds*2)+30))
            claim = self.store.claim(owner, limit=4, lease_seconds=lease)
            if not claim:
                self.last_status = "idle"
                return
            signature, revision = self.signature(), claim["base_revision"]
            def valid():
                state = self.store.state(owner)
                return (self.learning_enabled() and signature == self.signature()
                        and state["revision"] == revision and state["lease_token"] == claim["token"]
                        and state["lease_id"] == claim["id"] and state["lease_until"] > time.time()
                        and b._self_state.state()["owner_id"] == owner)
            def release(reason, failed=False):
                try:
                    if failed:
                        self.store.fail(claim["id"], claim["token"], owner_id=owner, reason=reason)
                    else:
                        self.store.defer(claim["id"], claim["token"], owner_id=owner, reason=reason,
                                         retry_at=time.time()+max(60, b.config.continuity.interval_seconds))
                except ValueError:
                    pass
            try:
                if not self.store.reserve_cycle(owner, day_start=day_start(b.config.general.timezone),
                    interval_seconds=b.config.continuity.interval_seconds, daily_limit=b.config.continuity.max_daily_batches):
                    self.last_status = "cycle_budget_or_cooldown"
                    release(self.last_status)
                    return
                exhausted = False
                def reserve():
                    nonlocal exhausted
                    allowed = b._self_state.reserve_thought(now=time.time(), day_start=day_start(b.config.general.timezone),
                        min_interval_seconds=0, daily_limit=b.config.unified_mind.max_daily_model_calls,
                        reserve_for_reply=getattr(b.config.unified_mind, "reserved_reply_calls", 0), purpose="background")
                    exhausted = not allowed
                    return allowed
                terms = _terms(" ".join(row["text"] for row in claim["inputs"]))
                threads = self.store.threads(owner, limit=200)
                threads.sort(key=lambda row: (-sum(term in self._searchable(row) for term in terms),
                                              -row["updated"], row["id"]))
                proposal = await understand_continuity(claim["inputs"], threads[:20],
                    generate=b.ctx.llm.generate, reserve=reserve, valid=valid, task_name=b.config.brain.model_task,
                    timeout=b.config.unified_mind.model_timeout_seconds)
                if proposal is None or not valid():
                    self.last_status = "model_budget" if exhausted else "context_changed" if not valid() else "review_failed"
                    release(self.last_status, failed=self.last_status == "review_failed")
                    return
                self.store.complete(claim["id"], claim["token"], proposal, owner_id=owner, expected_revision=revision)
                self.last_status = "updated" if proposal["updates"] else "no_pending_topic"
            except asyncio.CancelledError:
                release("interrupted")
                raise
            except Exception:
                self.last_status = "continuity_failed"
                release(self.last_status, failed=True)

    @staticmethod
    def _searchable(row):
        return (row["title"]+" "+row["summary"]+" "+" ".join(item["quote"] for item in row.get("evidence", []))).casefold()

    @staticmethod
    def _record(row):
        if not row or row.get("status") == "dismissed" or row.get("forgotten"):
            return None
        quotes = []
        evidence = row.get("evidence", [])
        selected = [evidence[-1]] if evidence else []
        if len(evidence) > 1 and evidence[0] != evidence[-1]:
            selected.append(evidence[0])
        for evidence in selected:
            stamp = datetime.fromtimestamp(evidence["timestamp"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            quotes.append("收到于"+stamp+"："+evidence["quote"])
        if not quotes:
            return None
        return {"id": row["id"], "source": "continuity", "kind": row["kind"],
                "scope": row["scope"], "subject_id": row["owner_id"],
                "text": ("主人私聊原话（自述；新话可更正旧话，相对日期基于当时，不代表已发生或授权提醒）；"
                         + ("整理状态：已结束（自述）。" if row["status"] == "resolved" else "") + "；".join(quotes)),
                "timestamp": row["updated"], "revision": row["revision"], "sensitivity": row["sensitivity"],
                "evidence_status": "owner_reported", "origin_key": row["evidence"][0]["id"]}

    def recall(self, query="", *, limit=3):
        if not self.memory_enabled():
            return []
        returning = _short(query) in _RETURNS
        terms = _terms(query)
        ranked = []
        for row in self.store.threads(self.owner(), limit=200):
            matches = sum(term in self._searchable(row) for term in terms)
            if not matches and not returning:
                continue
            record = self._record(row)
            if record:
                ranked.append((matches, row["updated"], record))
        ranked.sort(key=lambda item: (-item[0], -item[1], item[2]["id"]))
        return [item[2] for item in ranked[:1 if returning else limit]]

    def resolve(self, ids):
        if not self.memory_enabled():
            return {}
        rows = [self._record(self.store.thread(ident, owner_id=self.owner())) for ident in ids]
        return {row["id"]: row for row in rows if row}

    def learning_candidates(self):
        if not self.memory_enabled():
            return []
        return [record for row in self.store.threads(self.owner(), limit=3) if (record := self._record(row))]

    def inspect(self, kwargs, *, status="open", query="", offset=0):
        runtime = self.bot._ensure_unified()
        owner, request = self.owner(), dict(kwargs)
        if "message" in request:
            sender = extract_sender(request["message"])
            if not sender or sender.kind != "private" or sender.user_id != owner or not sender.message_id or not sender.stream_id:
                return {"success": False, "code": "owner_private_required"}
            request.update(session_id=sender.stream_id, reply_message_id=sender.message_id)
        context = runtime.context(request) if runtime and self.enabled() else None
        if not context or context["scope"] != "private:"+owner or context["speaker_id"] != owner:
            return {"success": False, "code": "owner_private_required"}
        if (status not in {"open", "resolved", "dismissed"} or not isinstance(query, str) or len(query) > 120
                or type(offset) is not int or not 0 <= offset <= 200):
            return {"success": False, "code": "invalid_status"}
        state = self.store.state(owner)
        threads = [row for row in self.store.threads(owner, limit=200, status=status)
                   if not query or query.casefold() in self._searchable(row)]
        return {"success": True, "data": {"contact": state["contact"], "support": state["support"],
            "processing": {key: value for key, value in self.store.status(owner).items()
                           if key in {"queued_inputs", "input_capacity", "queue_full"}},
            "threads": [{key: row[key] for key in ("id", "title", "summary", "kind", "status", "revision")}
                        for row in threads[offset:offset+20]],
            "has_more": offset+20 < len(threads), "total": len(threads),
            "instructions": "只读近况，基于主人自述，不是已核实经历或提醒承诺。忘记需主人完整私聊指令 /近况 忘记 <id> <revision>。"}}
