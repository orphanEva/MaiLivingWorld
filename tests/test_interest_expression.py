"""Current learned preferences reach ordinary, authenticated QQ replies."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from MaiLivingWorld.growth_model import empty_growth
from MaiLivingWorld.social_discovery import interest_question, reading_reaction_question
from MaiLivingWorld.tests.test_growth_runtime import model_response, review
from MaiLivingWorld.tests.test_mcp_plugin import GROUP, OTHER, OWNER, message
from MaiLivingWorld.tests.test_runtime import FakeContext, plugin


PHOTO = "摄影展资料介绍了构图、光线与色彩如何共同影响画面的表达。"
MUSIC = "音乐节资料介绍了高音量演出和通宵活动的安排。"


class InterestExpressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bot = self.make_bot()
        self.photo = self.source("photo", PHOTO)
        self.music = self.source("music", MUSIC)
        await self.learn(interests=[("摄影", 0.01, self.photo), ("高音量音乐节", -0.1, self.music)])

    def make_bot(self):
        bot = plugin.MaiLivingWorld()
        bot.ctx = FakeContext()
        bot.ctx.paths = SimpleNamespace(data_dir=Path(self.temp.name))
        bot.config = plugin.Config()
        bot.config.owner.enabled = bot.config.unified_mind.enabled = bot.config.growth.enabled = True
        bot.config.owner.qq = OWNER
        bot.config.unified_mind.mode = bot.config.growth.mode = "active"
        bot.config.unified_mind.min_interval_seconds = 0
        bot.config.unified_mind.max_daily_model_calls = 100
        bot.config.brain_memory.private_users = [OTHER]
        bot.config.brain_memory.group_ids = [GROUP]
        bot.config.mcp.enabled = bot.config.mcp.public_sources_enabled = True
        bot.config.reference.enabled = True
        return bot

    async def asyncTearDown(self):
        await self.bot.on_unload()
        self.temp.cleanup()

    def source(self, key, text, *, private=False):
        return self.bot._ensure_tool_store().put_observation(
            "cap" if private else "news", key, text,
            "private:"+OTHER if private else "public:news",
            subject_id=OTHER if private else "", sensitivity="personal" if private else "public",
            url="" if private else "https://www.ithome.com/0/875/123.htm")

    async def learn(self, *, interests=(), knowledge=()):
        self.bot._ensure_growth()
        data = empty_growth()
        data["interests"] = [{"topic": topic, "delta": delta, "evidence_ids": [source["id"]]}
                             for topic, delta, source in interests]
        data["knowledge"] = [{"topic": topic, "statement": text, "confidence": 0.9,
                              "evidence_ids": [source["id"]]} for topic, text, source in knowledge]
        records = {entry[-1]["id"]: entry[-1] for entry in (*interests, *knowledge)}
        self.bot._growth.enqueue(list(records.values()))
        self.bot.ctx.llm.generate.side_effect = [model_response(data), model_response(review(data))]
        await self.bot._grow()
        self.assertEqual(self.bot._growth_runtime.last_status, "learned")

    async def reply(self, text, mid="current", *, event=None, mode="say", sid="private-one", on_expression=None):
        event = event or {**message(mid, sid=sid, text=text), "raw_message": [{"type": "text", "data": text}]}
        await self.bot.capture_unified(message=event)
        self.memories = []
        async def expression(prompt, **options):
            data = json.loads(prompt.rsplit("UNTRUSTED_INPUT_JSON\n", 1)[1])
            self.memories.extend(data["memories"])
            if on_expression:
                on_expression()
            return model_response({"behavior": "neutral", "decisions": [
                {"id": row["id"], "mode": mode} for row in data["memories"]]})
        self.bot.ctx.llm.generate.side_effect = expression
        result = await self.bot.brain_reply_hint(session_id=event["session_id"], reply_message_id=event["message_id"],
            messages=[{"role": "system", "content": "宿主提示"}])
        return json.dumps(result, ensure_ascii=False)

    async def test_positive_and_negative_questions_select_the_corresponding_learned_direction(self):
        positive = await self.reply("你最近喜欢什么？")
        self.assertIn("自己的兴趣倾向：摄影（目前更愿意了解和接触）", positive)
        self.assertIn(PHOTO, positive)
        self.assertNotIn("高音量音乐节", positive)
        self.assertNotIn("偏好强度", positive)
        negative = await self.reply("你有没有不喜欢的？", "negative")
        self.assertIn("自己的兴趣倾向：高音量音乐节（目前不太感兴趣）", negative)
        self.assertIn(MUSIC, negative)
        self.assertNotIn("自己的兴趣倾向：摄影", negative)

    async def test_other_knowledge_cannot_crowd_out_preferences_after_restart(self):
        for index in range(7):
            text = f"第{index}份地质资料记载了岩石样本的成分与观测方法。"
            source = self.source("geology-"+str(index), text)
            await self.learn(knowledge=[(f"岩石成分{index}", text, source), (f"地质观测{index}", text, source)])
        self.assertFalse(any(row["kind"] == "interest" for row in self.bot._derived_memories("你有什么爱好")))
        await self.bot.on_unload()
        self.bot = self.make_bot()
        visible = await self.reply("你有什么爱好？", sid="new-private")
        self.assertIn("自己的兴趣倾向：摄影", visible)
        self.assertIn(PHOTO, visible)
        self.assertNotIn("岩石", visible)

    async def test_retracted_and_unapproved_preferences_are_not_outbound(self):
        omitted = await self.reply("你喜欢什么", mode="omit")
        self.assertNotIn("自己的兴趣倾向：摄影", omitted)
        self.assertNotIn(PHOTO, omitted)
        self.bot._tool_store.retract_observation("news", "photo")
        absent = await self.reply("你喜欢什么", "retracted")
        self.assertNotIn("自己的兴趣倾向：摄影", absent)
        self.assertNotIn(PHOTO, absent)
        self.assertIn("还没形成明确的偏好", absent)
        self.assertNotIn("高音量音乐节", absent)

    async def test_private_source_and_its_derived_interest_remain_internal(self):
        source = self.source("private", "另一个人私下分享了她珍藏的蓝色手稿。", private=True)
        await self.learn(interests=[("蓝色手稿", 0.08, source)])
        visible = await self.reply("你有什么爱好")
        self.assertIn("蓝色手稿", json.dumps(self.memories, ensure_ascii=False))
        self.assertNotIn("蓝色手稿", visible)
        self.assertIn("自己的兴趣倾向：摄影", visible)

    async def test_only_exact_authored_owner_private_input_sets_interest_marker(self):
        text = "你最近喜欢什么"
        for kind in ("forward", "different", "missing", "reply", "group", "other", "quoted"):
            event = {**message("invalid-"+kind, text=text), "raw_message": [{"type": "text", "data": text}]}
            if kind == "forward":
                event["raw_message"] = [{"type": "forward", "data": text}]
            elif kind == "different":
                event["raw_message"][0]["data"] = "转述朋友的问题"
            elif kind == "missing":
                event.pop("raw_message")
            elif kind == "reply":
                event["reply_to"] = "other-message"
            elif kind == "group":
                event["message_info"]["group_info"] = {"group_id": GROUP}
            elif kind == "other":
                event["message_info"]["user_info"]["user_id"] = OTHER
            else:
                event["processed_plain_text"] = event["raw_message"][0]["data"] = "朋友问“你最近喜欢什么”"
            visible = await self.reply(text, event=event)
            context = self.bot._unified.context({"session_id": event["session_id"], "reply_message_id": event["message_id"]})
            self.assertNotIn("interest_question", context or {}, kind)
            self.assertNotIn("主人在问你自己的喜好", visible, kind)

    async def test_minimum_recall_limit_keeps_preference_without_fabricating_reason(self):
        for limit in (1, 2):
            self.bot.config.unified_mind.recall_limit = limit
            visible = await self.reply("你喜欢什么", "limited-"+str(limit))
            self.assertEqual(len(self.memories), limit)
            self.assertIn("自己的兴趣倾向：摄影", visible)
            self.assertEqual(PHOTO in visible, limit == 2)
            self.assertIn("原因不清楚就保留不确定", visible)

    async def test_reaction_to_actually_sent_material_retains_its_learned_preference(self):
        await self.reply("你最近喜欢什么", "before")
        await self.bot.reference_send_feedback(sent=True,
            message=message("sent", uid="99999999", text=PHOTO))
        self.assertTrue(self.bot._reference.store.state(OWNER)["active"])
        await self.bot.on_unload()
        self.bot = self.make_bot()
        visible = await self.reply("你为什么喜欢这个？", sid="new-private")
        self.assertIn(PHOTO, visible)
        self.assertIn("自己的兴趣倾向：摄影", visible)
        self.assertIn("还没形成看法", visible)
        self.bot._tool_store.retract_observation("news", "photo")
        unavailable = await self.reply("你为什么喜欢这个", "retracted")
        self.assertNotIn(PHOTO, unavailable)
        self.assertNotIn("自己的兴趣倾向：摄影", unavailable)
        self.assertIn("先简短问清", unavailable)

    async def test_negative_reaction_keeps_dislike_and_multiple_sources_require_clarification(self):
        await self.reply("你有没有不喜欢的", "before-negative")
        await self.bot.reference_send_feedback(sent=True,
            message=message("negative-sent", uid="99999999", text=MUSIC))
        negative = await self.reply("你为什么不喜欢它", "negative-why")
        self.assertIn("自己的兴趣倾向：高音量音乐节（目前不太感兴趣）", negative)
        self.assertIn(MUSIC, negative)
        await self.reply("你有什么喜好", "both")
        await self.bot.reference_send_feedback(sent=True,
            message=message("both-sent", uid="99999999", text=PHOTO+" "+MUSIC))
        ambiguous = await self.reply("你为什么喜欢这个", "ambiguous")
        self.assertIn("先简短问清", ambiguous)
        self.assertNotIn("自己的兴趣倾向", ambiguous)

    async def test_quoted_reaction_cannot_reuse_a_preference_source(self):
        for kind in ("forward", "different", "missing", "reply"):
            await self.reply("你喜欢什么", "before-"+kind)
            await self.bot.reference_send_feedback(sent=True,
                message=message("sent-"+kind, uid="99999999", text=PHOTO))
            text = "你为什么喜欢这个"
            event = {**message("quoted-"+kind, text=text), "raw_message": [{"type": "text", "data": text}]}
            if kind == "forward":
                event["raw_message"] = [{"type": "forward", "data": text}]
            elif kind == "different":
                event["raw_message"][0]["data"] = "朋友问的问题"
            elif kind == "missing":
                event.pop("raw_message")
            else:
                event["reply_to"] = "earlier"
            visible = await self.reply(text, event=event)
            self.assertNotIn(PHOTO, visible, kind)
            self.assertNotIn("自己的兴趣倾向", visible, kind)

    async def test_source_retracted_during_expression_invalidates_pending_interest(self):
        visible = await self.reply("你喜欢什么", on_expression=lambda:
            self.bot._tool_store.retract_observation("news", "photo"))
        self.assertEqual(self.bot._unified.last_status, "context_changed")
        self.assertNotIn(PHOTO, visible)
        self.assertNotIn("自己的兴趣倾向：摄影", visible)

    async def test_neutral_and_observe_only_growth_do_not_become_positive_interests(self):
        source = self.source("photo-neutral", "另一份摄影资料比较了不同构图的局限。")
        await self.learn(interests=[("摄影", -0.01, source)])
        neutral = await self.reply("你喜欢什么")
        self.assertNotIn("自己的兴趣倾向", neutral)
        self.bot.config.growth.mode = "observe"
        observed = await self.reply("你有没有不喜欢的", "observe")
        self.assertNotIn("自己的兴趣倾向", observed)


class InterestQuestionTests(unittest.TestCase):
    def test_short_questions_are_whole_utterances(self):
        self.assertEqual(interest_question(" 你最近喜欢什么？！ "), "positive")
        self.assertEqual(interest_question("有没有不喜欢的"), "negative")
        self.assertEqual(interest_question("有没有不喜欢的东西？"), "negative")
        self.assertEqual(interest_question("你有什么喜好"), "mixed")
        for text in ("我喜欢什么", "她问你喜欢什么", "“你喜欢什么”", "你喜欢什么都可以", None, "你"*81):
            self.assertEqual(interest_question(text), "")
        for text in ("你为什么喜欢这个？", "你为什么不喜欢它"):
            self.assertTrue(reading_reaction_question(text))
        self.assertFalse(reading_reaction_question("她问你为什么喜欢这个"))
