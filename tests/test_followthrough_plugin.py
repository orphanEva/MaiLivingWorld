"""Actual plugin hooks, MCP task binding and proactive gates with offline Host/transport."""
from __future__ import annotations

import asyncio
import copy
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from MaiLivingWorld.tests.test_runtime import FakeContext, plugin
from MaiLivingWorld.tests.test_mcp_plugin import OWNER, OTHER, GROUP, TASK, OfflineMCP, message
from MaiLivingWorld import mcp_client
from MaiLivingWorld.delivery import verify_owner_delivery
from MaiLivingWorld.store import Store


class FollowthroughPluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.transport = patch.object(mcp_client, "HTTPMCPClient", side_effect=lambda *a, **k: OfflineMCP("cap"))
        self.transport.start()
        self.bot = self.make_bot()

    def make_bot(self):
        b = plugin.MaiLivingWorld()
        b.ctx = FakeContext()
        b.ctx.paths = SimpleNamespace(data_dir=Path(self.temp.name))
        b.config = plugin.Config()
        b.config.owner.enabled = b.config.unified_mind.enabled = b.config.followthrough.enabled = True
        b.config.owner.qq = OWNER
        b.config.unified_mind.mode = "active"
        b.config.unified_mind.min_interval_seconds = 0
        b.config.mcp.enabled = b.config.mcp_cap.enabled = b.config.mcp.allow_writes = True
        b.config.brain_memory.private_users = [OTHER]
        b.config.brain_memory.group_ids = [GROUP]
        b._store = Store(Path(self.temp.name)/"living_world.sqlite3")
        return b

    async def asyncTearDown(self):
        await self.bot.on_unload()
        self.transport.stop()
        self.temp.cleanup()

    async def capture(self, **kw):
        event = message(**kw)
        await self.bot.capture_owner(message=event)
        await self.bot.capture_unified(message=event)
        return event

    async def draft(self):
        event = await self.capture()
        result = await self.bot.mcp_call("cap", "jm_start_download", {"jm_id": "123"}, message=event)
        self.assertTrue(result["success"])
        proof = self.bot._followthrough.store.source("cap", TASK)
        self.assertEqual(proof["source_signature"], self.bot._mcp_source_signature("cap"))
        result = await self.bot.followthrough("watch", "cap", TASK, message=event)
        self.assertTrue(result["success"])
        return result

    async def sent(self, text, mid="out1", **kw):
        sent = kw.pop("sent", True)
        event = message(uid="99999999", mid=mid, text=text, **kw)
        return await self.bot.followthrough_send_feedback(message=event, sent=sent)

    async def ready(self):
        result = await self.draft()
        await self.sent(result["delivery_text"])
        await self.bot._followthrough.tick()
        row = self.bot._followthrough.due()
        self.assertIsNotNone(row)
        self.assertEqual(row["result"], "succeeded")
        return row

    def proactive(self):
        b = self.bot
        b.config.general.enabled = True
        b.config.general.dry_run = False
        b.config.audience.private_users = [OWNER]
        b.config.audience.quiet_start = b.config.audience.quiet_end = "00:00"
        b.config.audience.history_gate = False
        return datetime.now(ZoneInfo("Asia/Shanghai"))

    async def test_real_task_promise_poll_report_and_restart(self):
        result = await self.draft()
        row_id = result["data"]["id"]
        rt = self.bot._followthrough
        await rt.tick()
        self.assertEqual(len(self.bot._mcp_client("cap").calls), 1)
        for text, options in ((result["delivery_text"], {"sent": False}),
                              ("她说："+result["delivery_text"], {}),
                              (result["delivery_text"], {"group": GROUP}),
                              (result["delivery_text"], {"sid": "other"})):
            await self.sent(text, **options)
            self.assertEqual(rt.store.get(OWNER, row_id)["status"], "draft")
        await self.sent(result["delivery_text"])
        self.assertEqual(rt.store.get(OWNER, row_id)["status"], "active")
        await self.bot.on_unload()
        self.bot = self.make_bot()
        await self.capture(mid="m2", sid="new-session")
        self.bot.ctx.chat.get_stream_by_user_id.return_value = {"stream_id": "new-session"}
        await self.bot._followthrough.tick()
        self.assertEqual(self.bot._mcp_client("cap").calls, [("jm_get_download_status", {"task_id": TASK})])
        report = await self.bot.followthrough("report", session_id="new-session")
        self.assertTrue(report["success"])
        await self.sent(report["delivery_text"], mid="out2", sid="new-session", sent=False)
        self.assertEqual(self.bot._followthrough.store.get(OWNER, row_id)["status"], "ready")
        await self.sent(report["delivery_text"], mid="out2", sid="new-session")
        self.assertEqual(self.bot._followthrough.store.get(OWNER, row_id)["status"], "reported")
        await self.sent(report["delivery_text"], mid="out2", sid="new-session")
        self.assertEqual(len(self.bot._followthrough.store._read("SELECT id FROM followthrough_receipts")), 2)
        self.assertNotIn("/srv/private", json.dumps(self.bot._followthrough.store.items(OWNER)))

    async def test_delivery_rejects_non_bot_ambiguous_destination_and_late_owner_change(self):
        await self.capture()
        receipt = {"sent": True, "message": message(uid="99999999", mid="sent", text="合成回执")}
        self.assertIsNotNone(await verify_owner_delivery(self.bot, receipt))
        for update in ({"message_id": ""}, {"account_id": OWNER}, {"platform": "other"},
                       {"raw_message": [{"type": "forward", "text": "合成回执"}]},
                       {"message_info": {"user_info": {"user_id": OWNER}}}):
            bad = copy.deepcopy(receipt)
            bad["message"].update(update)
            self.assertIsNone(await verify_owner_delivery(self.bot, bad))
        for sent in (False, "true", 1, None):
            self.assertIsNone(await verify_owner_delivery(self.bot, {**receipt, "sent": sent}))
        async def changed(*a, **k):
            self.bot.config.owner.qq = OTHER
            return {"stream_id": "private-one"}
        self.bot.ctx.chat.get_stream_by_user_id.side_effect = changed
        self.assertIsNone(await verify_owner_delivery(self.bot, receipt))

    async def test_source_snapshot_predates_await_and_old_service_cannot_be_adopted(self):
        await self.capture()
        original = self.bot._mcp_source_signature("cap")
        client = self.bot._mcp_client("cap")
        async def change():
            self.bot.config.mcp_cap.url = "http://different-service:8000/mcp"
        client.on_call = change
        response = await self.bot.mcp_call("cap", "jm_start_download", {"jm_id": "123"}, session_id="private-one")
        self.assertFalse(response["success"])
        proof = self.bot._followthrough.store.source("cap", TASK)
        self.assertEqual(proof["source_signature"], original)
        self.assertNotEqual(proof["source_signature"], self.bot._mcp_source_signature("cap"))
        self.assertFalse((await self.bot.followthrough("watch", "cap", TASK, session_id="private-one"))["success"])

    async def test_changed_token_refuses_cached_client_and_invalidates_followup(self):
        with patch.dict(os.environ, {"MLW_CAP_MCP_TOKEN": "synthetic-old-token"}):
            result = await self.draft()
            client = self.bot._mcp_client("cap")
            os.environ["MLW_CAP_MCP_TOKEN"] = "synthetic-new-token"
            await self.sent(result["delivery_text"])
            self.assertEqual(self.bot._followthrough.store.get(OWNER, result["data"]["id"])["status"], "draft")
            with self.assertRaises(mcp_client.MCPError):
                self.bot._mcp_client("cap")
            self.assertEqual(len(client.calls), 1)
            self.assertNotIn("synthetic-", json.dumps(self.bot._followthrough.store.source("cap", TASK)))

    async def test_old_unproven_task_and_old_private_context_rejected(self):
        old = await self.capture()
        self.bot._ensure_tool_store().bind_task("cap", TASK, "private:"+OWNER, OWNER, "jm_start_download")
        self.assertFalse((await self.bot.followthrough("watch", "cap", TASK, message=old))["success"])
        await self.capture(mid="new")
        for request in ({"message": old}, {"message": {}}, {"message": message(uid=OTHER)},
                        {"message": message(group=GROUP)}, {"reply_message_id": "m1"}):
            response = await self.bot.followthrough("list", session_id="private-one", **request)
            self.assertFalse(response["success"])

    async def test_proactive_report_uses_gates_and_only_receipt_finishes(self):
        row = await self.ready()
        now = self.proactive()
        await self.bot._try_target("private", OWNER, now)
        call = self.bot.ctx.maisaka.proactive.trigger.call_args
        self.assertIsNotNone(call)
        report = self.bot._followthrough.store.get(OWNER, row["id"])
        self.assertIn(report["delivery_text"], call.args[1])
        self.assertNotIn(TASK, call.args[1])
        self.assertEqual(report["status"], "ready")
        self.bot.ctx.llm.generate.assert_not_called()
        await self.sent(report["delivery_text"], mid="report")
        self.assertEqual(self.bot._followthrough.store.get(OWNER, row["id"])["status"], "reported")

    async def test_proactive_off_dry_run_quiet_and_new_activity_keep_ready(self):
        row = await self.ready()
        now = self.proactive()
        for name in ("off", "dry", "quiet", "busy", "not_whitelisted"):
            self.bot._last_decision.clear()
            self.bot.config.general.enabled = name != "off"
            self.bot.config.general.dry_run = name == "dry"
            self.bot.config.audience.private_users = [] if name == "not_whitelisted" else [OWNER]
            self.bot.config.owner.add_to_private_targets = False
            self.bot.ctx.message.get_by_time_in_chat.return_value = [{}] if name == "busy" else []
            with patch.object(plugin, "is_quiet", return_value=name == "quiet"):
                await self.bot._try_target("private", OWNER, now)
            self.bot.ctx.maisaka.proactive.trigger.assert_not_called()
            current = self.bot._followthrough.store.get(OWNER, row["id"])
            self.assertEqual(current["status"], "ready")
            self.assertEqual(current["report_until"], 0)

    async def test_pause_and_failed_host_request_do_not_mark_reported(self):
        row = await self.ready()
        now = self.proactive()
        self.bot.config.continuity.enabled = True
        self.bot.config.continuity.auto_topics = False
        await self.capture(mid="busy", text="先去忙")
        await self.bot._try_target("private", OWNER, now)
        self.bot.ctx.maisaka.proactive.trigger.assert_not_called()
        await self.capture(mid="back", text="我回来了")
        self.bot.ctx.maisaka.proactive.trigger.return_value = {"success": False}
        await self.bot._try_target("private", OWNER, now)
        self.assertEqual(self.bot.ctx.maisaka.proactive.trigger.await_count, 1)
        self.assertEqual(self.bot._followthrough.store.get(OWNER, row["id"])["status"], "ready")

    async def test_ready_result_becomes_private_reply_tool_hint_when_proactive_disabled(self):
        await self.ready()
        self.bot.config.general.enabled = False
        await self.capture(mid="next", text="我回来了")
        result = await self.bot.brain_reply_hint(session_id="private-one", reply_message_id="next",
            messages=[{"role": "system", "content": "test"}])
        self.assertIn("living_world_followthrough", json.dumps(result))
        planner = await self.bot.owner_planner_prompt(session_id="private-one", reply_message_id="next",
            messages=[{"role": "system", "content": "test"}])
        self.assertIn("living_world_followthrough", json.dumps(planner))
        await self.capture(mid="other", uid=OTHER, sid="other-session")
        result = await self.bot.brain_reply_hint(session_id="other-session", reply_message_id="other",
            messages=[{"role": "system", "content": "test"}])
        self.assertNotIn("living_world_followthrough", json.dumps(result))
        planner = await self.bot.owner_planner_prompt(session_id="other-session", reply_message_id="other",
            messages=[{"role": "system", "content": "test"}])
        self.assertNotIn("living_world_followthrough", json.dumps(planner))

    async def test_empty_hints_never_modify_invalid_or_missing_reply_context(self):
        self.bot.config.habits.enabled = True
        self.bot.config.recollection.enabled = True
        self.bot.config.attention.enabled = True
        for options in ({}, {"session_id": "missing"}, {"session_id": "group-one"},
                        {"session_id": "private-one", "reply_message_id": "old"}):
            result = await self.bot.brain_reply_hint(**options, messages=[{"role": "system", "content": "original"}])
            self.assertEqual(result, {"action": "continue"})
        self.bot.ctx.llm.generate.assert_not_called()
