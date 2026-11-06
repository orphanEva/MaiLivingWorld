"""Real plugin recall paths with temporary ledgers and synthetic model replies."""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

from MaiLivingWorld.tests import test_unified_runtime as fixture
from MaiLivingWorld.tests import test_visual_plugin as media_fixture
from MaiLivingWorld.tests.test_unified_runtime import host_message, kwargs, rendered, response
from MaiLivingWorld.tests.test_growth_runtime import review


class MemoryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.UnifiedRuntimeTests.asyncSetUp
    asyncTearDown = fixture.UnifiedRuntimeTests.asyncTearDown
    make_bot = fixture.UnifiedRuntimeTests.make_bot
    event = fixture.UnifiedRuntimeTests.event
    group_capture = fixture.UnifiedRuntimeTests.group_capture

    def configure_growth(self):
        self.bot.config.growth.enabled = True
        self.bot.config.growth.mode = "active"
        self.bot.config.unified_mind.semantic_recall = True
        self.bot.config.unified_mind.max_daily_model_calls = 100
        self.bot._ensure_growth()

    def seed(self, count=33, *, beliefs=False):
        self.configure_growth()
        for index in range(count):
            topic = "陶艺" if index == 0 else "天文方向" + str(index)
            identity = self.event("曾阅读有关" + topic + "的材料并产生兴趣", identity="archive-" + str(index))
            record = self.bot._resolve_growth_roots([identity])[identity]
            data = {"interests": [{"topic": topic, "delta": .1, "evidence_ids": [identity]}]}
            if beliefs:
                data["beliefs"] = [{"topic": topic, "statement": "学习陶艺需要练习", "confidence": .6,
                                    "evidence_ids": [identity]}]
            job_id = self.bot._growth.enqueue([record], now=100 + index * 2)
            job = self.bot._growth.claim(now=100 + index * 2)
            self.bot._growth.complete(job_id, job["token"], data, [record], now=101 + index * 2, archive_overflow=True)

    def model_says(self, identity):
        async def generate(prompt, **options):
            if "内部检索词" in prompt:
                return {"content": json.dumps({"queries": ["陶艺"]})}
            return response([{"id": identity, "mode": "say"}])
        self.bot.ctx.llm.generate.side_effect = generate

    async def test_archive_recall_survives_new_session_restart_and_source_withdrawal(self):
        self.seed()
        memories = self.bot._collect_unified("以前研究陶艺吗")
        archived = next(record for record in memories if record.get("archived"))
        self.assertEqual(archived["kind"], "historical_interest")
        self.assertFalse(any(record.get("archived") for record in self.bot._derived_memories()))
        self.model_says(archived["id"])
        await self.bot.capture_unified(message=host_message("archive-question", text="以前研究陶艺吗"))
        first = await self.bot.brain_reply_hint(**kwargs(mid="archive-question"))
        self.assertIn("归档时的成长记录", rendered(first))
        await self.bot.on_unload()
        self.bot = self.make_bot()
        self.configure_growth()
        self.model_says(archived["id"])
        await self.bot.capture_unified(message=host_message("archive-new-session", sid="new-session", text="以前研究陶艺吗"))
        second = await self.bot.brain_reply_hint(**kwargs("new-session", "archive-new-session"))
        self.assertIn("归档时的成长记录", rendered(second))
        with self.bot._brain.con:
            self.bot._brain.con.execute("DELETE FROM brain_events WHERE id=?", ("archive-0",))
        self.assertEqual(len(self.bot._growth.archived()), 1)
        self.assertFalse(any(record["id"] == archived["id"] for record in self.bot._derived_memories("陶艺")))
        await self.bot.capture_unified(message=host_message("archive-withdrawn", sid="new-session", text="以前研究陶艺吗"))
        withdrawn = await self.bot.brain_reply_hint(**kwargs("new-session", "archive-withdrawn"))
        self.assertNotIn("归档时的成长记录", rendered(withdrawn))

    async def test_private_archive_keeps_group_expression_boundaries(self):
        self.seed()
        archived = next(record for record in self.bot._derived_memories("陶艺") if record.get("archived"))
        self.model_says(archived["id"])
        await self.group_capture("archive-group", text="以前研究陶艺吗")
        result = await self.bot.brain_reply_hint(**kwargs("group-one", "archive-group"))
        self.assertNotIn(archived["text"], rendered(result))
        prompts = [call.args[0] for call in self.bot.ctx.llm.generate.await_args_list]
        self.assertTrue(any(archived["text"] in prompt for prompt in prompts))

    async def test_growth_runtime_enables_archive_without_an_additional_setting(self):
        self.seed(count=32)
        self.bot.config.unified_mind.semantic_recall = False
        identity = self.event("读到新的木工教程，想继续了解木工", identity="archive-runtime")
        data = {"interests": [{"topic": "木工", "delta": .1, "evidence_ids": [identity]}],
                "knowledge": [], "questions": [], "beliefs": [], "traits": [], "mood": {}}
        self.bot.ctx.llm.generate.side_effect = [
            {"content": json.dumps(data, ensure_ascii=False)},
            {"content": json.dumps(review(data), ensure_ascii=False)}]
        await self.bot._grow()
        self.assertEqual(self.bot._growth_runtime.last_status, "learned")
        self.assertEqual(self.bot._growth.capacity_counts()["interests"]["archived"], 1)
        self.assertTrue(any(item["topic"] == "木工" for item in self.bot._growth.state()["interests"]))

    async def test_semantic_default_interval_allows_same_turn_review_but_blocks_next_turn(self):
        self.bot.config.unified_mind.semantic_recall = True
        self.bot.config.unified_mind.min_interval_seconds = 30
        identity = self.event("以前阅读过陶艺制作方法")
        self.model_says(identity)
        await self.bot.capture_unified(message=host_message("first", text="陶艺"))
        first = await self.bot.brain_reply_hint(**kwargs(mid="first"))
        self.assertIn("以前阅读过陶艺制作方法", rendered(first))
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 2)
        self.assertEqual(self.bot._self_state.state()["thought_count"], 2)
        await self.bot.capture_unified(message=host_message("second", text="陶艺的制作"))
        second = await self.bot.brain_reply_hint(**kwargs(mid="second"))
        self.assertNotIn("以前阅读过陶艺制作方法", rendered(second))
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 2)
        self.assertEqual(self.bot._self_state.state()["thought_count"], 2)

    async def test_semantic_optional_call_leaves_last_daily_call_for_expression(self):
        self.bot.config.unified_mind.semantic_recall = True
        self.bot.config.unified_mind.min_interval_seconds = 30
        self.bot.config.unified_mind.max_daily_model_calls = 1
        identity = self.event("以前阅读过陶艺制作方法")
        self.model_says(identity)
        await self.bot.capture_unified(message=host_message("only", text="陶艺"))
        result = await self.bot.brain_reply_hint(**kwargs(mid="only"))
        self.assertIn("以前阅读过陶艺制作方法", rendered(result))
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 1)
        self.assertNotIn("内部检索词", self.bot.ctx.llm.generate.call_args.args[0])

    async def test_failed_semantic_call_can_continue_with_normal_recall_in_same_turn(self):
        self.bot.config.unified_mind.semantic_recall = True
        self.bot.config.unified_mind.min_interval_seconds = 30
        identity = self.event("以前阅读过陶艺制作方法")
        self.bot.ctx.llm.generate.side_effect = [TimeoutError(), response([{"id": identity, "mode": "say"}])]
        await self.bot.capture_unified(message=host_message("fallback", text="陶艺"))
        result = await self.bot.brain_reply_hint(**kwargs(mid="fallback"))
        self.assertIn("以前阅读过陶艺制作方法", rendered(result))
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 2)

    async def test_concurrent_same_reply_uses_one_semantic_reservation(self):
        self.bot.config.unified_mind.semantic_recall = True
        self.bot.config.unified_mind.min_interval_seconds = 30
        identity = self.event("以前阅读过陶艺制作方法")
        started, release = asyncio.Event(), asyncio.Event()
        async def generate(prompt, **options):
            if "内部检索词" in prompt:
                started.set()
                await release.wait()
                return {"content": '{"queries":["陶艺"]}'}
            return response([{"id": identity, "mode": "say"}])
        self.bot.ctx.llm.generate.side_effect = generate
        await self.bot.capture_unified(message=host_message("concurrent", text="陶艺"))
        first = asyncio.create_task(self.bot.brain_reply_hint(**kwargs(mid="concurrent")))
        try:
            await asyncio.wait_for(started.wait(), 2)
            await self.bot.brain_reply_hint(**kwargs(mid="concurrent"))
        finally:
            release.set()
        result = await first
        self.assertIn("以前阅读过陶艺制作方法", rendered(result))
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 2)
        self.assertEqual(self.bot._self_state.state()["thought_count"], 2)

    async def test_cached_semantic_query_cannot_bypass_interval_after_source_edit(self):
        self.bot.config.unified_mind.semantic_recall = True
        self.bot.config.unified_mind.min_interval_seconds = 30
        identity = self.event("以前阅读过陶艺制作方法")
        self.model_says(identity)
        await self.bot.capture_unified(message=host_message("cached", text="陶艺"))
        request = kwargs(mid="cached")
        await self.bot.brain_reply_hint(**request)
        self.assertIn("以前阅读过陶艺制作方法", rendered(await self.bot.brain_reply_hint(**request)))
        with self.bot._brain.con:
            self.bot._brain.con.execute("UPDATE brain_events SET summary=? WHERE id=?", ("更正后的陶艺资料", "E1"))
        result = await self.bot.brain_reply_hint(**request)
        self.assertNotIn("以前阅读过陶艺制作方法", rendered(result))
        self.assertNotIn("更正后的陶艺资料", rendered(result))
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 2)

    async def test_retired_archive_during_expression_wait_is_not_exposed(self):
        self.seed()
        archived = next(record for record in self.bot._derived_memories("陶艺") if record.get("archived"))
        async def generate(prompt, **options):
            if "内部检索词" in prompt:
                return {"content": '{"queries":["陶艺"]}'}
            old = self.bot._growth.archived()[0]["item"]
            self.bot._growth.retire(old["key"], self.bot._growth.state()["revision"])
            return response([{"id": archived["id"], "mode": "say"}])
        self.bot.ctx.llm.generate.side_effect = generate
        await self.bot.capture_unified(message=host_message("retiring", text="以前研究陶艺吗"))
        result = await self.bot.brain_reply_hint(**kwargs(mid="retiring"))
        self.assertNotIn("归档时的成长记录", rendered(result))
        self.assertEqual(self.bot._unified.last_status, "context_changed")

    async def test_history_cache_includes_limit_and_store_identity(self):
        self.configure_growth()
        with patch.object(self.bot._growth, "history", wraps=self.bot._growth.history) as history:
            self.bot._recent_growth_history(0)
            self.bot._recent_growth_history(0)
            self.assertEqual(history.call_count, 1)
            history.assert_called_with(limit=100)
            self.bot.config.unified_mind.semantic_recall = False
            self.bot._recent_growth_history(0)
            history.assert_called_with(limit=20)
            self.bot.config.unified_mind.semantic_recall = True
            self.bot._recent_growth_history(0)
            history.assert_called_with(limit=100)
            self.assertEqual(history.call_count, 3)

    async def test_learning_preset_chains_dialogue_semantics_and_expression_at_default_interval(self):
        self.bot.set_plugin_config({"owner": {"setup_mode": "陪伴与成长", "qq": "12345678"}})
        self.assertEqual(self.bot.config.unified_mind.min_interval_seconds, 30)
        identity = self.event("工作时曾用写清单的方法理清步骤")
        async def generate(prompt, **options):
            if "UNTRUSTED_DIALOGUE_INPUT" in prompt:
                return {"content": json.dumps({"mode": "listen", "confidence": .95,
                    "evidence": "我只是想吐槽一下，你先别给建议"}, ensure_ascii=False)}
            if "内部检索词" in prompt:
                return {"content": '{"queries":["工作 步骤"]}'}
            return response([{"id": identity, "mode": "say"}])
        self.bot.ctx.llm.generate.side_effect = generate
        text = "今天工作上的事一件接一件，弄得我很烦。我只是想吐槽一下，你先别给建议。"
        await self.bot.capture_unified(message=host_message("learning", text=text))
        result = await self.bot.brain_reply_hint(**kwargs(mid="learning"))
        self.assertIn("工作时曾用写清单的方法理清步骤", rendered(result))
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 3)
        self.assertEqual(self.bot._self_state.state()["thought_count"], 3)
        self.assertEqual(self.bot._unified.last_status, "ready")

    async def test_archived_belief_reenters_review_with_all_original_sources(self):
        self.seed(beliefs=True)
        self.bot.config.growth.review_beliefs = True
        old = next(entry["item"] for entry in self.bot._growth.archived() if entry["category"] == "beliefs")
        identity = self.event("今天的学习说明学习陶艺需要练习", identity="archive-belief-return")
        data = {"interests": [], "knowledge": [], "questions": [], "traits": [], "mood": {},
                "beliefs": [{"topic": "陶艺", "statement": old["text"], "confidence": .7,
                    "relation": "reinforce", "previous_key": old["key"], "previous_revision": old["revision"],
                    "evidence_ids": [identity], "quotes": [{"source_id": identity, "quote": "学习陶艺需要练习"}]}]}
        self.bot.ctx.llm.generate.side_effect = [
            {"content": json.dumps(data, ensure_ascii=False)},
            {"content": json.dumps(review(data), ensure_ascii=False)}]
        await self.bot._grow()
        prompt = self.bot.ctx.llm.generate.await_args_list[0].args[0]
        given = json.loads(prompt.split("UNTRUSTED_GROWTH_INPUT\n", 1)[1])
        self.assertEqual(self.bot._growth_runtime.last_status, "learned", given)
        restored = next(item for item in self.bot._growth.state()["beliefs"] if item["key"] == old["key"])
        self.assertEqual(restored["revision"], 2)
        self.assertEqual({ref["id"] for ref in restored["sources"]}, {identity, old["sources"][0]["id"]})
        current = json.loads(prompt.split("UNTRUSTED_GROWTH_INPUT\n", 1)[1])["current_self"]
        self.assertTrue(any(item["key"] == old["key"] for item in current["beliefs"]))


class MemoryMediaIntegrationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = media_fixture.VisualPluginTests.asyncSetUp
    asyncTearDown = media_fixture.VisualPluginTests.asyncTearDown
    new_bot = media_fixture.VisualPluginTests.new_bot
    receive = media_fixture.VisualPluginTests.receive
    reply = media_fixture.VisualPluginTests.reply

    async def test_media_ticket_chains_semantics_and_expression_once_at_default_interval(self):
        self.bot.config.unified_mind.min_interval_seconds = 30
        self.bot.config.unified_mind.semantic_recall = True
        self.bot.config.brain_memory.enabled = True
        fixture.UnifiedRuntimeTests.event(self, "我阅读过橘猫的生活习性", identity="cat-memory")
        original = self.bot.ctx.llm.generate.side_effect
        async def generate(prompt, **options):
            if isinstance(prompt, str) and "内部检索词" in prompt:
                return {"content": '{"queries":["橘猫"]}'}
            return await original(prompt, **options)
        self.bot.ctx.llm.generate.side_effect = generate
        request = await self.receive(media_fixture.media_event("ticket-chain", text="这只猫在做什么"))
        self.assertIn(media_fixture.OBSERVATION, await self.reply(request))
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 3,
            (self.bot._unified.last_status, [(call.kwargs.get("task_name"), str(call.args[0])[:80])
                                             for call in self.bot.ctx.llm.generate.await_args_list]))
        self.assertEqual(self.bot._self_state.state()["thought_count"], 3)
        self.assertEqual(self.bot._unified.last_status, "ready")
        self.assertIn(media_fixture.OBSERVATION, await self.reply(request))
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 3)
