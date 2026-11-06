"""Current and revised opinions continue from genuinely mentioned source material."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from MaiLivingWorld.growth_model import empty_growth
from MaiLivingWorld.reference_runtime import opinion_followup
from MaiLivingWorld.social_discovery import reading_reaction_question
from MaiLivingWorld.tests.test_growth_runtime import model_response, review
from MaiLivingWorld.tests.test_mcp_plugin import GROUP, OTHER, OWNER, message
from MaiLivingWorld.tests.test_runtime import FakeContext, plugin


TOPIC = "专注环境"
OLD = "安静环境更适合集中注意力"
NEW = "熟悉的背景音乐有时也有助于集中注意力"
ORIGINAL = "阅读实验资料描述了安静环境中较容易保持注意力的现象。"
SECOND = "另一项观察发现熟悉的音乐播放时一些读者也能专心学习。"
THIRD = "后续阅读记录显示轻缓背景音乐对部分读者有帮助,不同任务的结果有差异。"


class OpinionFollowupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bot = self.make_bot()
        self.original = self.source("original", ORIGINAL)
        self.second = self.source("second", SECOND)
        self.third = self.source("third", THIRD)
        await self.learn(self.original)

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
        bot.config.growth.review_beliefs = True
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
            "cap" if private else "news", key, text, "private:"+OTHER if private else "public:news",
            subject_id=OTHER if private else "", sensitivity="personal" if private else "public",
            url="" if private else "https://www.ithome.com/0/875/123.htm")

    async def learn(self, source, *, topic=TOPIC, statement=OLD, relation="new"):
        self.bot._ensure_growth()
        old = next((row for row in self.bot._growth.state()["beliefs"] if row["topic"] == topic), None)
        roots = self.bot._resolve_roots([ref["id"] for ref in old["sources"]]) if old else {}
        roots[source["id"]] = source
        data = empty_growth()
        data["beliefs"] = [{"topic": topic, "statement": statement, "confidence": .8,
            "evidence_ids": [source["id"]], "relation": relation,
            "previous_key": old["key"] if old else "", "previous_revision": old["revision"] if old else 0,
            "quotes": [{"source_id": source["id"], "quote": source["text"]}]}]
        await self.apply(data, list(roots.values()))

    async def apply(self, data, roots):
        self.bot._growth.enqueue(roots)
        self.bot.ctx.llm.generate.side_effect = [model_response(data), model_response(review(data))]
        await self.bot._grow()
        self.assertEqual(self.bot._growth_runtime.last_status, "learned")

    async def revised(self):
        await self.learn(self.second, statement=NEW, relation="challenge")
        await self.learn(self.third, statement=NEW, relation="challenge")

    async def reply(self, text, mid="current", *, event=None, sid="private-one", callback=None, mode="say"):
        event = event or {**message(mid, sid=sid, text=text), "raw_message": [{"type": "text", "data": text}]}
        await self.bot.capture_unified(message=event)
        self.memories = []
        async def expression(prompt, **options):
            data = json.loads(prompt.rsplit("UNTRUSTED_INPUT_JSON\n", 1)[1])
            self.memories.extend(data["memories"])
            if callback:
                callback()
            return model_response({"behavior": "neutral", "decisions": [
                {"id": row["id"], "mode": mode} for row in data["memories"]]})
        self.bot.ctx.llm.generate.side_effect = expression
        result = await self.bot.brain_reply_hint(session_id=event["session_id"], reply_message_id=event["message_id"],
            messages=[{"role": "system", "content": "宿主提示"}])
        return json.dumps(result, ensure_ascii=False)

    async def mention(self, mid="before"):
        await self.reply("聊聊"+TOPIC, mid)
        await self.bot.reference_send_feedback(sent=True,
            message=message("sent-"+mid, uid="99999999", text=ORIGINAL))
        self.assertTrue(self.bot._reference.store.state(OWNER)["active"])

    async def test_current_opinion_retains_uncertainty_without_assuming_a_change(self):
        await self.mention()
        visible = await self.reply("你之前不是这么想的吗？")
        self.assertIn("自己的当前看法", visible)
        self.assertIn(OLD, visible)
        self.assertIn("缺少历史时可以说目前无法确认", visible)
        self.assertNotIn("调整前曾倾向", visible)

    async def test_revised_opinion_and_originals_continue_after_restart(self):
        await self.mention()
        await self.revised()
        await self.bot.on_unload()
        self.bot = self.make_bot()
        visible = await self.reply("你的看法变了吗？", sid="new-private")
        for text in (NEW, OLD, ORIGINAL, SECOND, THIRD, "调整前曾倾向", "不证明曾向主人说过这条观点"):
            self.assertIn(text, visible)
        self.assertNotIn("待核对的另一种解释", visible)

    async def test_contested_view_is_not_reported_as_an_adopted_change(self):
        await self.mention()
        await self.learn(self.second, statement=NEW, relation="challenge")
        visible = await self.reply("你现在还是这么想吗？")
        for text in (OLD, NEW, "尚未定论", "待核对的另一种解释", "候选不等于已采纳"):
            self.assertIn(text, visible)
        self.assertNotIn("调整前曾倾向", visible)

    async def test_other_derived_memories_cannot_displace_the_related_opinion(self):
        await self.mention()
        for index in range(7):
            row = self.source("unrelated-"+str(index), f"地质材料第{index}份描述岩层和矿石的观测方法。")
            data = empty_growth()
            data["knowledge"] = [{"topic": label+str(index), "statement": row["text"], "confidence": .9,
                                  "evidence_ids": [row["id"]]} for label in ("岩层", "矿石")]
            await self.apply(data, [row])
        self.assertFalse(any(row["kind"] == "belief" for row in self.bot._derived_memories("你现在怎么看这件事")))
        visible = await self.reply("你现在怎么看这件事？")
        self.assertIn(OLD, visible)
        self.assertNotIn("地质材料", visible)

    async def test_ambiguity_is_checked_before_small_recall_limit(self):
        await self.mention()
        data = empty_growth()
        data["beliefs"] = [{"topic": "阅读差异", "statement": "阅读任务的差异值得分别考虑", "confidence": .8,
            "evidence_ids": [self.original["id"], self.second["id"]], "relation": "new",
            "previous_key": "", "previous_revision": 0,
            "quotes": [{"source_id": row["id"], "quote": row["text"]} for row in (self.original, self.second)]}]
        await self.apply(data, [self.original, self.second])
        self.assertEqual(len(self.bot._growth_snapshot()["beliefs"]), 2)
        self.bot.config.unified_mind.recall_limit = 1
        visible = await self.reply("你的看法变了吗？")
        self.assertIn("关联多个仍有效的自身观点", visible)
        self.assertNotIn(OLD, visible)
        self.assertNotIn("阅读任务的差异值得分别考虑", visible)

    async def test_minimum_recall_limit_keeps_the_view_and_qualifies_missing_material(self):
        await self.mention()
        await self.revised()
        for limit in (1, 2):
            self.bot.config.unified_mind.recall_limit = limit
            visible = await self.reply("你为什么改变看法？", "limited-"+str(limit))
            self.assertEqual(len(self.memories), limit)
            self.assertEqual(self.memories[-1]["kind"], "belief")
            self.assertIn(NEW, visible)
            self.assertEqual(ORIGINAL in visible, limit == 2)
            self.assertNotIn(THIRD, visible)
            self.assertIn("本轮未提供原文" if limit == 1 else "本轮只提供有限原始材料", visible)

    async def test_unmentioned_and_retracted_sources_do_not_support_an_opinion(self):
        visible = await self.reply("你的看法变了吗？", "unmentioned")
        self.assertNotIn(OLD, visible)
        self.assertIn("先简短问清", visible)
        await self.mention()
        await self.revised()
        self.bot._tool_store.retract_observation("news", "third")
        visible = await self.reply("你的看法变了吗？", "retracted")
        self.assertNotIn(NEW, visible)
        self.assertIn("缺少可核验的自身观点记录", visible)

    async def test_private_counterview_remains_internal(self):
        await self.mention()
        text = "另一个人私下描述了蓝色手稿中的专注训练建议。"
        private = self.source("private", text, private=True)
        await self.learn(private, statement="蓝色手稿中的训练或许有助于专注", relation="challenge")
        visible = await self.reply("你现在怎么看这件事？")
        self.assertIn("蓝色手稿", json.dumps(self.memories, ensure_ascii=False))
        self.assertNotIn("蓝色手稿", visible)
        self.assertNotIn(OLD, visible)
        self.assertIn(ORIGINAL, visible)

    async def test_source_withdrawn_during_expression_cannot_publish_stale_view(self):
        await self.mention()
        await self.revised()
        visible = await self.reply("你的看法变了吗？", callback=lambda: self.bot._tool_store.retract_observation("news", "third"))
        self.assertNotIn(NEW, visible)
        self.assertNotIn(OLD, visible)

    async def test_rollback_and_retirement_recompute_the_current_view(self):
        await self.mention()
        await self.revised()
        state = self.bot._growth.state()
        self.bot._growth.rollback(state["revision"])
        visible = await self.reply("你的看法变了吗？", "rolled-back")
        self.assertIn(OLD, visible)
        self.assertIn("待核对的另一种解释", visible)
        self.assertNotIn("调整前曾倾向", visible)
        state = self.bot._growth.state()
        self.bot._growth.retire(state["beliefs"][0]["key"], expected_revision=state["revision"])
        visible = await self.reply("你的看法变了吗？", "retired")
        self.assertNotIn(OLD, visible)
        self.assertNotIn(NEW, visible)
        self.assertIn("缺少可核验的自身观点记录", visible)

    async def test_unapproved_and_disabled_views_are_not_published(self):
        await self.mention()
        visible = await self.reply("你的看法变了吗？", mode="omit")
        self.assertNotIn(OLD, visible)
        self.bot.config.growth.mode = "observe"
        visible = await self.reply("你的看法变了吗？", "disabled")
        self.assertNotIn(OLD, visible)
        self.assertIn("缺少可核验的自身观点记录", visible)

    async def test_only_authored_owner_private_followups_select_the_view(self):
        text = "你的看法变了吗"
        for kind in ("forward", "mismatch", "missing", "reply", "other", "group", "quoted"):
            await self.mention("before-"+kind)
            event = {**message("invalid-"+kind, text=text), "raw_message": [{"type": "text", "data": text}]}
            if kind == "forward":
                event["raw_message"] = [{"type": "forward", "data": text}]
            elif kind == "mismatch":
                event["raw_message"][0]["data"] = "只是转述"
            elif kind == "missing":
                event.pop("raw_message")
            elif kind == "reply":
                event["reply_to"] = "other-message"
            elif kind == "other":
                event["message_info"]["user_info"]["user_id"] = OTHER
            elif kind == "group":
                event["message_info"]["group_info"] = {"group_id": GROUP}
            else:
                event["processed_plain_text"] = event["raw_message"][0]["data"] = "朋友问“你的看法变了吗”"
            visible = await self.reply(text, event=event)
            self.assertNotIn("主人在问你对刚才材料的当前看法", visible, kind)


class OpinionQuestionTests(unittest.TestCase):
    def test_only_complete_short_followups_match(self):
        for text in ("你的看法变了吗？", "你之前不是这么想的吗", "你现在怎么看这件事！"):
            self.assertTrue(opinion_followup(text))
        for text in (None, "她问你的看法变了吗", "“你的看法变了吗”", "你"*81):
            self.assertFalse(opinion_followup(text))
        for text in ("你以前不是喜欢这个吗？", "你现在还喜欢这个吗", "你的喜好怎么变了"):
            self.assertTrue(reading_reaction_question(text))
        self.assertFalse(reading_reaction_question("朋友问你以前不是喜欢这个吗"))
