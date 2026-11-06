"""Recall of actual autonomous reading through ordinary QQ receive and reply hooks."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from MaiLivingWorld.tests.test_mcp_plugin import GROUP, OTHER, OWNER, message
from MaiLivingWorld.tests.test_runtime import FakeContext, plugin
from MaiLivingWorld.tests.test_social_runtime import PublicFixture
from MaiLivingWorld.tests.test_unified_runtime import kwargs
from MaiLivingWorld.tests.test_public_tools_plugin import REFERENCE, fixture_client


AUTONOMOUS = "自主浏览时亲自选中"


class DiscoverySources(PublicFixture):
    def __init__(self):
        super().__init__()
        self.number = 0
        self.unavailable = False

    async def call_tool(self, name, args):
        result = await super().call_tool(name, args)
        if name == "news_search":
            result["items"][0]["id"] = f"un:2026:11:{1143147 + self.number}"
        if name == "news_read":
            result["url"] = f"https://news.un.org/zh/story/2026/11/{1143147 + self.number}"
            result["title"] += str(self.number)
        if name in ("news_read", "web_read") and self.unavailable:
            result.update(text="", basis="unavailable")
        return result


class SocialDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.enterContext(patch.dict(os.environ, {
            "MLW_MASTODON_INSTANCE": "", "MLW_MASTODON_TOKEN": "", "MLW_BRAVE_SEARCH_KEY": "fixture-only-key"}))
        self.site = DiscoverySources()
        self.mode = "say"
        self.pick = "科技新闻"
        self.prompts = []
        self.round = 0
        self.bot = self.make_bot()

    def make_bot(self):
        bot = plugin.MaiLivingWorld()
        bot.ctx = FakeContext()
        bot.ctx.paths = SimpleNamespace(data_dir=Path(self.folder.name))
        bot.set_plugin_config({
            "owner": {"enabled": True, "qq": OWNER},
            "unified_mind": {"enabled": True, "mode": "active", "min_interval_seconds": 0},
            "brain_memory": {"include_owner": True, "group_ids": [GROUP], "private_users": [OTHER]},
            "mcp": {"enabled": True, "public_sources_enabled": True, "social_mode": "只逛逛"},
        })
        bot._mcp_client = lambda provider: self.site
        async def model(prompt, **options):
            self.prompts.append(prompt)
            if "UNTRUSTED_INPUT_JSON\n" in prompt:
                data = json.loads(prompt.rsplit("UNTRUSTED_INPUT_JSON\n", 1)[1])
                value = {"behavior": "neutral", "decisions": [
                    {"id": row["id"], "mode": self.mode} for row in data["memories"]]}
            else:
                data = json.loads(prompt.rsplit("\n", 1)[1])
                number = next((row["number"] for row in data.get("条目", []) if self.pick in row["title"]), 0)
                value = {"number": number}
            return {"content": json.dumps(value, ensure_ascii=False)}
        bot.ctx.llm.generate = AsyncMock(side_effect=model)
        return bot

    async def asyncTearDown(self):
        await self.bot.on_unload()

    async def browse(self):
        self.round += 1
        store = self.bot._ensure_tool_store()
        claim = store.claim_social_window
        # Only advance the exploration lease. Persisted reads retain their actual clock time.
        with patch.object(store, "claim_social_window", side_effect=lambda key, day:
                          claim(key, 0, now=self.round * 10801)):
            await self.bot._social_runtime().tick()

    async def ask(self, mid, text="你最近逛到了什么？", **audience):
        event = message(mid, text=text, **audience)
        event["raw_message"] = [{"type": "text", "data": text}]
        await self.bot.capture_unified(message=event)
        result = await self.bot.brain_reply_hint(**kwargs(event["session_id"], mid))
        return json.dumps(result, ensure_ascii=False)

    async def read_reference(self, *, autonomous=True):
        self.bot._ensure_tool_store()
        client = fixture_client()
        client.call_tool.return_value = REFERENCE
        return await self.bot._mcp_bridge.execute("reference", "reference_read", {"page_id": 42},
            config=self.bot.config.mcp, context={"scope": "private:"+OWNER, "speaker_id": OWNER,
            "sid": "reference-research", "mid": "read-page"}, owner=OWNER, client=client, valid=lambda: True,
            record_observations=True, autonomous_read=autonomous, timezone=self.bot.config.general.timezone)

    async def test_autonomous_reference_read_is_recalled_after_restart_with_browsing_off(self):
        result = await self.read_reference()
        self.assertTrue(result["success"], result)
        record, = self.bot._tool_store.recent_social_reads(OWNER)
        self.assertEqual(record["source_name"], "reference")
        self.assertIn("修订98", record["text"])
        await self.bot.on_unload()
        self.bot = self.make_bot()
        self.bot.config.mcp.social_mode = "关闭"
        visible = await self.ask("read-recall", "你最近读过什么？")
        self.assertIn(REFERENCE["url"], visible)
        self.assertIn(AUTONOMOUS, visible)
        self.assertIn("source_revision", visible)
        self.assertNotIn("修订98", visible)
        self.mode = "omit"
        hidden = await self.ask("read-hidden", "你最近读过什么？")
        self.assertNotIn(REFERENCE["url"], hidden)
        self.assertNotIn(AUTONOMOUS, hidden)

    async def test_requested_reference_read_does_not_become_own_discovery(self):
        result = await self.read_reference(autonomous=False)
        self.assertTrue(result["success"], result)
        self.assertEqual(len(self.bot._tool_store.observations()), 1)
        self.assertEqual(self.bot._tool_store.recent_social_reads(OWNER), [])
        self.assertNotIn(AUTONOMOUS, await self.ask("manual-reference"))

    async def test_reading_survives_restart_and_owner_can_recall_it_with_source(self):
        await self.browse()
        record, = self.bot._ensure_tool_store().recent_social_reads(OWNER)
        self.assertIn("观测报告", record["text"])
        self.assertEqual(self.bot._ensure_tool_store().recent_social_reads(OTHER), [])
        await self.bot.on_unload()
        self.bot = self.make_bot()
        for mid, question in (("first", "你最近逛到了什么？"), ("second", "最近有没有看到什么有意思的东西？")):
            visible = await self.ask(mid, question)
            self.assertIn(record["url"], visible)
            self.assertIn(AUTONOMOUS, visible)
            self.assertNotIn("fixture-only-key", visible)
            deliberation = self.bot.ctx.llm.generate.await_args.args[0]
            self.assertIn("观测报告", deliberation)
            self.assertNotIn(record["url"], deliberation)

    async def test_web_read_is_actual_discovery_but_skipping_or_failed_reads_are_not(self):
        self.pick = "并不感兴趣"
        await self.browse()
        self.assertEqual(self.bot._ensure_tool_store().recent_social_reads(OWNER), [])
        self.pick = "公开网页"
        self.site.unavailable = True
        await self.browse()
        self.assertEqual(self.bot._ensure_tool_store().recent_social_reads(OWNER), [])
        self.site.unavailable = False
        await self.browse()
        record, = self.bot._ensure_tool_store().recent_social_reads(OWNER)
        self.assertEqual(record["evidence_status"], "web_excerpt")
        visible = await self.ask("web")
        self.assertIn(record["url"], visible)
        self.assertIn(AUTONOMOUS, visible)

    async def test_expression_permission_and_current_audience_control_discovery_claim(self):
        await self.browse()
        record, = self.bot._ensure_tool_store().recent_social_reads(OWNER)
        self.mode = "omit"
        hidden = await self.ask("omitted")
        self.assertNotIn(record["url"], hidden)
        self.assertNotIn(AUTONOMOUS, hidden)
        self.mode = "say"
        for mid, audience in (
                ("group", {"group": GROUP, "sid": "group-session"}),
                ("stranger", {"uid": OTHER, "sid": "other-session"})):
            visible = await self.ask(mid, **audience)
            self.assertNotIn(AUTONOMOUS, visible)

    async def test_disabled_reading_memory_and_quote_do_not_claim_autonomous_discovery(self):
        await self.browse()
        for index, field in enumerate(("remember_results", "public_sources_enabled", "enabled")):
            setattr(self.bot.config.mcp, field, False)
            self.assertNotIn(AUTONOMOUS, await self.ask(f"disabled-{index}"))
            setattr(self.bot.config.mcp, field, True)
        self.assertNotIn(AUTONOMOUS, await self.ask("quote", "朋友问“你最近逛到了什么？”，我只是转述。"))
        self.assertNotIn(AUTONOMOUS, await self.ask("ordinary", "今天天气怎么样？"))

    async def test_requested_tool_read_is_not_an_autonomous_experience(self):
        text = "帮我读这篇公开报道"
        event = message("requested", text=text)
        event["raw_message"] = [{"type": "text", "data": text}]
        await self.bot.capture_unified(message=event)
        result = await self.bot.mcp_call("news", "news_read", {"id": "un:2026:11:1143147"}, message=event)
        self.assertTrue(result["success"], result)
        self.assertEqual(len(self.bot._ensure_tool_store().observations()), 1)
        self.assertEqual(self.bot._ensure_tool_store().recent_social_reads(OWNER), [])
        self.assertNotIn(AUTONOMOUS, await self.ask("after-request"))

    async def test_recent_discovery_limit_expiry_and_retraction(self):
        for number in range(3):
            self.site.number = number
            await self.browse()
        store = self.bot._ensure_tool_store()
        records = store.recent_social_reads(OWNER)
        self.assertEqual(len(records), 2)
        self.assertEqual({r["url"] for r in records}, {
            "https://news.un.org/zh/story/2026/11/1143148", "https://news.un.org/zh/story/2026/11/1143149"})
        newest = max(row["timestamp"] for row in records)
        self.assertTrue(store.recent_social_reads(OWNER, now=newest + 7*86400 - 1))
        self.assertEqual(store.recent_social_reads(OWNER, now=newest + 7*86400 + 1), [])
        for record in store.observations():
            key = "article:" + hashlib.sha256(record["url"].encode()).hexdigest()
            store.retract_observation("news", key)
        self.assertEqual(store.recent_social_reads(OWNER), [])
        self.assertNotIn(AUTONOMOUS, await self.ask("after-retraction"))

    async def test_manual_source_update_cannot_become_the_old_autonomous_experience(self):
        await self.browse()
        record, = self.bot._ensure_tool_store().recent_social_reads(OWNER)
        previous = self.site.call_tool
        async def amended_source(name, args):
            result = await previous(name, args)
            if name == "news_read":
                result["text"] = "最新更正加入了另一组观测数据。"
            return result
        self.site.call_tool = amended_source
        event = message("update", text="请重新读这篇报道")
        event["raw_message"] = [{"type": "text", "data": event["processed_plain_text"]}]
        await self.bot.capture_unified(message=event)
        result = await self.bot.mcp_call("news", "news_read", {"id": "un:2026:11:1143147"}, message=event)
        self.assertTrue(result["success"])
        self.assertIn("最新更正", result["content"])
        self.assertEqual(self.bot._ensure_tool_store().recent_social_reads(OWNER), [])
        self.assertNotIn(AUTONOMOUS, await self.ask("after-manual-update"))
        self.assertIn(record["id"], {row["id"] for row in self.bot._ensure_tool_store().observations()})

    async def test_retracting_and_manually_restoring_same_text_does_not_restore_autonomous_claim(self):
        await self.browse()
        store = self.bot._ensure_tool_store()
        record, = store.recent_social_reads(OWNER)
        key = "article:" + hashlib.sha256(record["url"].encode()).hexdigest()
        store.retract_observation("news", key)
        event = message("restored", text="请再读这篇公开报道")
        event["raw_message"] = [{"type": "text", "data": event["processed_plain_text"]}]
        await self.bot.capture_unified(message=event)
        result = await self.bot.mcp_call("news", "news_read", {"id": "un:2026:11:1143147"}, message=event)
        self.assertTrue(result["success"])
        self.assertEqual(store.observations()[0]["text"], record["text"])
        self.assertEqual(store.recent_social_reads(OWNER), [])
        self.assertNotIn(AUTONOMOUS, await self.ask("after-manual-restoration"))
