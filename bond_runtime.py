"""Owner-authored relationship preferences and memories, shared across sessions.

Only complete commands in real owner private messages change this ledger. The
model can inspect it, but cannot manufacture a confirmation or change identity.
Memories take the same audience/expression path as all other original evidence.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
import time
from zoneinfo import ZoneInfo

from .bond_store import BondStore
from .companion_style import companion_style_hint, circadian_hint, _preferences
from .owner import extract_sender, normalize_qq
from .unified_memory import _sensitivity, _terms

_ADDRESS = r"[\w\u3400-\u9fff·]{1,24}"
_TOKEN = r"[a-z0-9:_-]{16,100}"


def parse_bond_command(text):
    """Conservative full-message grammar, never extraction from quoted prose."""
    if not isinstance(text, str) or not text.strip() or len(text) > 460:
        return None
    text = text.strip()
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        return None
    for pattern, action in (
        (rf"/相处 称呼 ({_ADDRESS})", "set_address"),
        (r"(?:以后)?叫我([\w\u3400-\u9fff·]{1,24}?)(?:吧)?", "set_address"),
        (rf"/相处 不叫 ({_ADDRESS})", "ban_address"),
        (rf"(?:以后)?(?:别|不要)(?:再)?叫我({_ADDRESS})", "ban_address"),
    ):
        match = re.fullmatch(pattern, text)
        if match:
            value = match[1]
            if action == "set_address" and text.startswith("/相处") and value == "默认":
                value = ""
            return {"action": action, "value": value}
    affection = {"默认": "default", "自然": "allow", "暂停": "avoid", "克制": "avoid"}
    if text.startswith("/相处 亲昵 ") and text[7:] in affection:
        return {"action": "set_affection", "value": affection[text[7:]]}
    if text in {"别撒娇", "不要撒娇", "以后别撒娇", "以后不要撒娇", "别再撒娇"}:
        return {"action": "set_affection", "value": "avoid"}
    if text in {"可以撒娇", "以后可以撒娇"}:
        return {"action": "set_affection", "value": "allow"}
    for prefix, action, limit in (("/相处 边界 ", "add_boundary", 120),
                                  ("/相处 记住 ", "memory", 400)):
        if text.startswith(prefix) and 0 < len(text[len(prefix):].strip()) <= limit:
            return {"action": action, "value": text[len(prefix):].strip()}
    match = re.fullmatch(rf"/相处 删除边界 ({_TOKEN})", text)
    if match:
        return {"action": "remove_boundary", "value": match[1]}
    match = re.fullmatch(r"/相处 纪念日 (\d{4}-\d{2}-\d{2}) (.{1,120})", text)
    if match:
        try:
            datetime.strptime(match[1], "%Y-%m-%d")
        except ValueError:
            return None
        return {"action": "anniversary", "value": match[2].strip(), "occurred_on": match[1]}
    match = re.fullmatch(rf"/相处 忘记 ({_TOKEN}) ([1-9][0-9]{{0,8}})", text)
    if match:
        return {"action": "retire", "moment_id": match[1], "expected_revision": int(match[2])}
    return None


class BondRuntime:
    def __init__(self, bot):
        self.bot = bot
        self.store = BondStore(Path(bot.ctx.paths.data_dir) / "bond.sqlite3", **bot._ledger_storage_options())

    def close(self):
        self.store.close()

    def owner(self):
        b = self.bot
        return normalize_qq(b.config.owner.qq) if b.config.owner.enabled else ""

    def enabled(self):
        b = self.bot
        return bool(b.config.plugin.enabled and b.config.bond.enabled and b._unified_active() and self.owner())

    def capture(self, message):
        if not self.enabled() or not isinstance(message, dict):
            return False
        owner, sender = self.owner(), extract_sender(message)
        if (not sender or sender.kind != "private" or sender.user_id != owner
                or not sender.stream_id or not sender.message_id
                or str(message.get("account_id") or "") == owner):
            return False
        # Forwarded/media components cannot turn their displayed text into consent.
        raw = message.get("raw_message")
        if isinstance(raw, list) and any(not isinstance(p, dict) or p.get("type") != "text" for p in raw):
            return False
        command = parse_bond_command(message.get("processed_plain_text"))
        if command is None:
            return False
        previous = self.store.profile(owner)["revision"]
        action = command["action"]
        if action in {"memory", "anniversary"}:
            self.store.confirm_moment(message, owner_id=owner, summary=command["value"], kind=action,
                                      occurred_on=command.get("occurred_on", ""))
        elif action == "retire":
            self.store.retire_moment(message, owner_id=owner, moment_id=command["moment_id"],
                                     expected_revision=command["expected_revision"])
        else:
            self.store.command(message, owner_id=owner, action=action, value=command.get("value"))
        return self.store.profile(owner)["revision"] != previous

    def style(self):
        if not self.enabled():
            return ""
        profile = {**self.store.profile(self.owner()), "fallback_address": self.bot.config.owner.title}
        hint = companion_style_hint(profile, "private")
        if self.bot.config.bond.daily_rhythm:
            hint += "\n" + circadian_hint(self.bot.config.general.timezone, time.time())
        return hint

    def address(self):
        profile = {**self.store.profile(self.owner()), "fallback_address": self.bot.config.owner.title}
        return _preferences(profile)["current_address"] or None

    def _records(self):
        if not self.enabled():
            return []
        records = []
        for row in self.store.moments(self.owner(), limit=200):
            if row.get("status") != "active":
                continue
            text = ("主人亲自留下的" + ("纪念日" if row["kind"] == "anniversary" else "回忆自述")
                    + "（未独立核实，不补写共同经历）"
                    + ("，日期=" + row["occurred_on"] if row.get("occurred_on") else "") + "：" + row["summary"])
            records.append({"id": row["id"], "source": "bond", "kind": row["kind"],
                "scope": row["scope"], "subject_id": row["owner_id"], "text": text,
                "timestamp": row["updated"], "revision": row["revision"],
                "sensitivity": _sensitivity(row["scope"], text), "evidence_status": "owner_reported",
                "occurred_on": row.get("occurred_on", "")})
        return records

    def recall(self, query="", *, limit=4, now=None):
        terms = _terms(query)
        now = time.time() if now is None else now
        try:
            today = datetime.fromtimestamp(now, ZoneInfo(self.bot.config.general.timezone)).strftime("%m-%d")
        except (ValueError, KeyError, OverflowError):
            today = ""
        ranked = []
        for record in self._records():
            relevance = sum(term in record["text"].casefold() for term in terms)
            due = bool(today and record["kind"] == "anniversary" and record["occurred_on"][5:] == today
                       and record["occurred_on"] <= datetime.fromtimestamp(now, ZoneInfo(self.bot.config.general.timezone)).strftime("%Y-%m-%d"))
            # Avoid bringing unrelated private recollections into every greeting.
            if terms and not relevance and not due:
                continue
            if not terms and not due:
                continue
            ranked.append((relevance + (3 if due else 0), record))
        ranked.sort(key=lambda pair: (-pair[0], -pair[1]["timestamp"], pair[1]["id"]))
        return [record for _, record in ranked[:limit]]

    def resolve(self, ids):
        selected = set(ids)
        return {row["id"]: row for row in self._records() if row["id"] in selected}

    def learning_candidates(self):
        # Learning may inspect recent authored memories even when the current
        # conversation is unrelated; ordinary replies keep relevance filtering.
        return sorted(self._records(), key=lambda row: (-row["timestamp"], row["id"]))[:4]

    def inspect(self, kwargs, *, query="", offset=0, limit=20):
        runtime = self.bot._ensure_unified()
        owner = self.owner()
        request = dict(kwargs)
        if "message" in request:
            sender = extract_sender(request["message"])
            if (not sender or sender.kind != "private" or sender.user_id != owner
                    or not sender.stream_id or not sender.message_id):
                return {"success": False, "code": "owner_private_required"}
            request.update(session_id=sender.stream_id, reply_message_id=sender.message_id)
        context = runtime.context(request) if runtime and self.enabled() else None
        if not context or context["scope"] != "private:"+owner or context["speaker_id"] != owner:
            return {"success": False, "code": "owner_private_required"}
        if (not isinstance(query, str) or len(query) > 120 or type(offset) is not int or not 0 <= offset <= 200
                or type(limit) is not int or not 1 <= limit <= 20):
            return {"success": False, "code": "invalid_query"}
        profile = self.store.profile(owner)
        moments = [m for m in self.store.moments(owner, limit=200)
                   if m["status"] == "active" and (not query or query.casefold() in m["summary"].casefold())]
        return {"success": True, "data": {
            "profile": {k: profile[k] for k in ("preferred_address", "blocked_addresses", "affection", "boundaries", "revision")},
            "moments": [{k: m[k] for k in ("id", "summary", "kind", "occurred_on", "revision")}
                        for m in moments[offset:offset+limit]],
            "total": len(moments), "has_more": offset+limit < len(moments),
            "instructions": "仅可读取。更改需主人本人私聊发送完整相处指令；回忆是主人自述，不是已核实事实。"}}
