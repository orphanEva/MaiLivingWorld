"""Offline learning-to-library loop, preserving evidence and persistent limits."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from MaiLivingWorld.tests.test_runtime import FakeContext, plugin
from MaiLivingWorld.tests.test_growth_runtime import proposal, review, model_response


OWNER = "12345678"


class LibraryClient:
    retired = False

    def __init__(self, callback=None):
        self.calls = []
        self.callback = callback

    async def list_tools(self):
        return {"search_library": {"input_schema": {"type": "object", "properties": {
            "q": {"type": "string"}, "page_size": {"type": "integer"},
            "x_restrict": {"type": "integer"}, "statuses": {"type": "array"}}}},
            "get_library_work": {"input_schema": {"type": "object", "properties": {
                "pixiv_id": {"type": "integer"}, "kind": {"type": "string"}, "file_limit": {"type": "integer"}}}}}

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.callback:
            self.callback()
        if name == "search_library":
            return {"items": [{"pixiv_id": 321, "type": "novel"}]}
        return {"pixiv_id": 321, "type": "novel", "title": "倾听与交流", "accessible": True, "status": "active"}

    async def close(self):
        pass


class ResearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bot = self.make_bot()
        store = self.bot._ensure_tool_store()
        self.source = store.put_observation("cap", "album:1", "作品元数据：交流方法", "private:"+OWNER,
                                           subject_id=OWNER)
        data = proposal(self.source["id"])
        self.bot.ctx.llm.generate.side_effect = [model_response(data), model_response(review(data))]
        await self.bot._grow()
        self.assertEqual(len(self.bot._growth_snapshot()["questions"]), 1)

    def make_bot(self):
        bot = plugin.MaiLivingWorld()
        bot.ctx = FakeContext()
        bot.ctx.paths = SimpleNamespace(data_dir=Path(self.temp.name))
        bot.config = plugin.Config()
        bot.config.owner.enabled = bot.config.unified_mind.enabled = bot.config.growth.enabled = True
        bot.config.owner.qq = OWNER
        bot.config.unified_mind.mode = bot.config.growth.mode = "active"
        bot.config.unified_mind.min_interval_seconds = 0
        bot.config.mcp.enabled = bot.config.mcp.research_enabled = bot.config.mcp_pixiv.enabled = True
        bot._mcp_clients["pixiv"] = LibraryClient()
        return bot

    async def asyncTearDown(self):
        await self.bot.on_unload()
        self.temp.cleanup()

    async def test_question_reads_one_local_detail_and_restarts_keep_cooldown(self):
        client = self.bot._mcp_clients["pixiv"]
        await self.bot._research_tools()
        self.assertEqual([name for name, _ in client.calls], ["search_library", "get_library_work"])
        self.assertEqual(client.calls[0][1]["x_restrict"], 0)
        observations = self.bot._tool_store.observations()
        self.assertEqual(len(observations), 2)
        self.assertTrue(all(row["scope"] == "private:"+OWNER for row in observations))
        self.assertEqual(self.bot._growth_snapshot()["questions"][0]["status"], "open")
        await self.bot.on_unload()
        self.bot = self.make_bot()
        await self.bot._research_tools()
        self.assertEqual(self.bot._mcp_clients["pixiv"].calls, [])

    async def test_withdrawn_question_during_query_cannot_record_or_read_more(self):
        client = self.bot._mcp_clients["pixiv"]
        client.callback = lambda: self.bot._tool_store.retract_observation("cap", "album:1")
        await self.bot._research_tools()
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.bot._tool_store.observations(), [])
        self.assertEqual(self.bot._growth_snapshot()["questions"], [])

    async def test_disabled_and_budget_exhausted_never_call_service(self):
        client = self.bot._mcp_clients["pixiv"]
        self.bot.config.mcp.research_enabled = False
        await self.bot._research_tools()
        self.bot.config.mcp.research_enabled = True
        self.bot.config.mcp.max_daily_calls = 0
        await self.bot._research_tools()
        self.assertEqual(client.calls, [])

    async def test_observe_mode_never_queries_external_service(self):
        self.bot.config.growth.mode = "observe"
        await self.bot._research_tools()
        self.assertEqual(self.bot._mcp_clients["pixiv"].calls, [])
