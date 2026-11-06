"""Autonomous news and web menus use read bodies without replaying unchanged listings."""
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from MaiLivingWorld.public_news import _article_url
from MaiLivingWorld.tests.test_mcp_plugin import OWNER, message
from MaiLivingWorld.tests.test_runtime import FakeContext, plugin
from MaiLivingWorld.tests.test_social_runtime import PublicFixture


class Articles(PublicFixture):
    def __init__(self):
        super().__init__()
        self.web = False
        self.entries = ["un:2026:11:1143147"]
        self.title = "观测报告"
        self.basis = "article"
        self.body_source = "观测资料"
        self.reads = []

    def row(self, ident):
        url = "https://observatory.example/report" if ident.startswith("web_") else _article_url(ident)[1]
        return {"id": ident, "url": url, "title": self.title, "source": "观测资料",
                "summary": "对照两次观测结果", "published_at": "2026-11-06"}

    async def call_tool(self, name, args):
        if name in ("news_search", "web_search"):
            return {"items": [self.row(ident) for ident in self.entries]
                    if (name == "web_search") == self.web else []}
        if name in ("news_read", "web_read"):
            self.reads.append(args["id"])
            return {**self.row(args["id"]), "source": self.body_source,
                    "text": "公开报告记录了两次观测时的曝光参数和光照。",
                    "basis": "web_excerpt" if name == "web_read" and self.basis == "article" else self.basis}
        return await super().call_tool(name, args)


class ArticleFreshnessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.enterContext(patch.dict(os.environ, {"MLW_MASTODON_INSTANCE": "", "MLW_MASTODON_TOKEN": "",
                                                  "MLW_BRAVE_SEARCH_KEY": "fixture-key"}))
        self.site = Articles()
        self.round = 0
        self.bot = self.make_bot()

    def make_bot(self):
        bot = plugin.MaiLivingWorld()
        bot.ctx = FakeContext()
        bot.ctx.paths = SimpleNamespace(data_dir=Path(self.folder.name))
        bot.set_plugin_config({"owner": {"enabled": True, "qq": OWNER},
            "unified_mind": {"enabled": True, "mode": "active"},
            "mcp": {"enabled": True, "public_sources_enabled": True, "social_mode": "只逛逛"}})
        bot._mcp_client = lambda provider: self.site
        bot.ctx.llm.generate = AsyncMock(return_value={"content": json.dumps({"number": 1})})
        return bot

    async def asyncTearDown(self):
        await self.bot.on_unload()

    async def browse(self):
        self.round += 1
        stamp = time.time() + self.round * 10801
        day = datetime.fromtimestamp(stamp, ZoneInfo(self.bot.config.general.timezone)).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        store = self.bot._ensure_tool_store()
        original = store.claim_social_window
        with patch.object(store, "claim_social_window", side_effect=lambda key, unused: original(key, day, now=stamp)):
            await self.bot._social_runtime().tick()

    async def test_unchanged_read_article_yields_to_new_story_after_restart(self):
        await self.browse()
        await self.bot.on_unload()
        self.bot = self.make_bot()
        self.site.entries.append("un:2026:11:1143148")
        await self.browse()
        self.assertEqual(self.site.reads, ["un:2026:11:1143147", "un:2026:11:1143148"])
        # Manually requesting the first article still goes through its normal tool path.
        event = message("manual", text="请重新读这篇报道")
        event["raw_message"] = [{"type": "text", "data": event["processed_plain_text"]}]
        await self.bot.capture_unified(message=event)
        result = await self.bot.mcp_call("news", "news_read", {"id": self.site.entries[0]}, message=event)
        self.assertTrue(result["success"])
        self.assertEqual(self.site.reads[-1], "un:2026:11:1143147")

    async def test_web_handle_rotation_does_not_turn_the_same_url_into_new_reading(self):
        self.site.web = True
        self.site.entries = ["web_first"]
        await self.browse()
        self.site.entries = ["web_second"]
        await self.browse()
        self.assertEqual(self.site.reads, ["web_first"])
        self.site.title = "更正后的观测报告"
        await self.browse()
        self.assertEqual(self.site.reads, ["web_first", "web_second"])

    async def test_summary_read_remains_eligible_for_a_later_body_read(self):
        self.site.basis = "feed_summary"
        await self.browse()
        self.site.basis = "article"
        await self.browse()
        self.assertEqual(self.site.reads, ["un:2026:11:1143147"] * 2)
        await self.browse()
        self.assertEqual(len(self.site.reads), 2)

    async def test_new_listing_cannot_reuse_an_older_body_when_current_result_has_no_receipt(self):
        await self.browse()
        self.site.title = "更正后的观测报告"
        self.site.body_source = ""
        await self.browse()
        self.site.body_source = "观测资料"
        await self.browse()
        self.assertEqual(self.site.reads, ["un:2026:11:1143147"] * 3)

