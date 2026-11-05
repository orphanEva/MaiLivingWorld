"""Shared-reading boundaries at actual plugin exits, using SQLite and offline MCP."""
from __future__ import annotations

import copy
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from MaiLivingWorld.tests.test_runtime import FakeContext, plugin
from MaiLivingWorld.tests.test_companion_runtime import NovelMCP, TEXT
from MaiLivingWorld.tests.test_mcp_plugin import GROUP, OTHER, OWNER, message, schema
from MaiLivingWorld.shared_reading_store import command_text
from MaiLivingWorld.store import Store


SPOILER = "终章揭示旅人的真实身份"
SAFE = "无关作品的公开资料"


class BoundaryMCP(NovelMCP):
    def __init__(self):
        super().__init__()
        self.payload = None

    async def list_tools(self):
        tools = await super().list_tools()
        tools.update({
            "jm_get_album": {"input_schema": schema({"jm_id": {"type": "string"}}, ["jm_id"])},
            "search_library": {"input_schema": schema({"query": {"type": "string"},
                "page_size": {"type": "integer"}}, ["query"])},
        })
        return tools

    async def call_tool(self, name, arguments):
        if self.payload is not None:
            self.calls.append((name, dict(arguments)))
            return copy.deepcopy(self.payload)
        if name == "jm_get_album":
            self.calls.append((name, dict(arguments)))
            return {"jm_id": arguments["jm_id"], "title": SAFE}
        value = await super().call_tool(name, arguments)
        if name == "get_library_work":
            value["title"] = SPOILER if arguments["pixiv_id"] == 123 else SAFE
        return value


class ProjectsSharedReadingPluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bot = self.make_bot()
        await self.capture("opening")
        result = await self.bot._ensure_companion().read("open", "pixiv", "novel", "123", "",
                                                       {"session_id": "private-one"})
        self.assertTrue(result["success"], result)
        self.rid = result["data"]["id"]
        await self.capture("start-novel", text=command_text("start", self.rid))
        self.assertEqual(self.bot._shared_reading.store.session(OWNER, self.rid)["owner_units"], 0)
        tools, companion = self.bot._ensure_tool_store(), self.bot._ensure_companion()
        source = tools.put_observation("cap", "reading:pdf:456:source", "合成PDF版本绑定", "private:"+OWNER,
                                       subject_id=OWNER, origin_key="album:456")
        row = companion.store.open_reading("cap", "pdf", "456", "1", owner_id=OWNER,
            scope="private:"+OWNER, source_refs=[companion._ref(source)], total_units=2, title=SPOILER)
        self.cap_rid = row["id"]
        await self.capture("start-pdf", text=command_text("start", self.cap_rid))

    def make_bot(self, *, unified=True):
        bot = plugin.MaiLivingWorld()
        bot.ctx = FakeContext()
        bot.ctx.paths = SimpleNamespace(data_dir=Path(self.temp.name))
        bot.config = plugin.Config()
        bot.config.owner.enabled = bot.config.shared_reading.enabled = True
        bot.config.owner.qq = OWNER
        bot.config.unified_mind.enabled = unified
        bot.config.unified_mind.mode = "active"
        bot.config.unified_mind.max_daily_model_calls = 0
        bot.config.brain_memory.private_users = [OTHER]
        bot.config.brain_memory.group_ids = [GROUP]
        bot.config.mcp.enabled = bot.config.mcp_cap.enabled = bot.config.mcp_pixiv.enabled = True
        bot.config.reading.enabled = True
        bot.config.reading.excerpt_chars = 100
        bot._store = Store(Path(self.temp.name)/"living_world.sqlite3")
        bot._mcp_clients = {"pixiv": BoundaryMCP(), "cap": BoundaryMCP()}
        return bot

    async def capture(self, mid, **kwargs):
        event = message(mid=mid, **kwargs)
        await self.bot.capture_owner(message=event)
        await self.bot.capture_unified(message=event)
        return event

    async def asyncTearDown(self):
        await self.bot.on_unload()
        self.temp.cleanup()

    async def test_reading_list_and_next_hide_content_without_advancing_owner(self):
        await self.capture("next")
        client = self.bot._mcp_clients["pixiv"]
        before_calls = len(client.calls)
        result = await self.bot.reading("next", reading_id=self.rid, session_id="private-one")
        self.assertTrue(result["success"], result)
        self.assertGreater(len(client.calls), before_calls)
        self.assertNotIn(SPOILER, str(result))
        self.assertNotIn(TEXT[:20], str(result))
        self.assertNotIn("content_items", result)
        row = self.bot._companion.store.reading(self.rid, owner_id=OWNER)
        self.assertEqual(row["cursor"], 100)
        self.assertEqual(self.bot._companion.store.units(self.rid)[0]["excerpt"], TEXT[:100])
        result = await self.bot.reading("list", session_id="private-one")
        self.assertTrue(result["success"], result)
        self.assertIn("共读作品", result["content"])
        self.assertNotIn(SPOILER, result["content"])
        shared = self.bot._shared_reading.store.session(OWNER, self.rid)
        self.assertEqual((shared["owner_units"], shared["discussed_units"]), (0, 0))
        self.assertFalse((await self.bot.shared_reading("notes", self.rid, 1, session_id="private-one"))["success"])
        self.bot.ctx.llm.generate.assert_not_called()

    async def test_direct_mcp_and_numeric_aliases_block_before_transport(self):
        await self.capture("direct")
        requests = (("cap", "jm_get_album", {"jm_id": "456"}),
                    ("cap", "jm_get_album", {"jm_id": "000456"}),
                    ("pixiv", "get_library_work", {"kind": "novel", "pixiv_id": 123}),
                    ("pixiv", "read_novel_excerpt", {"pixiv_id": 123}))
        before = {name: len(client.calls) for name, client in self.bot._mcp_clients.items()}
        for provider, tool, arguments in requests:
            with self.subTest(provider=provider, tool=tool, arguments=arguments):
                result = await self.bot.mcp_call(provider, tool, arguments, session_id="private-one")
                self.assertFalse(result["success"])
                self.assertIn("shared_reading_boundary", result["content"])
        self.assertEqual({name: len(client.calls) for name, client in self.bot._mcp_clients.items()}, before)
        result = await self.bot.mcp_call("cap", "jm_get_album", {"jm_id": "999"}, session_id="private-one")
        self.assertTrue(result["success"], result)
        self.assertIn(SAFE, result["content"])

    async def test_nested_mcp_results_remove_protected_works_and_keep_unrelated_items(self):
        for provider, tool, arguments, key, protected in (
            ("pixiv", "search_library", {"query": "森林"}, "pixiv_id", "00123"),
            ("cap", "jm_get_album", {"jm_id": "999"}, "jm_id", "000456"),
        ):
            with self.subTest(provider=provider):
                await self.capture("nested-"+provider)
                self.bot._mcp_clients[provider].payload = {key: 999 if provider == "pixiv" else "999",
                    "title": SAFE, "nested": {"items": [{key: protected, "title": SPOILER},
                                                         {key: 777, "title": SAFE}]}}
                result = await self.bot.mcp_call(provider, tool, arguments, session_id="private-one")
                self.assertTrue(result["success"], result)
                self.assertNotIn(SPOILER, result["content"])
                self.assertNotIn(protected, result["content"])
                self.assertIn(SAFE, result["content"])
                self.assertIn("777", result["content"])
                await self.capture("direct-result-"+provider)
                self.bot._mcp_clients[provider].payload = {key: protected, "title": SPOILER}
                result = await self.bot.mcp_call(provider, tool, arguments, session_id="private-one")
                self.assertFalse(result["success"])
                self.assertIn("shared_reading_boundary", result["content"])
                self.assertNotIn(SPOILER, result["content"])

    async def test_protected_pixiv_image_never_fetches_media(self):
        await self.capture("image")
        self.bot.config.mcp.media_enabled = True
        before = len(self.bot._mcp_clients["pixiv"].calls)
        with patch("MaiLivingWorld.mcp_media.fetch_pixiv_image", AsyncMock()) as fetch:
            result = await self.bot.pixiv_image(123, session_id="private-one")
        self.assertFalse(result["success"])
        self.assertIn("shared_reading_boundary", result["content"])
        self.assertNotIn("content_items", result)
        fetch.assert_not_called()
        self.assertEqual(len(self.bot._mcp_clients["pixiv"].calls), before)

    async def test_owner_and_group_reply_recall_filter_keeps_internal_knowledge(self):
        tools = self.bot._ensure_tool_store()
        raw = tools.put_observation("pixiv", "reading:novel:123:1:100:0", "森林"+SPOILER,
            "private:"+OWNER, subject_id=OWNER, origin_key="work:novel:123")
        safe = tools.put_observation("pixiv", "work:novel:777", "森林"+SAFE,
            "private:"+OWNER, subject_id=OWNER, origin_key="work:novel:777")
        records = self.bot._collect_unified("森林")
        self.assertIn(raw["id"], {row["id"] for row in records})
        derivative = {"id": "projects:result:"+"a"*64, "text": SPOILER, "sources": [{"id": raw["id"]}]}
        for index, context in enumerate(({}, {"uid": OTHER, "sid": "group-one", "group": GROUP})):
            await self.capture("recall-"+str(index), text="森林作品", **context)
            sid = context.get("sid", "private-one")
            seen = []
            async def inspect_reply(*args, **kwargs):
                seen.extend(kwargs["recall"]("森林"))
                return ""
            with patch.object(self.bot._unified, "reply_hint", AsyncMock(side_effect=inspect_reply)) as reply:
                await self.bot.brain_reply_hint(session_id=sid, reply_message_id="recall-"+str(index))
                reply.assert_awaited_once()
            self.assertNotIn(raw["id"], {row["id"] for row in seen})
            self.assertIn(safe["id"], {row["id"] for row in seen})
            filtered = self.bot._expression_records([raw, derivative, safe],
                {"scope": "group:"+GROUP if context else "private:"+OWNER,
                 "speaker_id": OTHER if context else OWNER})
            self.assertEqual(filtered, [safe])
        self.assertEqual(tools.resolve([raw["id"]])[0]["text"], raw["text"])
        self.bot.ctx.llm.generate.assert_not_called()

    async def test_legacy_brain_and_mind_hints_are_suppressed(self):
        self.bot.config.unified_mind.enabled = False
        await self.assert_legacy_hints_protected()

    async def assert_legacy_hints_protected(self):
        self.bot.config.mind.enabled = self.bot.config.brain_memory.enabled = True
        self.bot.config.brain_memory.mode = "active"
        await self.capture("legacy")
        self.bot._brain_recent_private["private-one"] = ("private:"+OWNER, time.monotonic())
        brain = SimpleNamespace(hint=Mock(return_value=SPOILER))
        mind = SimpleNamespace(private_hint=Mock(return_value=SPOILER))
        with patch.object(self.bot, "_brain", brain), patch.object(self.bot, "_mind", mind):
            self.assertEqual(self.bot._brain_hint_for({"session_id": "private-one"}), "")
            self.assertNotIn(SPOILER, self.bot._owner_prompt_for({"session_id": "private-one"}))
            brain.hint.assert_not_called()
            mind.private_hint.assert_not_called()
            self.bot.config.shared_reading.enabled = False
            try:
                self.assertEqual(self.bot._brain_hint_for({"session_id": "private-one"}), SPOILER)
                self.assertIn(SPOILER, self.bot._owner_prompt_for({"session_id": "private-one"}))
            finally:
                self.bot.config.shared_reading.enabled = True

    async def test_stopped_and_disabled_sources_still_protect_plugin_exits(self):
        for index, rid in enumerate((self.rid, self.cap_rid)):
            await self.capture("stop-"+str(index), text=command_text("stop", rid))
        self.bot.config.reading.enabled = self.bot.config.mcp.enabled = False
        self.bot.config.mcp_cap.enabled = self.bot.config.mcp_pixiv.enabled = False
        self.assertTrue(self.bot._shared_reading_protected())
        result = await self.bot.mcp_call("cap", "jm_get_album", {"jm_id": "000456"}, session_id="private-one")
        self.assertIn("shared_reading_boundary", result["content"])
        result = await self.bot.pixiv_image(123, session_id="private-one")
        self.assertIn("shared_reading_boundary", result["content"])
        result = await self.bot.shared_reading("list", session_id="private-one")
        self.assertTrue(result["success"], result)
        self.assertNotIn(SPOILER, str(result))
        self.assertTrue(all(row["status"] == "stopped" and not row["source_available"] for row in result["data"]))

    async def test_cold_start_with_unified_disabled_restores_boundary_before_tool_or_hint(self):
        await self.bot.on_unload()
        self.bot = self.make_bot(unified=False)
        self.assertIsNone(self.bot._self_state)
        result = await self.bot.mcp_call("cap", "jm_get_album", {"jm_id": "000456"}, session_id="private-one")
        self.assertIn("shared_reading_boundary", result["content"])
        self.assertEqual(self.bot._self_state.state()["owner_id"], OWNER)
        self.assertEqual(self.bot._shared_reading.store.session(OWNER, self.rid)["owner_units"], 0)
        self.assertFalse(self.bot._mcp_clients["cap"].calls)
        await self.assert_legacy_hints_protected()


if __name__ == "__main__":
    unittest.main()
