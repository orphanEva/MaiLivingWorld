"""Actual Companion/MCP paths under attention interruption; all transports offline."""
from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import AsyncMock

from MaiLivingWorld.tests import test_companion_runtime as reading_fixtures
from MaiLivingWorld.tests import test_mcp_plugin as mcp_fixtures

OWNER = mcp_fixtures.OWNER


class InterruptibleMCP(reading_fixtures.NovelMCP):
    def __init__(self):
        super().__init__()
        self.after_result = None

    async def list_tools(self):
        tools = await super().list_tools()
        tools["search_library"] = {"input_schema": mcp_fixtures.schema({
            "q": {"type": "string"}, "page_size": {"type": "integer"}, "x_restrict": {"type": "integer"},
            "statuses": {"type": "array", "items": {"type": "string"}}}, ["q"])}
        return tools

    async def call_tool(self, name, arguments):
        if name == "search_library":
            self.calls.append((name, dict(arguments)))
            result = {"items": [{"pixiv_id": 123, "type": "novel", "title": "FOREIGN_RESULT_MUST_NOT_COMMIT"}], "total": 1}
        else:
            result = await super().call_tool(name, arguments)
        if self.after_result:
            await self.after_result(name)
        return result


class AttentionIntegrationReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bot = reading_fixtures.CompanionPluginTests.make_bot(self)
        self.bot.config.unified_mind.mode = "active"
        self.bot.config.reading.auto_continue = True
        self.bot.config.reading.interval_seconds = 0
        self.bot.config.attention.interval_seconds = 0
        self.client = InterruptibleMCP()
        self.bot._mcp_clients["pixiv"] = self.client

    async def asyncTearDown(self):
        await self.bot.on_unload()
        self.temp.cleanup()

    async def opened(self):
        # Open before attention activation so the background step starts outside
        # the owner's engagement window without forging an inbound message.
        await self.bot.capture_unified(message=mcp_fixtures.message(mid="open-book"))
        result = await self.bot._ensure_companion().read("open", "pixiv", "novel", "123", "", {"session_id": "private-one"})
        self.assertTrue(result["success"], result)
        self.bot.config.attention.enabled = True
        runtime = self.bot._ensure_attention()
        self.bot.ctx.llm.generate.reset_mock()
        return runtime, result["data"]["id"]

    async def incoming(self):
        await self.bot.capture_unified(message=mcp_fixtures.message(mid="new-owner-turn", text="我回来了，先和我聊聊"))

    def assert_unread(self, ident):
        store = self.bot._companion.store
        self.assertEqual(store.reading(ident, owner_id=OWNER)["cursor"], 0)
        self.assertEqual(store.con.execute("SELECT COUNT(*) FROM companion_units WHERE reading_id=?", (ident,)).fetchone()[0], 0)

    async def test_normal_reading_progress_preserves_candidate_version_and_finishes(self):
        runtime, ident = await self.opened()
        initial = runtime.candidates()[0]
        await runtime.tick()
        self.assertEqual(runtime.last_status, "progress")
        self.assertEqual(self.bot._companion.store.reading(ident)["cursor"], 100)
        self.assertEqual(runtime.candidates()[0], initial)
        await runtime.tick()
        self.assertEqual(runtime.last_status, "progress")
        self.assertEqual(self.bot._companion.store.reading(ident)["cursor"], 200)
        self.assertEqual(runtime.candidates()[0], initial)
        # A completed work leaving the candidate list is a real progress result.
        for _ in range((len(reading_fixtures.TEXT)+99)//100):
            if self.bot._companion.store.reading(ident)["cursor"] >= len(reading_fixtures.TEXT):
                break
            await runtime.tick()
            self.assertEqual(runtime.last_status, "progress")
        self.assertEqual(self.bot._companion.store.reading(ident)["cursor"], len(reading_fixtures.TEXT))
        self.assertEqual(runtime.candidates(), [])
        self.assertEqual(runtime.store.state(OWNER)["last_outcome"], "progress")

    async def test_new_message_during_actual_mcp_fetch_prevents_reading_commit(self):
        runtime, ident = await self.opened()
        async def after_result(name):
            if name == "read_novel_excerpt":
                await self.incoming()
        self.client.after_result = after_result
        await runtime.tick()
        self.assert_unread(ident)
        self.bot.ctx.llm.generate.assert_not_awaited()
        self.assertEqual(runtime.store.state(OWNER)["last_outcome"], "interrupted")

    async def test_new_message_during_note_model_prevents_second_pass_and_unit_commit(self):
        runtime, ident = await self.opened()
        async def model(*args, **kwargs):
            await self.incoming()
            return {"response": json.dumps(reading_fixtures.NOTE)}
        self.bot.ctx.llm.generate = AsyncMock(side_effect=model)
        await runtime.tick()
        self.assert_unread(ident)
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 1)
        self.assertEqual(runtime.store.state(OWNER)["last_outcome"], "interrupted")

    async def test_reading_config_changed_during_model_prevents_commit(self):
        runtime, ident = await self.opened()
        async def model(*args, **kwargs):
            self.bot.config.reading.enabled = False
            return {"response": json.dumps(reading_fixtures.NOTE)}
        self.bot.ctx.llm.generate = AsyncMock(side_effect=model)
        await runtime.tick()
        self.assert_unread(ident)
        self.assertEqual(runtime.store.state(OWNER)["last_outcome"], "interrupted")

    async def test_owner_changed_during_model_cannot_commit_previous_owners_unit(self):
        runtime, ident = await self.opened()
        async def model(*args, **kwargs):
            self.bot.config.owner.qq = mcp_fixtures.OTHER
            self.bot._ensure_unified()
            return {"response": json.dumps(reading_fixtures.NOTE)}
        self.bot.ctx.llm.generate = AsyncMock(side_effect=model)
        await runtime.tick()
        self.assert_unread(ident)
        self.assertEqual(runtime.store.state(OWNER)["last_outcome"], "interrupted")
        self.assertEqual(runtime.store.state(mcp_fixtures.OTHER)["cycle_count"], 0)

    async def test_source_withdrawn_during_model_cannot_commit_or_reappear_as_candidate(self):
        runtime, ident = await self.opened()
        async def model(*args, **kwargs):
            self.bot._tool_store.retract_origin("pixiv", "work:novel:123")
            return {"response": json.dumps(reading_fixtures.NOTE)}
        self.bot.ctx.llm.generate = AsyncMock(side_effect=model)
        await runtime.tick()
        self.assert_unread(ident)
        self.assertEqual(runtime.candidates(), [])
        self.assertNotEqual(runtime.store.state(OWNER)["last_outcome"], "progress")

    def research(self):
        b = self.bot
        b.config.attention.enabled = b.config.growth.enabled = True
        b.config.growth.mode = "active"
        b.config.mcp.research_enabled = b.config.mcp.remember_results = True
        b.config.mcp.research_interval_seconds = 1
        b._ensure_unified()
        question = {"key": "q:"+"a"*24, "topic": "森林里的故事", "text": "想了解森林里的故事", "status": "open",
                    "revision": 1, "sources": []}
        snapshot = {"questions": [question], "interests": [], "traits": {"curiosity": .6}}
        b._growth_snapshot = lambda: snapshot
        return b._ensure_attention(), question

    async def test_new_message_during_real_research_bridge_stops_followup_and_observation(self):
        runtime, _ = self.research()
        async def after_result(name):
            if name == "search_library":
                await self.incoming()
        self.client.after_result = after_result
        await runtime.tick()
        self.assertEqual([name for name, _ in self.client.calls], ["search_library"])
        self.assertEqual(self.bot._tool_store.con.execute("SELECT COUNT(*) FROM tool_observations").fetchone()[0], 0)
        self.assertEqual(runtime.store.state(OWNER)["last_outcome"], "interrupted")

    async def test_question_revision_changed_during_real_research_cannot_publish_old_result(self):
        runtime, question = self.research()
        async def after_result(name):
            if name == "search_library":
                question["revision"] += 1
        self.client.after_result = after_result
        await runtime.tick()
        self.assertEqual([name for name, _ in self.client.calls], ["search_library"])
        self.assertEqual(self.bot._tool_store.con.execute("SELECT COUNT(*) FROM tool_observations").fetchone()[0], 0)
        self.assertNotEqual(runtime.store.state(OWNER)["last_outcome"], "progress")
