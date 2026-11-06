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
from MaiLivingWorld.social_runtime import _public_interest_scores, _search_topic, public_interests


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
                  "news_search": ("query", "limit"), "web_search": ("query", "limit"), "news_read": ("id",),
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
        bot._interest_memories = lambda: [{"kind": "interest", "topic": PRIVATE + "里的天文摄影",
                                           "text": PRIVATE, "id": "private-source"}]

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

    def learn(self, ident, *, interests=(), knowledge=()):
        stamp = time.time()
        brain = self.bot._ensure_brain()
        with brain.con:
            brain.con.execute("""INSERT INTO brain_events
                (id,scope,actor_id,kind,summary,importance,happened,created)
                VALUES (?,?,?,'experience',?,3,?,?)""",
                (ident, "private:"+OWNER, OWNER, PRIVATE+ident, stamp, stamp))
        source = "brain:event:"+ident
        records = list(self.bot._resolve_roots([source]).values())
        self.bot._ensure_growth()
        store = self.bot._growth
        store.enqueue(records)
        job = store.claim()
        proposal = {"interests": [{"topic": topic, "delta": weight, "evidence_ids": [source]}
                                  for topic, weight in interests],
                    "knowledge": [{"topic": topic, "statement": "已经记录的测试知识", "confidence": 0.8,
                                   "evidence_ids": [source]} for topic in knowledge]}
        store.complete(job["id"], job["token"], proposal, records)
        return source

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

    async def test_no_account_news_search_uses_the_safe_interest_topic(self):
        self.bot.config.mcp.social_mode = "只逛逛"
        self.bot._interest_memories = lambda: [
            {"kind": "interest", "topic": PRIVATE + "的摄影", "weight": .3},
            {"kind": "interest", "topic": "游戏", "weight": -.4}]
        self.pick = "摄影"
        original = self.fixture.call_tool
        async def topic_news(name, args):
            if name == "news_search":
                self.fixture.calls.append((name, dict(args)))
                title = "摄影光线实验" if args.get("query") == "摄影" else "普通社区通告"
                return {"items": [{"id": "un:2026:11:1143147", "title": title, "summary": ""}],
                        "unavailable_sources": []}
            return await original(name, args)
        self.fixture.call_tool = topic_news
        with patch.dict(os.environ, {"MLW_MASTODON_TOKEN": "", "MLW_BRAVE_SEARCH_KEY": ""}):
            await self.tick()
        self.assertEqual(self.calls("news_search"), [{"limit": 10, "query": "摄影"}])
        self.assertEqual(self.calls("news_read"), [{"id": "un:2026:11:1143147"}])
        self.assertFalse(self.calls("web_search") or self.calls("mastodon_identity") or self.calls("mastodon_reply"))
        self.assertNotIn(PRIVATE, repr(self.prompts) + repr(self.fixture.calls))

    async def test_complete_empty_topic_search_falls_back_to_latest_news_once(self):
        self.pick = "科技新闻"
        original = self.fixture.call_tool
        async def empty_topic(name, args):
            if name == "news_search" and args.get("query"):
                self.fixture.calls.append((name, dict(args)))
                return {"items": [], "unavailable_sources": []}
            return await original(name, args)
        self.fixture.call_tool = empty_topic
        with patch.dict(os.environ, {"MLW_MASTODON_TOKEN": "", "MLW_BRAVE_SEARCH_KEY": ""}):
            await self.tick()
        self.assertEqual(self.calls("news_search"), [{"limit": 10, "query": "天文"}, {"limit": 10}])
        self.assertEqual(len(self.calls("news_read")), 1)

    async def test_topic_fallback_does_not_repeat_failed_partial_or_unverified_searches(self):
        original = self.fixture.call_tool
        for index, response in enumerate(({"items": []}, {"items": [], "unavailable_sources": ["feed"]}, None)):
            with self.subTest(response=response):
                async def incomplete(name, args):
                    if name == "news_search":
                        self.fixture.calls.append((name, dict(args)))
                        if response is None:
                            raise RuntimeError("synthetic provider failure")
                        return response
                    return await original(name, args)
                self.fixture.call_tool = incomplete
                before = len(self.calls("news_search"))
                with patch.dict(os.environ, {"MLW_MASTODON_TOKEN": "", "MLW_BRAVE_SEARCH_KEY": ""}):
                    if index:
                        await self.next_window(hours=index*3)
                    else:
                        await self.tick()
                self.assertEqual(len(self.calls("news_search"))-before, 1)
                self.assertFalse(self.calls("news_read"))

    async def test_topic_fallback_preserves_the_tool_budget_and_interest_version(self):
        original = self.fixture.call_tool
        interests = [{"kind": "interest", "topic": "摄影", "weight": .3}]
        self.bot._interest_memories = lambda: interests
        change = False
        async def empty_topic(name, args):
            if name == "news_search":
                self.fixture.calls.append((name, dict(args)))
                if change:
                    interests[0]["weight"] = -.3
                return {"items": [], "unavailable_sources": []}
            return await original(name, args)
        self.fixture.call_tool = empty_topic
        self.bot.config.mcp.max_daily_calls = 1
        with patch.dict(os.environ, {"MLW_MASTODON_TOKEN": "", "MLW_BRAVE_SEARCH_KEY": ""}):
            await self.tick()
            self.assertEqual(len(self.calls("news_search")), 1)
            self.bot.config.mcp.max_daily_calls = 10
            change = True
            await self.next_window()
        self.assertEqual(len(self.calls("news_search")), 2)
        self.assertFalse(self.calls("news_read"))

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
        self.bot._interest_memories = lambda: [
            {"kind": "interest", "topic": "游戏", "weight": -0.8},
            {"kind": "interest", "topic": PRIVATE + "的摄影", "weight": 0.3}]
        await self.tick()
        self.assertEqual(self.calls("web_search")[0]["query"], "摄影")
        self.assertEqual(self.calls("mastodon_search")[0]["query"], "摄影")
        self.assertEqual(self.calls("news_search")[0]["query"], "摄影")
        self.assertNotIn(PRIVATE, repr(self.prompts) + repr(self.fixture.calls))
        self.assertNotIn("游戏", public_interests([{"kind": "interest", "topic": "游戏", "weight": -0.5}]))
        self.assertEqual(public_interests([{"kind": "interest", "topic": t, "weight": -1}
                                          for t in public_interests([])]), [])

    async def test_all_disliked_topics_skip_outbound_search_but_can_consider_news(self):
        self.bot._interest_memories = lambda: [
            {"kind": "interest", "topic": topic, "weight": -0.3} for topic in public_interests([])]
        self.pick = "不感兴趣"
        await self.tick()
        self.assertFalse(self.calls("mastodon_search") or self.calls("web_search"))
        self.assertEqual(self.calls("news_search"), [{"limit": 10}])
        self.assertFalse(self.calls("news_read") or self.calls("mastodon_reply"))
        data = json.loads(self.prompts[0].rsplit("\n", 1)[-1])
        self.assertEqual(data["公开兴趣方向"], [])
        self.assertTrue(all(score == -0.3 for score in data["兴趣倾向"].values()))

    async def test_learned_preference_ranks_candidates_before_the_four_item_limit(self):
        self.bot._interest_memories = lambda: [
            {"kind": "interest", "topic": PRIVATE+"的摄影", "weight": 0.4},
            {"kind": "interest", "topic": "游戏", "weight": -0.8}]
        underlying = self.fixture.call_tool
        async def many_news(name, args):
            if name == "news_search":
                return {"items": [
                    *({"id": f"game-{i}", "title": f"游戏报道{i}"} for i in range(4)),
                    {"id": "new-interest", "title": "摄影观察"},
                    {"id": "unknown", "title": "未知的公开报道"}]}
            return await underlying(name, args)
        self.fixture.call_tool = many_news
        self.pick = "摄影观察"
        await self.tick()
        data = json.loads(self.prompts[0].rsplit("\n", 1)[-1])
        news = [row["title"] for row in data["条目"] if row["title"] in
                {"摄影观察", "未知的公开报道", *(f"游戏报道{i}" for i in range(4))}]
        self.assertEqual(news, ["摄影观察", "未知的公开报道", "游戏报道0", "游戏报道1"])
        self.assertEqual(self.calls("news_read"), [{"id": "new-interest"}])
        self.assertEqual(data["兴趣倾向"]["游戏"], -0.8)
        self.assertEqual(data["兴趣倾向"]["摄影"], 0.4)
        self.assertNotIn(PRIVATE, repr(self.prompts) + repr(self.fixture.calls))

    async def test_weighted_search_sequence_continues_after_restart(self):
        interests = [{"kind": "interest", "topic": "天文", "weight": 0.9},
                     {"kind": "interest", "topic": "摄影", "weight": 0.1}]
        self.bot._interest_memories = lambda: interests
        self.pick = "不感兴趣"
        await self.tick()
        await self.next_window()
        await self.bot.on_unload()
        self.bot = self.new_bot()
        self.bot._interest_memories = lambda: interests
        await self.next_window(hours=6)
        self.assertEqual([call["query"] for call in self.calls("web_search")], ["天文", "摄影", "天文"])

    async def test_changing_learned_weight_during_selection_discards_the_old_choice(self):
        interests = [{"kind": "interest", "topic": "天文", "weight": 0.4}]
        self.bot._interest_memories = lambda: interests
        original = self.bot.ctx.llm.generate.side_effect
        async def changed(prompt, **kwargs):
            result = await original(prompt, **kwargs)
            interests[0]["weight"] = -0.4
            return result
        self.bot.ctx.llm.generate.side_effect = changed
        await self.tick()
        self.assertFalse(self.calls("mastodon_read") or self.calls("news_read") or self.calls("mastodon_reply"))
        self.bot.ctx.llm.generate.side_effect = original
        self.pick = "不感兴趣"
        await self.next_window()
        self.assertEqual([call["query"] for call in self.calls("web_search")], ["天文", "摄影"])

    async def test_all_32_persisted_interests_survive_unrelated_knowledge_and_restart(self):
        del self.bot._interest_memories
        self.bot.config.growth.enabled = True
        self.bot.config.growth.mode = "active"
        self.bot.config.brain_memory.enabled = True
        topics = [("天文", 0.1), ("游戏", -0.1)] + [(f"偏好{i}", 0.1) for i in range(30)]
        for index in range(0, 32, 2):
            self.learn(f"interest-{index}", interests=topics[index:index+2])
        for index in range(0, 14, 2):
            self.learn(f"knowledge-{index}", knowledge=[f"知识{index}", f"知识{index+1}"])
        self.assertFalse(any(row["kind"] == "interest" for row in self.bot._derived_memories()))
        before = self.bot._interest_memories()
        self.assertEqual(len(before), 32)
        self.assertEqual(public_interests(before), ["天文"])
        self.pick = "不感兴趣"
        await self.tick()
        self.assertEqual(self.calls("web_search")[0]["query"], "天文")
        await self.bot.on_unload()
        self.bot = self.new_bot()
        del self.bot._interest_memories
        self.bot.config.growth.enabled = True
        self.bot.config.growth.mode = "active"
        self.bot.config.brain_memory.enabled = True
        self.assertEqual(self.bot._interest_memories(), before)
        brain = self.bot._ensure_brain()
        with brain.con:
            brain.con.execute("DELETE FROM brain_events WHERE id='interest-0'")
        self.assertEqual(len(self.bot._interest_memories()), 30)
        self.bot.config.growth.mode = "observe"
        self.assertEqual(self.bot._interest_memories(), [])

    async def test_changed_original_interest_source_during_read_prevents_public_reply(self):
        del self.bot._interest_memories
        self.bot.config.growth.enabled = True
        self.bot.config.growth.mode = "active"
        self.bot.config.brain_memory.enabled = True
        self.learn("astronomy", interests=[("天文", 0.1)])
        def change_source():
            brain = self.bot._ensure_brain()
            with brain.con:
                brain.con.execute("UPDATE brain_events SET summary=? WHERE id='astronomy'", ("修正后的经历",))
        self.fixture.after_read = change_source
        await self.tick()
        self.assertEqual(len(self.calls("mastodon_read")), 1)
        self.assertEqual(self.bot._interest_memories(), [])
        self.assertFalse(self.calls("mastodon_reply"))

    async def test_source_revision_change_preserving_score_still_discards_selection(self):
        interests = [{"kind": "interest", "id": "growth:interest:one", "topic": "天文", "weight": 0.4,
                      "revision": 1, "sources": [{"id": "original", "digest": "old-version"}]}]
        self.bot._interest_memories = lambda: interests
        original = self.bot.ctx.llm.generate.side_effect
        async def changed(prompt, **kwargs):
            result = await original(prompt, **kwargs)
            interests[0]["sources"][0]["digest"] = "new-version"
            return result
        self.bot.ctx.llm.generate.side_effect = changed
        await self.tick()
        self.assertFalse(self.calls("mastodon_read") or self.calls("news_read") or self.calls("mastodon_reply"))


