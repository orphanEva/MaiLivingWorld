"""Current learned preferences reach ordinary, authenticated QQ replies."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from MaiLivingWorld.growth_model import empty_growth
from MaiLivingWorld.social_discovery import interest_followup_question, interest_question, reading_reaction_question
from MaiLivingWorld.tests.test_growth_runtime import model_response, review
from MaiLivingWorld.tests.test_growth_store import fill_interest_sources
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

    async def test_long_interest_survives_restart_and_bounded_questions_then_checks_last_source_again(self):
        await self.reply("你最近喜欢什么", "before-long-interest")
        await self.bot.reference_send_feedback(sent=True,
            message=message("photo-before-long-interest", uid="99999999", text=PHOTO))
        additional = [self.source("long-photo-"+str(index), f"第{index}份摄影资料讨论了构图和光线。")
                      for index in range(129)]
        fill_interest_sources(self.bot._growth, additional, "摄影")
        await self.bot.on_unload()
        self.bot = self.make_bot()
        current = next(row for row in self.bot._interest_memories() if row["topic"] == "摄影")
        self.assertEqual(len(current["sources"]), 130)
        self.assertTrue(any(row["topic"] == "摄影" for row in self.bot._growth_snapshot()["interests"]))
        for limit in (1, 2, 12):
            self.bot.config.unified_mind.recall_limit = limit
            visible = await self.reply("你现在还喜欢这个吗", "long-followup-"+str(limit), sid="long-new-session")
            self.assertIn("自己的兴趣倾向：摄影", visible)
            self.assertIn("兴趣有所回升", visible)
            self.assertLessEqual(len(self.memories), limit)
            self.assertEqual(sum(row["kind"] == "interest" for row in self.memories), 1)
            self.assertTrue(all("sources" not in row for row in self.memories))
        self.bot.config.unified_mind.recall_limit = 2
        ordinary = await self.reply("你最近喜欢什么", "long-general-question")
        self.assertIn("自己的兴趣倾向：摄影", ordinary)
        self.assertLessEqual(len(self.memories), 2)
        changed = await self.reply("你最近喜欢什么", "long-retracted-during-expression", on_expression=lambda:
            self.bot._tool_store.retract_observation("news", "long-photo-128"))
        self.assertNotIn("自己的兴趣倾向：摄影", changed)
        self.assertFalse(any(row["topic"] == "摄影" for row in self.bot._interest_memories()))

    async def test_late_private_source_keeps_a_long_interest_internal_until_old_source_is_withdrawn(self):
        additional = [self.source("private-photo-"+str(index), f"第{index}份公开资料记录了摄影的取景方式。")
                      for index in range(128)]
        private = self.source("private-photo-last", "另一个人私下描述了摄影带给她的心情。", private=True)
        fill_interest_sources(self.bot._growth, additional+[private], "摄影")
        current = next(row for row in self.bot._interest_memories() if row["topic"] == "摄影")
        self.assertEqual((len(current["sources"]), current["sensitivity"], current["subject_id"]), (130, "personal", ""))
        visible = await self.reply("你最近喜欢什么", "long-private-interest")
        self.assertTrue(any(row["kind"] == "interest" for row in self.memories))
        self.assertNotIn("自己的兴趣倾向：摄影", visible)
        self.assertNotIn(private["text"], visible)
        self.bot._tool_store.retract_observation("news", "photo")
        self.assertFalse(any(row["topic"] == "摄影" for row in self.bot._interest_memories()))

    async def test_three_interests_share_a_bounded_reply_after_all_dependencies_are_checked(self):
        for index, topic in enumerate(("摄影", "天文", "园艺")):
            sources = [self.source(f"several-{index}-{number}", f"{topic}资料记录了第{number}次观察。")
                       for number in range(49 if topic == "摄影" else 50)]
            fill_interest_sources(self.bot._growth, sources, topic)
        selected = [row for row in self.bot._interest_memories() if row["weight"] > 0]
        self.assertEqual(len(selected), 3)
        self.assertEqual(sum(len(row["sources"]) for row in selected), 150)
        self.bot.config.unified_mind.recall_limit = 6
        with patch.object(self.bot, "_resolve_roots", wraps=self.bot._resolve_roots) as resolve:
            visible = await self.reply("你最近喜欢什么", "three-interests")
        self.assertTrue(all(len(call.args[0]) <= 128 for call in resolve.call_args_list))
        self.assertEqual([row["id"] for row in self.memories[:3]], [row["id"] for row in selected])
        self.assertEqual(len(self.memories), 6)
        for topic in ("摄影", "天文", "园艺"):
            self.assertIn("自己的兴趣倾向："+topic, visible)

    async def test_growth_dependency_batches_deduplicate_in_order_and_keep_exact_read_limits(self):
        unique = ["brain:event:batch"+str(index) for index in range(257)]
        requested = unique[:140]+unique[:2]+unique[140:]
        def resolve(ids):
            return {sid: {"id": sid} for sid in ids if sid != unique[128]}
        with patch.object(self.bot, "_resolve_roots", side_effect=resolve) as reader:
            result = self.bot._resolve_growth_roots(requested)
            self.assertEqual([len(call.args[0]) for call in reader.call_args_list], [128, 128, 1])
            self.assertEqual([sid for call in reader.call_args_list for sid in call.args[0]], unique)
            self.assertEqual(list(result), [sid for sid in unique if sid != unique[128]])
        with self.assertRaisesRegex(ValueError, "invalid memory ids"):
            self.bot._resolve_roots(unique)

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

    async def test_interest_history_followup_checks_all_topics_sharing_one_source_before_limiting(self):
        detail = "现场资料比较了聚光灯、拍摄角度与舞台边的声压。"
        source = self.source("stage", detail)
        await self.learn(interests=[("舞台拍摄", .04, source), ("现场音量", -.09, source)])
        private = self.source("sound-note", "另一个人私下分享了自己的音量笔记。", private=True)
        await self.learn(interests=[("现场音量", -.01, private)])
        await self.reply("聊聊聚光灯、拍摄角度与舞台边的声压", "before-stage")
        await self.bot.reference_send_feedback(sent=True,
            message=message("stage-sent", uid="99999999", text=detail))
        refs = self.bot._reference.store.state(OWNER)["focus_refs"]
        self.assertEqual([ref["id"] for ref in refs], [source["id"]])
        for limit in (1, 2, 12):
            self.bot.config.unified_mind.recall_limit = limit
            visible = await self.reply("你以前不是喜欢这个吗？", "ambiguous-stage-"+str(limit))
            self.assertIn("指刚才材料里的哪一部分", visible)
            self.assertEqual(self.memories, [])
            for withheld in ("舞台拍摄", "现场音量", private["text"]):
                self.assertNotIn(withheld, visible)
        open_ended = await self.reply("你觉得它怎么样？", "open-stage")
        self.assertIn(detail, open_ended)
        self.assertIn("自己的兴趣倾向：舞台拍摄", open_ended)

    async def test_unique_interest_followup_reserves_the_interest_and_reports_only_verified_change(self):
        await self.reply("你最近喜欢什么", "before-photo")
        await self.bot.reference_send_feedback(sent=True, message=message("photo-sent", uid="99999999", text=PHOTO))
        self.bot.config.unified_mind.recall_limit = 1
        first = await self.reply("你以前不是喜欢这个吗", "before-change")
        self.assertEqual([row["kind"] for row in self.memories], ["interest"])
        self.assertIn("自己的兴趣倾向：摄影", first)
        self.assertIn("历史不足时可以说明以前的倾向还无法确认", first)
        self.assertNotIn("最近一次有记录的调整", first)
        self.assertIn("本轮未提供原文", first)
        second = self.source("composition", "后来的构图资料还展示了夜色如何影响画面。")
        await self.learn(interests=[("摄影", .01, second)])
        await self.bot.on_unload()
        self.bot = self.make_bot()
        self.bot.config.unified_mind.recall_limit = 2
        visible = await self.reply("你的喜好怎么变了", "after-change", sid="new-private")
        self.assertEqual([row["kind"] for row in self.memories], ["observation", "interest"])
        self.assertIn(PHOTO, visible)
        self.assertIn("兴趣有所回升", visible)
        self.assertNotIn(second["text"], visible)
        outdated = await self.reply("你现在还喜欢这个吗", "revoked", sid="new-private", on_expression=lambda:
            self.bot._tool_store.retract_observation("news", "composition"))
        self.assertNotIn("自己的兴趣倾向：摄影", outdated)
        self.assertNotIn("兴趣有所回升", outdated)
        missing = await self.reply("你现在还喜欢这个吗", "no-interest", sid="new-private")
        self.assertIn("目前无法确认对这部分内容的喜好", missing)
        self.assertNotIn("高音量音乐节", missing)

    async def test_only_owner_authored_private_interest_followup_resolves_a_specific_preference(self):
        text = "你现在还喜欢这个吗"
        for kind in ("forward", "mismatch", "reply", "other", "group"):
            await self.reply("你最近喜欢什么", "before-"+kind)
            await self.bot.reference_send_feedback(sent=True, message=message("sent-"+kind, uid="99999999", text=PHOTO))
            event = {**message("bad-"+kind, text=text), "raw_message": [{"type": "text", "data": text}]}
            if kind == "forward":
                event["raw_message"] = [{"type": "forward", "data": text}]
            elif kind == "mismatch":
                event["raw_message"][0]["data"] = "转述朋友的问题"
            elif kind == "reply":
                event["reply_to"] = "other-message"
            elif kind == "other":
                event["message_info"]["user_info"]["user_id"] = OTHER
            else:
                event["message_info"]["group_info"] = {"group_id": GROUP}
            visible = await self.reply(text, event=event)
            self.assertNotIn("主人在问刚才材料相关的自身兴趣", visible, kind)

    async def test_single_private_interest_and_its_direction_remain_withheld(self):
        source = self.source("shared-focus", "这份公开材料讨论了摄影师如何记录舞台现场。")
        await self.learn(interests=[("蓝色手稿", -.08, source)])
        private = self.source("secret-note", "另一个人私下讲过蓝色手稿的来历。", private=True)
        await self.learn(interests=[("蓝色手稿", -.01, private)])
        await self.reply("聊聊摄影师如何记录舞台现场", "before-hidden")
        await self.bot.reference_send_feedback(sent=True, message=message("hidden-sent", uid="99999999", text=source["text"]))
        visible = await self.reply("你现在还喜欢这个吗", "hidden-interest")
        self.assertTrue(any(row["kind"] == "interest" and "蓝色手稿" in row["text"] for row in self.memories))
        self.assertIn(source["text"], visible)
        for withheld in ("蓝色手稿", private["text"], "目前不太感兴趣", "兴趣有所减弱"):
            self.assertNotIn(withheld, visible)

    async def test_another_version_of_the_same_original_cannot_resolve_the_interest(self):
        runtime = self.bot._ensure_reference()
        stale = {**self.photo, "text": "这份材料过去是另一版。"}
        result = runtime._interest_followup({"query": "你现在还喜欢这个吗"}, stale)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["records"], [])

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
            self.assertFalse(interest_followup_question(text))
        for text in ("你以前不是喜欢这个吗？", "你现在还喜欢这个吗", "你的喜好怎么变了！"):
            self.assertTrue(interest_followup_question(text))
            self.assertTrue(reading_reaction_question(text))
            self.assertFalse(interest_followup_question("朋友问"+text))
        self.assertFalse(reading_reaction_question("她问你为什么喜欢这个"))
