"""Real plugin receive/reply/send paths with disposable stores and offline models."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from MaiLivingWorld.tests.test_runtime import FakeContext, plugin
from MaiLivingWorld.tests.test_mcp_plugin import message, OWNER, OTHER, GROUP


TOPIC = "准备星空摄影的拍摄计划需要先核对天气和月相。"
OTHER_TOPIC = "准备森林徒步的物品清单需要先确认路线和补水。"
ADVICE = "先把拍摄计划拆成三个小步骤，今天只处理第一个。"


class AdviceReferencePluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bot = self.make_bot()
        self.source = self.source_row("stars", TOPIC)
        self.other = self.source_row("forest", OTHER_TOPIC)
        self.prompts = []
        self.set_model()

    def make_bot(self):
        b = plugin.MaiLivingWorld()
        b.ctx = FakeContext()
        b.ctx.paths = SimpleNamespace(data_dir=Path(self.temp.name))
        b.config = plugin.Config()
        b.config.owner.enabled = b.config.unified_mind.enabled = True
        b.config.owner.qq = OWNER
        b.config.unified_mind.mode = "active"
        b.config.unified_mind.min_interval_seconds = 0
        b.config.advice.enabled = b.config.reference.enabled = b.config.mcp.enabled = True
        b.config.advice.auto_feedback = False
        b.config.brain_memory.private_users = [OTHER]
        b.config.brain_memory.group_ids = [GROUP]
        return b

    def source_row(self, key, text):
        return self.bot._ensure_tool_store().put_observation("pixiv", key, text, "private:"+OWNER, subject_id=OWNER)

    def set_model(self):
        async def model(prompt, **kwargs):
            data = json.loads(prompt.rsplit("\n", 1)[1])
            self.prompts.append(data)
            return {"response": json.dumps({"behavior": "neutral", "decisions": [
                {"id": row["id"], "mode": "say"} for row in data["memories"]]})}
        self.bot.ctx.llm.generate = AsyncMock(side_effect=model)

    async def capture(self, mid="in", text="聊聊星空摄影", **kwargs):
        event = message(mid=mid, text=text, **kwargs)
        await self.bot.capture_unified(message=event)
        return {"session_id": event["session_id"], "reply_message_id": event["message_id"]}

    async def reply(self, request):
        return await self.bot.brain_reply_hint(**request, messages=[{"role": "system", "content": "原系统提示"}])

    async def send_reference(self, text, mid="out", **kwargs):
        await self.bot.reference_send_feedback(sent=True, message=message(mid=mid, uid="99999999", text=text, **kwargs))

    async def prepare_advice(self):
        request = await self.capture("advice-in", "给我一点准备拍摄的建议")
        prepared = await self.bot.advice("prepare", "拍摄计划", ADVICE, [self.source["id"]], **request)
        self.assertTrue(prepared["success"], prepared)
        return prepared

    async def asyncTearDown(self):
        await self.bot.on_unload()
        self.temp.cleanup()

    async def test_verified_reference_survives_restart_and_only_recalls_its_topic(self):
        request = await self.capture()
        await self.reply(request)
        self.assertFalse(self.bot._reference.store.state(OWNER)["active"])
        await self.send_reference(TOPIC)
        self.assertTrue(self.bot._reference.store.state(OWNER)["active"])
        await self.bot.on_unload()
        self.bot = self.make_bot()
        self.set_model()
        request = await self.capture("return", "后来呢", sid="private-new")
        result = await self.reply(request)
        self.assertIn(TOPIC, str(result))
        self.assertNotIn(OTHER_TOPIC, str(result))
        self.assertEqual([r["id"] for r in self.prompts[-1]["memories"]], [self.source["id"]])
        self.assertEqual(self.bot._unified.context(request)["query"], "后来呢")
        self.assertIn("不补猜新的进展", str(result))

    async def test_unavailable_or_ambiguous_reference_does_not_search_unrelated_memories(self):
        request = await self.capture("vague", "那件事呢")
        result = await self.reply(request)
        self.assertIn("先简短问清", str(result))
        self.assertNotIn(TOPIC, str(result))
        self.bot.ctx.llm.generate.assert_not_awaited()
        request = await self.capture("both", "聊聊拍摄和徒步")
        await self.reply(request)
        await self.send_reference(TOPIC+OTHER_TOPIC)
        self.assertEqual(len(self.bot._reference.store.state(OWNER)["focus_refs"]), 2)
        previous = self.bot.ctx.llm.generate.await_count
        result = await self.reply(await self.capture("vague-again", "后来呢"))
        self.assertIn("先简短问清", str(result))
        self.assertEqual(self.bot.ctx.llm.generate.await_count, previous)
        self.assertNotIn(TOPIC, str(result))

    async def test_source_retraction_or_new_topic_cannot_resurrect_old_focus(self):
        await self.reply(await self.capture())
        await self.send_reference(TOPIC)
        self.bot._tool_store.retract_observation("pixiv", "stars")
        result = await self.reply(await self.capture("withdrawn", "继续说"))
        self.assertIn("先简短问清", str(result))
        self.assertNotIn(TOPIC, str(result))
        await self.capture("new-topic", "今天想谈谈做饭")
        self.assertFalse(self.bot._reference.store.state(OWNER)["active"])

    async def test_private_reference_is_not_inherited_by_group_or_another_person(self):
        await self.reply(await self.capture())
        await self.send_reference(TOPIC)
        request = await self.capture("group", "后来呢", uid=OTHER, sid="group-one", group=GROUP)
        result = await self.reply(request)
        self.assertNotIn(TOPIC, str(result))
        self.assertNotIn("MaiLivingWorld:reference", str(result))
        self.assertEqual(self.bot._reference.resolve(request)["status"], "untrusted")

    async def test_late_send_after_clear_never_marks_current_topic(self):
        await self.reply(await self.capture())
        async def while_awaiting(*args, **kwargs):
            await self.capture("clear", "/话题 清除")
            return {"stream_id": "private-one"}
        self.bot.ctx.chat.get_stream_by_user_id = AsyncMock(side_effect=while_awaiting)
        await self.send_reference(TOPIC)
        self.assertFalse(self.bot._reference.store.state(OWNER)["active"])

    async def test_advice_requires_delivery_then_owner_feedback_and_stays_derived(self):
        prepared = await self.prepare_advice()
        ident = prepared["data"]["id"]
        store = self.bot._advice.store
        await self.bot.advice_send_feedback(sent=False, message=message(mid="failed", uid="99999999", text=prepared["delivery_text"]))
        self.assertEqual(store.get(OWNER, ident)["status"], "draft")
        await self.bot.advice_send_feedback(sent=True, message=message(mid="delivered", uid="99999999", text=prepared["delivery_text"]))
        row = store.get(OWNER, ident)
        self.assertEqual(row["status"], "active")
        self.assertEqual(self.bot._advice.records(), [])
        await self.capture("unhelpful", f"/建议 没用 {ident} {row['revision']}")
        records = self.bot._collect_unified("拍摄计划")
        learned = next(r for r in records if r["source"] == "advice")
        self.assertIn("没有帮助", learned["text"])
        self.assertEqual(learned["evidence_status"], "owner_reported")
        self.assertEqual(learned["sensitivity"], "personal")
        self.assertFalse(any(r["source"] == "advice" for r in self.bot._collect_roots("拍摄计划")))
        with self.assertRaises(ValueError):
            self.bot._resolve_roots([learned["id"]])
        await self.bot.on_unload()
        self.bot = self.make_bot()
        self.assertEqual(self.bot._ensure_advice().records()[0]["evidence_status"], "owner_reported")

    async def test_forgetting_advice_removes_result_and_old_feedback_cannot_restore_it(self):
        prepared = await self.prepare_advice()
        await self.bot.advice_send_feedback(sent=True, message=message(mid="advice-sent", uid="99999999", text=prepared["delivery_text"]))
        ident = prepared["data"]["id"]
        row = self.bot._advice.store.get(OWNER, ident)
        old = f"/建议 有用 {ident} {row['revision']}"
        await self.capture("report", old)
        row = self.bot._advice.store.get(OWNER, ident)
        await self.capture("forget", f"/建议 忘记 {ident} {row['revision']}")
        await self.capture("report", old)
        self.assertEqual(self.bot._advice.store.get(OWNER, ident)["status"], "forgotten")
        self.assertEqual(self.bot._advice.records(), [])
        self.assertNotIn(ADVICE, str(await self.bot.advice("list", **(await self.capture("inspect", "看看建议状态")))))

    async def test_sources_are_discoverable_without_exposing_another_person(self):
        hidden = self.bot._ensure_tool_store().put_observation("pixiv", "other-private", "另一个人的私人拍摄计划与住址线索",
                                                              "private:"+OTHER, subject_id=OTHER)
        request = await self.capture("discover", "星空摄影")
        found = await self.bot.advice("sources", **request)
        self.assertTrue(found["success"], found)
        ids = [row["id"] for row in found["data"]]
        self.assertIn(self.source["id"], ids)
        self.assertNotIn(hidden["id"], ids)
        prepared = await self.bot.advice("prepare", "拍摄计划", ADVICE, [self.source["id"]], **request)
        self.assertTrue(prepared["success"])
        self.bot.ctx.llm.generate.assert_not_awaited()

    async def test_shared_reading_protection_blocks_new_advice_source_and_late_delivery(self):
        b = self.bot
        b.config.reading.enabled = b.config.mcp_pixiv.enabled = True
        source = b._tool_store.put_observation("pixiv", "reading:novel:321:source", "版本一的作品资料",
            "private:"+OWNER, subject_id=OWNER, origin_key="work:novel:321")
        companion = b._ensure_companion()
        reading = companion.store.open_reading("pixiv", "novel", "321", "1", owner_id=OWNER,
            scope="private:"+OWNER, source_refs=[companion._ref(source)], total_units=100, title="未读作品")
        request = await self.capture("book-advice", "聊聊这份作品")
        prepared = await b.advice("prepare", "作品阅读", "可以先读开头的片段。", [source["id"]], **request)
        self.assertTrue(prepared["success"], prepared)
        b.config.shared_reading.enabled = True
        request = await self.capture("protect", "/共读 开始 "+reading["id"])
        await b.advice_send_feedback(sent=True, message=message(mid="late-book", uid="99999999", text=prepared["delivery_text"]))
        self.assertEqual(b._advice.store.get(OWNER, prepared["data"]["id"])["status"], "draft")
        request = await self.capture("book-again", "这份作品")
        found = await b.advice("sources", **request)
        self.assertNotIn(source["id"], str(found))
        denied = await b.advice("prepare", "作品阅读", "可以先读开头的片段。", [source["id"]], **request)
        self.assertFalse(denied["success"])
