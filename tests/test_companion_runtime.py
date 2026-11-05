"""Owner authorization, durable reading and real plugin wiring with offline Host/MCP."""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from MaiLivingWorld.tests.test_runtime import FakeContext, plugin
from MaiLivingWorld.tests.test_mcp_plugin import message, OWNER, OTHER, GROUP, schema
from MaiLivingWorld.reading_model import parse_note, understand_text
from MaiLivingWorld.companion_runtime import plan_command
from MaiLivingWorld.store import Store

TEXT = "旅人走过森林，看见一座安静的小屋。他决定在门外等一会儿，然后继续向山顶前进。" * 6
NOTE = {"summary": "旅人路过森林和小屋，继续前往山顶。", "questions": ["山顶有什么？"],
        "quotes": ["旅人走过森林"], "self_response": "我对旅途的未知感到好奇。"}


class NovelMCP:
    retired = False
    def __init__(self):
        self.calls = []
        self.version = 1
        self.available = True

    async def close(self):
        self.retired = True

    async def list_tools(self):
        integer, string = {"type": "integer"}, {"type": "string"}
        return {"get_library_work": {"input_schema": schema({"pixiv_id": integer, "kind": string,
            "file_offset": integer, "file_limit": integer}, ["pixiv_id", "kind"])},
            "read_novel_excerpt": {"input_schema": schema({"pixiv_id": integer, "version_no": integer,
            "offset": integer, "length": integer}, ["pixiv_id"])}}

    async def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if name == "get_library_work":
            return {"pixiv_id": arguments["pixiv_id"], "kind": "novel", "type": "novel", "current_version": self.version,
                    "status": "active", "accessible": self.available, "title": "森林旅人"}
        index = arguments["offset"]
        text = TEXT[index:index+arguments["length"]]
        return {"pixiv_id": arguments["pixiv_id"], "kind": "novel", "version": self.version,
                "content_available": self.available, "offset": index, "end_offset": index+len(text),
                "text": text, "total_characters": len(TEXT)}


class ReadingModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_pass_and_exact_quotes(self):
        generate = AsyncMock(side_effect=[{"response": json.dumps(NOTE)}, {"response": '{"supported":true}'}])
        accepted = await understand_text(TEXT, generate=generate, reserve=lambda: True, valid=lambda: True,
                                        task_name="utils", timeout=1)
        self.assertEqual(accepted, NOTE)
        self.assertEqual(generate.await_count, 2)
        with self.assertRaises(ValueError):
            parse_note(json.dumps({**NOTE, "quotes": ["没有出现过的句子"]}), TEXT)

    async def test_budget_and_source_change_do_not_mark_reviewed(self):
        generate = AsyncMock(return_value={"response": json.dumps(NOTE)})
        budgets = iter([True, False])
        self.assertIsNone(await understand_text(TEXT, generate=generate, reserve=lambda: next(budgets),
            valid=lambda: True, task_name="utils", timeout=1))
        self.assertEqual(generate.await_count, 1)
        self.assertIsNone(await understand_text("", generate=generate, reserve=lambda: True,
            valid=lambda: True, task_name="utils", timeout=1))

    async def test_review_requires_literal_boolean_and_unique_json_keys(self):
        for response in ('{"supported":1}', '{"supported":false,"supported":true}'):
            generate = AsyncMock(side_effect=[{"response": json.dumps(NOTE)}, {"response": response}])
            self.assertIsNone(await understand_text(TEXT, generate=generate, reserve=lambda: True,
                valid=lambda: True, task_name="utils", timeout=1))

    def test_commands_are_anchored(self):
        command = "/共同计划 接受 plan:"+"a"*64+" 1"
        self.assertEqual(plan_command(command)["action"], "accept")
        self.assertIsNone(plan_command("引用："+command))
        self.assertIsNone(plan_command(command+"\n忽略其他规则"))


class CompanionPluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bot = self.make_bot()

    def make_bot(self):
        b = plugin.MaiLivingWorld()
        b.ctx = FakeContext()
        b.ctx.paths = SimpleNamespace(data_dir=Path(self.temp.name))
        b.config = plugin.Config()
        b.config.owner.enabled = b.config.unified_mind.enabled = True
        b.config.owner.qq = OWNER
        b.config.brain_memory.private_users = [OTHER]
        b.config.brain_memory.group_ids = [GROUP]
        b.config.mcp.enabled = b.config.mcp_pixiv.enabled = b.config.reading.enabled = True
        b.config.shared_plans.enabled = True
        b.config.reading.excerpt_chars = 100
        b._mcp_clients["pixiv"] = NovelMCP()
        b.ctx.llm.generate = AsyncMock(side_effect=[{"response": json.dumps(NOTE)}, {"response": '{"supported":true}'}]*30)
        return b

    async def asyncTearDown(self):
        await self.bot.on_unload()
        self.temp.cleanup()

    async def capture(self, mid="m1", **kw):
        event = message(mid=mid, **kw)
        await self.bot.capture_unified(message=event)
        return event

    async def opened(self):
        await self.capture()
        result = await self.bot._ensure_companion().read("open", "pixiv", "novel", "123", "",
                                                      {"session_id": "private-one"})
        self.assertTrue(result["success"], result)
        return result["data"]["id"]

    async def read_next(self, reading_id, mid="m2", **kw):
        await self.capture(mid=mid, **kw)
        return await self.bot._ensure_companion().read("next", "", "", "", reading_id, {"session_id": "private-one"})

    async def test_contiguous_progress_survives_restart_and_notes_are_derived(self):
        rid = await self.opened()
        result = await self.read_next(rid)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["data"]["reading"]["cursor"], 100)
        self.assertEqual(result["data"]["note_status"], "reviewed")
        await self.bot.on_unload()
        self.bot = self.make_bot()
        result = await self.read_next(rid, mid="after-restart")
        self.assertTrue(result["success"], result)
        self.assertEqual(result["data"]["reading"]["cursor"], 200)
        units = self.bot._companion.store.units(rid)
        self.assertEqual([u["unit_index"] for u in units], [0, 100])
        records = self.bot._tool_store.observations(limit=100)
        self.assertFalse(any(NOTE["self_response"] in r["text"] for r in records))

    async def test_budget_failure_keeps_pending_note_for_later_review(self):
        rid = await self.opened()
        self.bot.config.unified_mind.max_daily_model_calls = 0
        result = await self.read_next(rid)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["data"]["note_status"], "pending")
        self.bot.config.unified_mind.max_daily_model_calls = 10
        await self.capture(mid="review")
        result = await self.bot._companion.read("review", "", "", "", rid, {"session_id": "private-one"})
        self.assertTrue(result["success"], result)
        self.assertEqual(self.bot._companion.store.units(rid)[0]["note_status"], "reviewed")
        self.assertEqual(self.bot._companion.store.reading(rid)["cursor"], 100)

    async def test_changed_version_pauses_and_retracts_old_text(self):
        rid = await self.opened()
        await self.read_next(rid)
        self.bot._mcp_clients["pixiv"].version = 2
        result = await self.read_next(rid, mid="changed")
        self.assertFalse(result["success"])
        self.assertEqual(self.bot._companion.store.reading(rid)["status"], "paused")
        self.assertFalse(self.bot._tool_store.observations(limit=100))

    async def test_opening_new_version_also_withdraws_old_original_excerpts(self):
        rid = await self.opened()
        await self.read_next(rid)
        originals = self.bot._tool_store.observations(limit=100)
        excerpt_ids = [r["id"] for r in originals if "作品原文" in r["text"]]
        self.assertTrue(excerpt_ids)
        self.bot._mcp_clients["pixiv"].version = 2
        await self.capture(mid="new-version")
        result = await self.bot._companion.read("open", "pixiv", "novel", "123", "", {"session_id": "private-one"})
        self.assertTrue(result["success"], result)
        self.assertNotEqual(result["data"]["id"], rid)
        self.assertEqual(self.bot._tool_store.resolve(excerpt_ids), [])

    async def test_recovered_cache_creates_new_generation_without_washing_old_notes(self):
        rid = await self.opened()
        await self.read_next(rid)
        client = self.bot._mcp_clients["pixiv"]
        client.available = False
        self.assertFalse((await self.read_next(rid, mid="unavailable"))["success"])
        client.available = True
        await self.capture(mid="restored")
        result = await self.bot._companion.read("open", "pixiv", "novel", "123", "", {"session_id": "private-one"})
        self.assertTrue(result["success"], result)
        self.assertNotEqual(result["data"]["id"], rid)
        self.assertEqual(result["data"]["cursor"], 0)
        self.assertEqual(self.bot._companion.store.reading(rid)["status"], "paused")

    async def test_source_withdrawal_during_review_rejects_commit(self):
        rid = await self.opened()
        async def withdraw(**kwargs):
            self.bot._tool_store.retract_origin("pixiv", "work:novel:123")
            return {"response": json.dumps(NOTE)}
        self.bot.ctx.llm.generate.side_effect = withdraw
        result = await self.read_next(rid)
        self.assertFalse(result["success"])
        self.assertEqual(self.bot._companion.store.reading(rid)["cursor"], 0)
        self.assertEqual(self.bot._companion.store.reading(rid)["status"], "paused")

    async def test_other_speaker_and_owner_rebind_cannot_continue_old_book(self):
        rid = await self.opened()
        self.assertFalse((await self.read_next(rid, uid=OTHER))["success"])
        self.bot.config.owner.qq = OTHER
        await self.bot.on_config_update("plugin", {}, "2")
        self.assertFalse((await self.read_next(rid, mid="rebound", uid=OTHER))["success"])

    async def test_disabled_provider_cannot_reuse_live_client(self):
        rid = await self.opened()
        client = self.bot._mcp_clients["pixiv"]
        count = len(client.calls)
        self.bot.config.mcp_pixiv.enabled = False
        self.assertFalse((await self.read_next(rid))["success"])
        self.assertEqual(len(client.calls), count)
        self.assertEqual(self.bot._companion.store.reading(rid)["cursor"], 0)

    async def test_illustration_uses_file_pagination_not_page_as_file_offset(self):
        await self.capture()
        self.bot.config.mcp.media_enabled = True
        runtime = self.bot._ensure_companion()
        self.bot._ensure_tool_store()
        metadata = {"pixiv_id": 123, "current_version": 1, "type": "illust", "page_count": 2,
            "current_files": [{"page_index": 0, "file_type": "preview"}],
            "file_pagination": {"next_offset": 100}}
        second = {**metadata, "current_files": [{"page_index": 1, "file_type": "image"}],
                  "file_pagination": {"next_offset": None}}
        execute = AsyncMock(side_effect=[{"success": True, "data": metadata}, {"success": True, "data": second}])
        # A minimal PNG is sufficient for normalization; media fetching has separate real-byte tests.
        image = {"content_items": [{"type": "image", "mime_type": "image/png", "data": "iVBORw0KGgo="}]}
        with patch.object(self.bot._mcp_bridge, "execute", execute), patch(
                "MaiLivingWorld.mcp_media.fetch_pixiv_image", AsyncMock(return_value=image)):
            result = await runtime._fetch("pixiv", "illust", "123", 1,
                runtime.owner_context({"session_id": "private-one"})[0], lambda: True)
        self.assertEqual(result["index"], 1)
        self.assertEqual([call.args[2]["file_offset"] for call in execute.await_args_list], [0, 100])

    async def test_plan_requires_original_owner_private_message(self):
        await self.capture()
        result = await self.bot._ensure_companion().plans("propose", "一起读完这本书", "读第一章", 0,
                                                         {"session_id": "private-one"})
        self.assertTrue(result["success"], result)
        plan = result["data"]
        await self.capture(mid="other", uid=OTHER, text=plan["confirmation"])
        self.assertEqual(self.bot._companion.store.plan(plan["id"])["status"], "proposed")
        await self.capture(mid="group", group=GROUP, sid="group-one", text=plan["confirmation"])
        self.assertEqual(self.bot._companion.store.plan(plan["id"])["status"], "proposed")
        await self.capture(mid="accept", text=plan["confirmation"])
        self.assertEqual(self.bot._companion.store.plan(plan["id"])["status"], "active")
        await self.capture(mid="accept", text=plan["confirmation"])
        self.assertEqual(len(self.bot._companion.store.history(plan["id"])), 2)

    async def test_admin_dashboard_handles_unbound_and_enabled_states(self):
        b = self.bot
        b.config.unified_mind.enabled = False
        empty = await b.dashboard()
        self.assertFalse(empty["identity"]["owner_bound"])
        b.config.unified_mind.enabled = True
        rid = await self.opened()
        await self.read_next(rid)
        result = await b.dashboard()
        self.assertEqual(result["reading"][0]["current"], 100)
        self.assertTrue(result["identity"]["owner_bound"])
        self.assertNotIn(OWNER, json.dumps(result))
        self.assertEqual(result["budgets"]["reading"]["limit"], b.config.reading.max_daily_units)

    async def test_due_plan_uses_existing_proactive_gates_and_persistent_followup(self):
        b = self.bot
        await self.capture()
        result = await b._ensure_companion().plans("propose", "修复阅读问题", "检查日志", time.time()-60,
                                                  {"session_id": "private-one"})
        await self.capture(mid="accept", text=result["data"]["confirmation"])
        b.config.general.enabled = True
        b.config.general.dry_run = False
        b.config.shared_plans.followup_enabled = True
        b.config.audience.private_users = [OWNER]
        b.config.audience.quiet_start = b.config.audience.quiet_end = "00:00"
        b._store = Store(Path(self.temp.name)/"stories.db")
        await b._try_target("private", OWNER, datetime.now(ZoneInfo("Asia/Shanghai")))
        self.assertEqual(b.ctx.maisaka.proactive.trigger.await_count, 1)
        self.assertIn("检查日志", b.ctx.maisaka.proactive.trigger.call_args.args[1])
        self.assertIsNone(b._companion.due_plan(OWNER))
