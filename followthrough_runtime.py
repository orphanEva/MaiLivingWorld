"""Follow existing owner jobs, then acknowledge only verified outbound receipts.

This module never submits/retries a job, sends QQ messages, or extracts promises
from free prose. Fixed standalone sentences provide an auditable delivery gate.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import re
import time

from .followthrough_store import FollowthroughStore, OPERATIONS, digest
from .mcp_policy import permitted
from .mcp_runtime import day_start
from .owner import extract_sender, normalize_qq


def task_result(provider, task_id, data):
    """Exact upstream contracts (cap feat/jm-mcp, pixiv codex/mcp-server).

    cap's task_registry exposes task_id/state; all three Pixiv Redis queues
    expose job_id/status. A successful RPC is not a successful background job.
    Error strings, paths, URLs and arbitrary result bodies never enter storage.
    """
    if not isinstance(data, dict):
        return "unknown"
    if provider == "cap":
        if data.get("task_id") != task_id or not isinstance(data.get("state"), str):
            return "unknown"
        return {"completed": "succeeded", "failed": "failed", "running": "pending"}.get(data.get("state"), "unknown")
    if provider == "pixiv":
        if data.get("job_id") != task_id or not isinstance(data.get("status"), str):
            return "unknown"
        return {"succeeded": "succeeded", "failed": "failed", "dead": "failed", "queued": "pending",
                "running": "pending", "finalizing": "pending", "retry_wait": "pending"}.get(data.get("status"), "unknown")
    return "unknown"


def safe_item(row):
    return {key: row[key] for key in ("id", "provider", "label", "status", "result", "revision", "polls")}


class FollowthroughRuntime:
    def __init__(self, bot):
        self.bot = bot
        self.store = FollowthroughStore(Path(bot.ctx.paths.data_dir)/"followthrough.sqlite3", **bot._ledger_storage_options())
        self._generation = 0
        self._lock = asyncio.Lock()
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
        b, owner = self.bot, self.owner()
        state = getattr(b, "_self_state", None)
        return bool(b.config.plugin.enabled and b.config.followthrough.enabled and b.config.mcp.enabled
                    and owner and state and state.state()["owner_id"] == owner)

    def signature(self):
        b = self.bot
        return digest([self._generation, self.owner(), b.config.plugin.enabled,
                       b.config.followthrough.model_dump(), b._mcp_generation,
                       b._mcp_signature("cap"), b._mcp_signature("pixiv")])

    def _activity(self):
        """Reuse current attention/Host message receipts; do not create chat history."""
        b, owner = self.bot, self.owner()
        attention = getattr(b, "_attention", None)
        active = bool(attention and attention.enabled())
        state = attention.store.state(owner) if active else {}
        unified = getattr(b, "_unified", None)
        contexts = getattr(unified, "_contexts", {})
        frontier = sorted((sid, mid) for sid, mid in getattr(unified, "_private", {}).items()
                          if contexts.get((sid, mid), {}).get("scope") == "private:"+owner)
        return (bool(state.get("engaged_until", 0) > time.time()),
                digest([active, state.get("latest_receipt", ""), state.get("engaged_until", 0),
                        getattr(unified, "_generation", 0), frontier]))

    def trusted(self, kwargs):
        if not self.enabled() or not isinstance(kwargs, dict):
            return None
        sender = extract_sender(kwargs.get("message"))
        if "message" in kwargs and (not sender or sender.kind != "private" or sender.user_id != self.owner()
                                     or not sender.stream_id or not sender.message_id):
            return None
        trusted = self.bot._mcp_context(kwargs)
        if not trusted:
            return None
        context, request = trusted
        owner = self.owner()
        if (context.get("scope") != "private:"+owner or context.get("speaker_id") != owner
                or not context.get("sid") or not context.get("mid")):
            return None
        return context, request

    def _binding(self, provider, task_id):
        if not self.enabled() or provider not in OPERATIONS or not isinstance(task_id, str) or not re.fullmatch(r"[0-9a-f]{32}", task_id):
            return None
        b, owner = self.bot, self.owner()
        server = b._mcp_server_config(provider)
        if not server or not server.enabled:
            return None
        task = b._ensure_tool_store().task(provider, task_id)
        proof = self.store.source(provider, task_id)
        if (not task or not proof or task["subject_id"] != owner or task["scope"] != "private:"+owner
                or proof["owner_id"] != owner or proof["scope"] != task["scope"]
                or proof["operation"] != task["operation"]
                or proof["source_signature"] != b._mcp_source_signature(provider)):
            return None
        operation, _, requirement = task["operation"].partition(":")
        context = {"scope": "private:"+owner, "speaker_id": owner}
        if (operation not in OPERATIONS[provider] or requirement not in {"", "account"}
                or not permitted(provider, operation, b.config.mcp, context, owner)
                or requirement == "account" and not b.config.mcp.allow_account_actions):
            return None
        return {"task": task, "source_signature": proof["source_signature"], "binding_digest": digest([task, proof])}

    def _current(self, row):
        binding = self._binding(row["provider"], row["task_id"])
        return bool(binding and binding["binding_digest"] == row["binding_digest"]
                    and binding["source_signature"] == row["source_signature"] and row["owner_id"] == self.owner())

    def capture(self, message):
        if not self.enabled() or not isinstance(message, dict):
            return False
        sender = extract_sender(message)
        if (not sender or sender.kind != "private" or sender.user_id != self.owner()
                or not sender.stream_id or not sender.message_id or str(message.get("account_id") or "") == self.owner()):
            return False
        parts = message.get("raw_message")
        if ((isinstance(parts, list) and any(not isinstance(part, dict) or part.get("type") != "text" for part in parts))
                or any(message.get(key) for key in ("is_picture", "is_voice", "is_emoji"))):
            return False
        text = message.get("processed_plain_text")
        match = re.fullmatch(r"/回告 取消 (followthrough:item:[0-9a-f]{64}) ([1-9][0-9]{0,8})", text.strip()) if isinstance(text, str) else None
        return bool(match and self.store.cancel(self.owner(), match[1], int(match[2])))

    async def tool(self, action, provider="", task_id="", item_id="", kwargs=None):
        trusted = self.trusted(kwargs or {})
        if not trusted:
            return {"success": False, "code": "owner_private_required"}
        owner, sid = self.owner(), trusted[0]["sid"]
        try:
            if action == "list":
                return {"success": True, "data": [safe_item(row) for row in self.store.items(owner)],
                        "instructions": "取消仅停止本插件跟进，不取消外部任务。主人可私聊 /回告 取消 <id> <revision>。"}
            if action == "watch":
                binding = self._binding(provider, task_id)
                if not binding:
                    return {"success": False, "code": "original_task_binding_required"}
                row = self.store.draft(owner, sid, provider, task_id, binding["task"]["operation"],
                                       binding["binding_digest"], binding["source_signature"])
                result = {"success": True, "data": safe_item(row)}
                if row["status"] == "draft":
                    result.update(delivery_text=row["promise_text"], instructions="如愿意跟进，请将 delivery_text 原样作为独立整条消息发送；仅真正发送成功后开始查询。草稿十分钟失效。")
                return result
            if action == "report":
                row = self.store.get(owner, item_id) if item_id else self.due(owner)
                report = self.prepare_report(owner, sid, row["id"], row["revision"]) if row else None
                if report:
                    return {"success": True, "data": {key: report[key] for key in ("id", "revision")},
                            "delivery_text": report["text"], "instructions": "将 delivery_text 原样作为独立整条消息发送；工具返回不代表已回告。"}
                return {"success": False, "code": "no_ready_result"}
            return {"success": False, "code": "invalid_action"}
        except (ValueError, TypeError):
            return {"success": False, "code": "invalid_followthrough_request"}

    async def sent(self, kwargs):
        if not self.enabled():
            return False
        from .delivery import verify_owner_delivery
        signature, owner = self.signature(), self.owner()
        receipt = await verify_owner_delivery(self.bot, kwargs)
        if not receipt or not self.enabled() or self.signature() != signature or receipt["owner"] != owner:
            return False
        row = self.store.match_delivery(owner, receipt["sid"], receipt["text"])
        if not row or not self._current(row):
            return False
        return self.store.record_delivery(owner, receipt["sid"], receipt["text"], receipt["receipt_id"],
                                          item_id=row["id"], expected_revision=row["revision"])

    def due(self, owner=None):
        owner = self.owner() if owner is None else owner
        if not self.enabled() or owner != self.owner():
            return None
        rows = sorted((row for row in self.store.items(owner) if row["status"] == "ready"
                       and row["report_until"] <= time.time()), key=lambda row: (row["updated"], row["id"]))
        return next((row for row in rows if self._current(row)), None)

    def prepare_report(self, owner, sid, item_id, expected_revision):
        if not self.enabled() or owner != self.owner():
            return None
        row = self.store.get(owner, item_id)
        if not row or not self._current(row):
            return None
        prepared = self.store.prepare_report(owner, sid, item_id, expected_revision)
        return ({"id": prepared["id"], "revision": prepared["revision"], "text": prepared["delivery_text"],
                 "owner": owner, "sid": sid, "signature": self.signature()} if prepared else None)

    def valid_report(self, owner, sid, item_id, expected_revision, signature=None):
        if not self.enabled() or owner != self.owner() or signature is not None and signature != self.signature():
            return False
        row = self.store.get(owner, item_id)
        return bool(row and self._current(row) and self.store.valid_report(owner, sid, item_id, expected_revision))

    async def tick(self):
        if not self.enabled() or self._lock.locked():
            return
        async with self._lock:
            b, owner = self.bot, self.owner()
            cfg = b.config.followthrough
            signature = self.signature()
            engaged, activity = self._activity()
            if engaged:
                self.last_status = "conversation"
                return
            # Retired services and revoked task permissions cannot spend another
            # service's polling allowance. The store selects under its owner lock;
            # valid() still checks the live binding after the claim and each await.
            eligible = [row["id"] for row in self.store.items(owner)
                        if row["status"] == "active" and self._current(row)]
            row = self.store.claim_poll(owner, day_start=day_start(b.config.general.timezone),
                daily_limit=min(12, cfg.max_daily_checks), interval_seconds=max(300, cfg.interval_seconds),
                eligible_ids=eligible,
                lease_seconds=max(120, min(3600, int(b.config.mcp.timeout_seconds)+30)))
            if not row:
                return
            def valid():
                return (self.enabled() and self.signature() == signature and self._current(row)
                        and self._activity() == (False, activity)
                        and self.store.valid_poll(owner, row["id"], row["lease_token"], row["revision"]))
            if not valid():
                self.last_status = "binding_unavailable"
                return
            provider, operation = row["provider"], row["operation"].partition(":")[0]
            tool = "jm_get_download_status" if provider == "cap" else "get_job"
            arguments = ({"task_id": row["task_id"]} if provider == "cap" else
                         {"job_id": row["task_id"], "queue": "media" if operation == "generate_ugoira_preview"
                          else "sync" if operation == "run_sync_source" else "archive"})
            context = {"scope": "private:"+owner, "speaker_id": owner, "sid": "internal-followthrough",
                       "mid": row["lease_token"], "query": ""}
            result = "unknown"
            try:
                b._ensure_tool_store()
                client = b._mcp_client(provider)
                response = await asyncio.wait_for(b._mcp_bridge.execute(provider, tool, arguments,
                    config=b.config.mcp, context=context, owner=owner, client=client, valid=valid,
                    timezone=b.config.general.timezone, record_observations=False),
                    max(1, float(b.config.mcp.timeout_seconds))+5)
                if valid() and isinstance(response, dict) and response.get("success") is True:
                    result = task_result(provider, row["task_id"], response.get("data"))
                self.last_status = result if valid() else "context_changed"
            except asyncio.CancelledError:
                self.last_status = "interrupted"
                raise
            except Exception:
                # No provider exception, raw result, path or credential is persisted.
                self.last_status = "query_unconfirmed"
            finally:
                if valid():
                    self.store.finish_poll(owner, row["id"], row["lease_token"], row["revision"], result,
                                           interval_seconds=max(300, cfg.interval_seconds))
