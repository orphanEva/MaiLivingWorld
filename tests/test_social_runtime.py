"""Actual companion policy, MCP bridge, hooks and SQLite with offline public providers."""
import json
import os
from datetime import datetime
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from MaiLivingWorld.tests.test_mcp_plugin import GROUP, OTHER, OWNER, message
from MaiLivingWorld.tests.test_runtime import FakeContext, plugin


PRIVATE = "主人私下提起的蓝信封"
ORIGINAL = "公开讨论：比较两台望远镜的口径时应该控制观测条件。"
PUBLIC_REPLY = "两次观测是在相同光照下进行的吗?"


class PublicFixture:
    def __init__(self):
        self.calls = []
        self.wrong_receipt = False
        self.after_read = None

    async def list_tools(self):
        inputs = {"mastodon_identity": (), "mastodon_mentions": ("limit",), "mastodon_search": ("query", "limit"),
                  "news_search": ("limit",), "web_search": ("query", "limit"), "news_read": ("id",),
                  "web_read": ("id",), "mastodon_read": ("id",)}
        return {name: {"input_schema": {"type": "object", "properties": {
            key: {"type": "integer" if key == "limit" else "string"} for key in keys}}} for name, keys in inputs.items()}

    async def call_tool(self, name, args):
        self.calls.append((name, dict(args)))
        if name == "mastodon_identity":
            return {"account_id": "11", "instance": "https://social.example", "acct": "robot"}
        if name == "mastodon_mentions":
            return {"items": []}
        if name == "mastodon_search":
            return {"items": [{"id": "99", "text": ORIGINAL, "account_id": "22"}]}
        if name in ("news_search", "web_search"):
            title = "科技新闻：望远镜" if name == "news_search" else "公开网页：观测报告"
            ident = "un:2026:11:1143147" if name == "news_search" else "web_search-public"
            return {"items": [{"id": ident, "title": title, "summary": "天文动态"}]}
        if name in ("news_read", "web_read"):
            url = "https://news.un.org/zh/story/2026/11/1143147" if name == "news_read" else "https://public.example/story"
            return {"id": args["id"], "source": "公开测试站", "title": "观测报告", "url": url,
                    "text": "这次测量使用了同一台望远镜。", "basis": "article" if name == "news_read" else "web_excerpt",
                    "published_at": "2026-06-10"}
        if name == "mastodon_read":
            if self.after_read:
                self.after_read()
            return {"id": args["id"], "visibility": "public", "account_id": "22", "text": ORIGINAL}
        if name == "mastodon_reply":
            return {"in_reply_to_id": "77" if self.wrong_receipt else args["id"], "account_id": "11",
                    "id": "100", "url": "https://social.example/@robot/100", "visibility": "public"}
        raise AssertionError(name)


class SocialRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"MLW_MASTODON_INSTANCE": "https://social.example",
                                           "MLW_MASTODON_TOKEN": "local-fake-token", "MLW_BRAVE_SEARCH_KEY": "fake-key"})
        self.env.start()
        self.fixture = PublicFixture()
        self.prompts = []
        self.pick = "公开讨论"
        self.reply_text = PUBLIC_REPLY
        self.bot = self.new_bot()

    def new_bot(self):
        bot = plugin.MaiLivingWorld()
        bot.ctx = FakeContext()
        bot.ctx.paths = SimpleNamespace(data_dir=Path(self.tmp.name))
        bot.set_plugin_config({"owner": {"enabled": True, "qq": OWNER},
            "unified_mind": {"enabled": True, "mode": "active", "min_interval_seconds": 0},
            "brain_memory": {"include_owner": True, "group_ids": [GROUP], "private_users": [OTHER]},
            "mcp": {"enabled": True, "public_sources_enabled": True, "social_mode": "参与讨论"},
            "general": {"dry_run": False}})
        bot._mcp_client = lambda provider: self.fixture
        bot._derived_memories = lambda query="": [{"kind": "interest", "topic": PRIVATE + "里的天文摄影",
                                                    "text": PRIVATE, "id": "private-source"}] if not query else []

        async def model(prompt, **kwargs):
            self.prompts.append(prompt)
            data = json.loads(prompt.rsplit("\n", 1)[-1])
            if "条目" in data:
                choice = next((row["number"] for row in data["条目"] if self.pick in row["title"]), 0)
                if not choice and self.pick == "公开网页" and len(data["条目"]) == 1:
                    choice = 1
                value = {"number": choice}
            elif "原帖" in data:
                value = {"reply": self.reply_text}
            else:
                value = {"behavior": "neutral", "decisions": []}
            return {"success": True, "response": json.dumps(value, ensure_ascii=False)}
        bot.ctx.llm.generate = AsyncMock(side_effect=model)
        return bot

    async def asyncTearDown(self):
        try:
            await self.bot.on_unload()
        finally:
            self.env.stop()
            self.tmp.cleanup()

    async def tick(self):
        await self.bot._social_runtime().tick()

    def calls(self, name):
        return [args for tool, args in self.fixture.calls if tool == name]

    async def next_window(self, hours=3):
        store = self.bot._ensure_tool_store()
        original = store.claim_social_window
        stamp = time.time() + hours * 3600 + 1
        day = datetime.fromtimestamp(stamp, ZoneInfo(self.bot.config.general.timezone)).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        with patch.object(store, "claim_social_window", side_effect=lambda key, current_day:
                          original(key, day, now=stamp)):
            await self.tick()

    async def reply_to(self, event):
        await self.bot.capture_unified(message=event)
        result = await self.bot.brain_reply_hint(session_id=event["session_id"], reply_message_id=event["message_id"],
                                                   messages=[{"role": "system", "content": "聊天"}])
        return str(result)

    async def test_web_title_to_article_to_exact_public_thread_and_one_reply_across_restart(self):
        self.pick = "公开网页"
        await self.bot.capture_unified(message=message("private", text=PRIVATE))
        await self.tick()
        self.assertEqual(self.calls("web_read"), [{"id": "web_search-public"}])
        self.assertEqual(self.calls("mastodon_read"), [{"id": "99"}])
        self.assertEqual([(r["id"], r["text"]) for r in self.calls("mastodon_reply")], [("99", PUBLIC_REPLY)])
        self.assertTrue(any(ORIGINAL in p and "同一台望远镜" in p for p in self.prompts))
        for secret in (PRIVATE, OWNER, "local-fake-token", "fake-key"):
            self.assertNotIn(secret, repr(self.prompts) + repr(self.fixture.calls))
        self.assertEqual(self.calls("mastodon_search")[0]["query"], "天文")
        self.assertEqual(self.calls("web_search")[0]["query"], "天文")
        self.assertIn("观测报告", str(self.bot._collect_roots("望远镜")))
        await self.bot.on_unload()
        self.bot = self.new_bot()
        before = len(self.fixture.calls)
        await self.tick()
        self.assertEqual(len(self.fixture.calls), before)
        self.reply_text = "换一台镜头测量会有区别吗？"
        await self.next_window()
        self.assertEqual(len(self.calls("mastodon_reply")), 1)

    async def test_can_skip_uninteresting_items_and_leave_a_post_without_replying(self):
        self.pick = "没有兴趣"
        await self.tick()
        self.assertFalse(self.calls("mastodon_read") or self.calls("news_read") or self.calls("web_read"))
        self.pick = "公开讨论"
        self.reply_text = ""
        await self.next_window()
        self.assertEqual(len(self.calls("mastodon_read")), 1)
        self.assertFalse(self.calls("mastodon_reply"))

    async def test_browse_only_and_dry_run_can_read_but_never_write(self):
        self.bot.config.mcp.social_mode = "只逛逛"
        await self.tick()
        self.bot.config.mcp.social_mode = "参与讨论"
        self.bot.config.general.dry_run = True
        await self.next_window()
        self.assertEqual(len(self.calls("mastodon_read")), 1)
        self.assertFalse(self.calls("mastodon_reply"))

    async def test_replied_and_own_posts_do_not_hide_fresh_mentions_after_restart(self):
        await self.tick()
        first_reply = self.calls("mastodon_reply")[0]["text"]
        await self.bot.on_unload()
        self.bot = self.new_bot()
        underlying = self.fixture.call_tool
        fresh = {"id": "101", "text": "公开讨论：我用的是同样的光照条件。",
                 "account_id": "22", "visibility": "public", "in_reply_to_id": "100"}
        async def next_turn(name, args):
            if name == "mastodon_mentions":
                return {"items": [{"id": "99", "text": ORIGINAL, "account_id": "22"}, fresh]}
            if name == "mastodon_search":
                return {"items": [{"id": "100", "text": "公开讨论：" + first_reply, "account_id": "11"},
                                  {"id": "99", "text": ORIGINAL, "account_id": "22"}]}
            if name == "mastodon_read" and args["id"] == "101":
                self.fixture.calls.append((name, dict(args)))
                return dict(fresh)
            if name == "mastodon_read" and args["id"] == "100":
                self.fixture.calls.append((name, dict(args)))
                return {"id": "100", "text": first_reply, "account_id": "11", "visibility": "public"}
            return await underlying(name, args)
        self.fixture.call_tool = next_turn
        self.reply_text = "可以再比较一下两次观测的时间。"
        start = len(self.prompts)
        await self.next_window()
        self.assertEqual([r["id"] for r in self.calls("mastodon_reply")], ["99", "101"])
        choice = json.loads(self.prompts[start].rsplit("\n", 1)[-1])
        titles = [r["title"] for r in choice["条目"]]
        self.assertIn(fresh["text"], titles)
        self.assertNotIn(ORIGINAL, titles)
        self.assertNotIn("公开讨论：" + first_reply, titles)
        self.assertEqual([r["id"] for r in self.calls("mastodon_read")], ["99", "101", "100"])
        reply = json.loads(self.prompts[-1].rsplit("\n", 1)[-1])
        self.assertEqual(reply["对方引用的账号本人公开原话"], first_reply)

    async def test_silent_read_is_not_repeated_but_a_changed_public_post_can_be_read(self):
        self.bot.config.mcp.social_mode = "只逛逛"
        await self.tick()
        await self.bot.on_unload()
        self.bot = self.new_bot()
        self.bot.config.mcp.social_mode = "只逛逛"
        underlying = self.fixture.call_tool
        edited = ORIGINAL + "更新：增加了观测时间。"
        async def changed(name, args):
            result = await underlying(name, args)
            if name == "mastodon_search":
                result["items"][0]["text"] = edited
            if name == "mastodon_read":
                result["text"] = edited
            return result
        await self.next_window()
        self.assertEqual(len(self.calls("mastodon_read")), 1)
        self.fixture.call_tool = changed
        await self.next_window(hours=6)
        self.assertEqual(len(self.calls("mastodon_read")), 2)
        self.assertFalse(self.calls("mastodon_reply"))

    async def test_filtering_happens_before_the_four_candidate_limit(self):
        underlying = self.fixture.call_tool
        async def many_own_posts(name, args):
            if name == "mastodon_search":
                self.assertEqual(args["limit"], 10)
                return {"items": [
                    *({"id": str(200+i), "text": "公开讨论：我自己发的", "account_id": "11"} for i in range(6)),
                    {"id": "99", "text": ORIGINAL, "account_id": "22"}]}
            return await underlying(name, args)
        self.fixture.call_tool = many_own_posts
        await self.tick()
        menu = json.loads(self.prompts[0].rsplit("\n", 1)[-1])["条目"]
        self.assertNotIn("我自己发的", str(menu))
        self.assertEqual([r["id"] for r in self.calls("mastodon_reply")], ["99"])

    async def test_changed_config_while_reading_discards_pending_decision(self):
        self.fixture.after_read = lambda: setattr(self.bot.config.mcp, "social_mode", "关闭")
        await self.tick()
        self.assertEqual(len(self.calls("mastodon_read")), 1)
        self.assertFalse(self.calls("mastodon_reply"))
        self.fixture.after_read = None
        self.bot.config.mcp.social_mode = "参与讨论"
        await self.next_window()
        self.assertEqual(len(self.calls("mastodon_read")), 2)
        self.assertEqual(len(self.calls("mastodon_reply")), 1)

    async def test_ambiguous_receipt_never_retried_and_help_only_in_actual_owner_private_prompt(self):
        self.fixture.wrong_receipt = True
        await self.tick()
        self.assertEqual(len(self.calls("mastodon_reply")), 1)
        await self.next_window()
        self.assertEqual(len(self.calls("mastodon_reply")), 1)
        group = await self.reply_to(message("group", uid=OWNER, group=GROUP, sid=GROUP, text="今天有什么事"))
        stranger = await self.reply_to(message("stranger", uid=OTHER, sid=OTHER, text="今天有什么事"))
        self.assertNotIn("没有取得可核对的回执", group + stranger)
        own = await self.reply_to(message("own", uid=OWNER, sid=OWNER, text="今天有什么事"))
        self.assertIn("没有取得可核对的回执", own)
        self.assertNotIn("local-fake-token", own)
        self.assertEqual(self.bot._social_runtime().help_hint(OWNER), "")

    async def test_no_account_can_read_news_and_seek_owner_help_without_credentials(self):
        self.pick = "科技新闻"
        with patch.dict(os.environ, {"MLW_MASTODON_TOKEN": ""}):
            await self.tick()
            self.assertTrue(self.calls("news_read"))
            self.assertFalse(self.calls("mastodon_identity") or self.calls("mastodon_reply"))
            self.assertIn("独立账号", self.bot._social_runtime().help_hint(OWNER))

    async def test_only_direct_owner_acknowledgment_resolves_pending_help(self):
        social = self.bot._social_runtime()
        self.bot._ensure_tool_store().social_help(social.key(OWNER), "social_reply_uncertain")
        ack = "外站问题已处理"
        text = [{"type": "text", "data": {"text": ack}}]
        for event in (message("group", group=GROUP, text=ack), message("other", uid=OTHER, text=ack),
                      {**message("forward", text=ack), "raw_message": [{"type": "forward", "data": {}}]}):
            await self.bot.capture_unified(message={"raw_message": text, **event})
            self.assertIn("回执", social.help_hint(OWNER))
        await self.bot.capture_unified(message={**message("owner-ack", text=ack), "raw_message": text})
        self.assertEqual(social.help_hint(OWNER), "")

    async def test_external_query_uses_positive_interests_and_respects_dislikes(self):
        self.bot._derived_memories = lambda: [
            {"kind": "interest", "topic": "游戏", "weight": -0.8},
            {"kind": "interest", "topic": PRIVATE + "的摄影", "weight": 0.3}]
        await self.tick()
        self.assertEqual(self.calls("web_search")[0]["query"], "摄影")
        self.assertEqual(self.calls("mastodon_search")[0]["query"], "摄影")
        self.assertNotIn(PRIVATE, repr(self.prompts) + repr(self.fixture.calls))
        from MaiLivingWorld.social_runtime import public_interests
        self.assertNotIn("游戏", public_interests([{"kind": "interest", "topic": "游戏", "weight": -0.5}]))
        self.assertEqual(public_interests([{"kind": "interest", "topic": t, "weight": -1}
                                          for t in public_interests([])]), [])
