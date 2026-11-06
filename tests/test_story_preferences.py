"""Source-backed preferences shape the ordinary QQ story-selection path."""
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

from MaiLivingWorld.explore import Story
from MaiLivingWorld.growth_model import empty_growth
from MaiLivingWorld.store import Store
from MaiLivingWorld.tests import test_growth_runtime as growth_fixture
from MaiLivingWorld.tests.test_growth_runtime import model_response, review


TARGET = "qq:private:12345678"


class StoryPreferenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bot = self.make_bot()

    def make_bot(self):
        bot = growth_fixture.GrowthRuntimeTests.make_bot(self)
        bot._store = Store(Path(self.temp.name)/"stories.sqlite3")
        bot.config.explorer.enabled = True
        bot.config.explorer.interests = []
        bot.config.explorer.candidate_count = 1
        bot.config.mcp.enabled = bot.config.mcp.public_sources_enabled = True
        bot.config.brain.use_llm_selection = False
        return bot

    async def asyncTearDown(self):
        await self.bot.on_unload()
        self.temp.cleanup()

    def source(self, key, text, *, private=False):
        return self.bot._ensure_tool_store().put_observation(
            "cap" if private else "news", key, text,
            "private:87654321" if private else "public:news",
            subject_id="87654321" if private else "", sensitivity="personal" if private else "public",
            url="" if private else "https://www.ithome.com/0/875/123.htm")

    async def learn(self, source, *, interests=(), knowledge=()):
        self.bot._ensure_growth()
        data = empty_growth()
        data["interests"] = [{"topic": topic, "delta": delta, "evidence_ids": [source["id"]]}
                             for topic, delta in interests]
        data["knowledge"] = [{"topic": topic, "statement": source["text"], "confidence": .9,
                              "evidence_ids": [source["id"]]} for topic in knowledge]
        self.bot._growth.enqueue([source])
        self.bot.ctx.llm.generate.side_effect = [model_response(data), model_response(review(data))]
        await self.bot._grow()
        self.assertEqual(self.bot._growth_runtime.last_status, "learned")

    def story(self, key, title, *, age=0):
        stamp = time.time()-age
        self.bot._store.save_stories([Story(key, "公开资料", title, "https://example.org/"+key, "材料摘要", stamp)])
        with self.bot._store.con:
            self.bot._store.con.execute("UPDATE stories SET first_seen=? WHERE key=?", (stamp, key))

    async def test_complete_preferences_rank_before_cutoff_and_survive_restart(self):
        source = self.source("photography", "摄影资料介绍画面构图与光线的变化。")
        await self.learn(source, interests=[("摄影", .05)])
        for index in range(7):
            source = self.source("science-"+str(index), "一份资料说明样本处理与观测方法。")
            await self.learn(source, knowledge=["样本"+str(index), "观测"+str(index)])
        self.assertFalse(any(row["kind"] == "interest" for row in self.bot._derived_memories()))
        self.story("favorite", "摄影光线实验", age=10)
        self.story("middle", "城市道路公告", age=5)
        self.story("latest", "常规产业简报")
        await self.bot.on_unload()
        self.bot = self.make_bot()
        self.assertEqual((await self.bot._choose_story(TARGET))["key"], "favorite")
        self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()

    async def test_negative_preference_outweighs_initial_keywords_and_may_skip(self):
        source = self.source("games", "游戏资料介绍竞争与胜负带来的感受。")
        await self.learn(source, interests=[("游戏", -.1)])
        self.bot.config.explorer.interests = ["游戏"]
        self.story("game", "游戏排行榜")
        self.story("neutral", "海洋观测记录", age=1)
        self.assertEqual((await self.bot._choose_story(TARGET))["key"], "neutral")
        self.bot._store.record_attempt(TARGET, time.time(), "explore", "neutral")
        self.assertIsNone(await self.bot._choose_story(TARGET))

    async def test_initial_keywords_still_guide_selection_without_learned_preferences(self):
        self.bot.config.explorer.interests = ["音乐"]
        self.story("music", "音乐活动", age=3)
        self.story("other", "公园通告")
        self.assertEqual((await self.bot._choose_story(TARGET))["key"], "music")

    async def test_stronger_interest_ranks_first_and_legacy_cognition_guides_unmatched_stories(self):
        source = self.source("arts", "摄影资料介绍光线，音乐资料介绍旋律。")
        await self.learn(source, interests=[("摄影", .1), ("音乐", .01)])
        self.story("photo", "摄影光线实验", age=3)
        self.story("music", "音乐创作")
        self.assertEqual((await self.bot._choose_story(TARGET))["key"], "photo")
        self.bot._store.record_attempt(TARGET, time.time(), "explore", "photo")
        self.bot._store.record_attempt(TARGET, time.time(), "explore", "music")
        self.bot.config.cognition.enabled = True
        self.bot.config.cognition.mode = "active"
        self.bot._ensure_cognition = lambda: SimpleNamespace(interest_hint=lambda **kwargs: "天文")
        self.story("sky", "天文观测报告", age=3)
        self.story("notice", "社区活动公告")
        self.assertEqual((await self.bot._choose_story(TARGET))["key"], "sky")

    async def test_model_gets_bounded_candidates_and_public_preference_fields_only(self):
        source = self.source("photography", "摄影资料介绍画面构图与光线的变化。")
        await self.learn(source, interests=[("摄影", .05)])
        private = self.source("private", "另一位用户私下谈过蓝信封手稿。", private=True)
        await self.learn(private, interests=[("蓝信封手稿", .1)])
        self.story("favorite", "摄影光线实验", age=5)
        self.story("latest", "常规简报")
        self.bot.config.brain.use_llm_selection = True
        async def choose(prompt, **kwargs):
            rows = json.loads(prompt.splitlines()[-1])
            self.assertEqual([row["key"] for row in rows], ["favorite"])
            self.assertIn('"topic": "摄影"', prompt)
            self.assertNotIn("蓝信封", prompt)
            self.assertNotIn(source["text"], prompt)
            self.assertNotIn(source["id"], prompt)
            return model_response({"share": True, "key": "favorite"})
        self.bot.ctx.llm.generate.side_effect = choose
        self.assertEqual((await self.bot._choose_story(TARGET))["key"], "favorite")

    async def test_retracted_preference_during_model_await_discards_old_selection(self):
        source = self.source("photography", "摄影资料介绍画面构图与光线的变化。")
        await self.learn(source, interests=[("摄影", .05)])
        self.story("favorite", "摄影光线实验")
        self.bot.config.brain.use_llm_selection = True
        async def choose(prompt, **kwargs):
            self.bot._tool_store.retract_observation("news", "photography")
            return model_response({"share": True, "key": "favorite"})
        self.bot.ctx.llm.generate.side_effect = choose
        self.assertIsNone(await self.bot._choose_story(TARGET))

    async def test_preference_weight_change_during_selection_discards_the_old_choice(self):
        source = self.source("photography", "摄影资料介绍画面构图与光线的变化。")
        await self.learn(source, interests=[("摄影", .05)])
        self.story("favorite", "摄影光线实验")
        self.bot.config.brain.use_llm_selection = True
        async def choose(prompt, **kwargs):
            new = self.source("photography-update", "新的摄影资料介绍了旧构图方式的局限。")
            await self.learn(new, interests=[("摄影", -.1)])
            return model_response({"share": True, "key": "favorite"})
        self.bot.ctx.llm.generate.side_effect = choose
        self.assertIsNone(await self.bot._choose_story(TARGET))

    async def test_blocked_attempted_and_outdated_stories_stay_ineligible(self):
        source = self.source("photography", "摄影资料介绍画面构图与光线的变化。")
        await self.learn(source, interests=[("摄影", .05)])
        self.bot.config.explorer.candidate_count = 3
        self.bot.config.explorer.blocked_words = ["广告"]
        self.story("blocked", "摄影广告")
        self.story("attempted", "摄影心得")
        self.story("old", "摄影旧闻", age=365*86400)
        self.story("normal", "海洋观测记录")
        self.bot._store.record_attempt(TARGET, time.time(), "explore", "attempted")
        self.assertEqual((await self.bot._choose_story(TARGET))["key"], "normal")