class InterestProjectionTests(unittest.TestCase):
    def test_weighted_rotation_keeps_weak_interests_and_stays_bounded(self):
        scores = _public_interest_scores([
            {"kind": "interest", "topic": "天文", "weight": 0.9},
            {"kind": "interest", "topic": "摄影", "weight": 0.1},
            {"kind": "interest", "topic": "游戏", "weight": -0.1}])
        cycle = [_search_topic(scores, sequence) for sequence in range(1, 11)]
        self.assertEqual(cycle.count("天文"), 9)
        self.assertEqual(cycle.count("摄影"), 1)
        self.assertNotIn("游戏", cycle)
        self.assertEqual(cycle, [_search_topic(scores, sequence) for sequence in range(11, 21)])
        scores = _public_interest_scores([
            {"kind": "interest", "topic": "天文", "weight": 0.9},
            {"kind": "interest", "topic": "摄影", "weight": 0.00000001}])
        self.assertIn("摄影", [_search_topic(scores, sequence) for sequence in range(1, 11)])

    def test_tiny_dislikes_do_not_become_neutral_and_conflicts_cancel(self):
        scores = _public_interest_scores([
            {"kind": "interest", "topic": "游戏", "weight": -0.00000001},
            {"kind": "interest", "topic": "天文", "weight": 0.3},
            {"kind": "interest", "topic": "天文", "weight": -0.1},
            {"kind": "interest", "topic": "天文", "weight": -0.2}])
        self.assertLess(scores["游戏"], 0)
        self.assertEqual(scores["天文"], 0)
        self.assertNotIn("游戏", [_search_topic(scores, sequence) for sequence in range(1, 101)])
