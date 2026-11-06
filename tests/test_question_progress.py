"""Actual growth and reply entry points retain source-backed question progress."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from MaiLivingWorld.growth_model import empty_growth
from MaiLivingWorld.tests.test_growth_runtime import model_response, review
from MaiLivingWorld.tests.test_mcp_plugin import OWNER, OTHER, message
from MaiLivingWorld.tests.test_runtime import FakeContext, plugin


TOPIC = "望远镜口径"
QUESTION = "望远镜口径为什么会影响分辨率"
ANSWER = "相同波长时更大的口径可以区分更小的角距,实际效果还受大气条件影响"
ORIGINAL = "报道节选:观测小组使用不同口径的望远镜,比较照片上两个相近光点的清晰程度。"
EVIDENCE = "百科节选:相同波长时,望远镜口径越大,理论角分辨率越高。实际观测还受大气扰动影响。"


class QuestionProgressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bot = self.make_bot()
        self.source = self.bot._ensure_tool_store().put_observation("news", "source", ORIGINAL,
            "public:news", sensitivity="public", url="https://www.ithome.com/0/875/123.htm")
        self.answer = self.bot._tool_store.put_observation("reference", "answer", EVIDENCE,
            "public:reference", sensitivity="public", url="https://zh.wikipedia.org/wiki/Telescope")
        await self.learn(TOPIC, QUESTION, "open", [self.source])

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
        bot.config.mcp.enabled = bot.config.mcp.public_sources_enabled = True
        bot.config.reference.enabled = True
        return bot

    async def asyncTearDown(self):
        await self.bot.on_unload()
        self.temp.cleanup()

    async def learn(self, topic, text, status, records):
        self.bot._ensure_growth()
        data = empty_growth()
        data["questions"] = [{"topic": topic, "question": text, "status": status,
                              "evidence_ids": [row["id"] for row in records]}]
        self.bot._growth.enqueue(records)
        self.bot.ctx.llm.generate.side_effect = [model_response(data), model_response(review(data))]
        await self.bot._grow()
        self.assertEqual(self.bot._growth_runtime.last_status, "learned")

    async def reply(self, text, mid="current", sid="private-one"):
        event = message(mid, sid=sid, text=text)
        event["raw_message"] = [{"type": "text", "data": text}]
        await self.bot.capture_unified(message=event)
        self.expression_memories = []
        async def expression(prompt, **options):
            data = json.loads(prompt.rsplit("UNTRUSTED_INPUT_JSON\n", 1)[1])
            self.expression_memories.extend(data["memories"])
            return model_response({"behavior": "neutral", "decisions": [
                {"id": row["id"], "mode": "say"} for row in data["memories"]]})
        self.bot.ctx.llm.generate.side_effect = expression
        result = await self.bot.brain_reply_hint(session_id=sid, reply_message_id=mid,
            messages=[{"role": "system", "content": "宿主提示"}])
        return json.dumps(result, ensure_ascii=False)

    async def mention(self):
        await self.reply("聊聊"+TOPIC, "before")
        await self.bot.reference_send_feedback(sent=True,
            message=message("out", uid="99999999", text=ORIGINAL))
        self.assertTrue(self.bot._reference.store.state(OWNER)["active"])

    async def test_resolved_result_stays_in_regular_recall_across_restart(self):
        await self.learn(TOPIC, ANSWER, "resolved", [self.source, self.answer])
        await self.bot.on_unload()
        self.bot = self.make_bot()
        result = await self.reply("再聊聊"+TOPIC)
        self.assertIn(ANSWER, result)
        self.assertIn("已标记解决的问题记录", result)
        self.assertIn("答案仍需原始材料支持", result)

    async def test_mentioned_material_connects_to_actual_updated_answer_after_restart(self):
        await self.mention()
        await self.learn(TOPIC, ANSWER, "resolved", [self.source, self.answer])
        await self.bot.on_unload()
        self.bot = self.make_bot()
        result = await self.reply("之前那个问题后来弄明白了吗？", sid="new-private")
        self.assertIn(ANSWER, result)
        self.assertIn(EVIDENCE, result)
        self.assertIn("先前提过材料不证明", result)
        self.assertIn("结题状态也不证明实际联网查阅", result)

    async def test_unmentioned_question_and_retracted_answer_do_not_fill_missing_progress(self):
        await self.learn(TOPIC, ANSWER, "resolved", [self.source, self.answer])
        missing = await self.reply("那个问题有答案了吗？", "unmentioned")
        self.assertNotIn(ANSWER, missing)
        self.assertIn("先简短问清", missing)
        await self.mention()
        self.bot._tool_store.retract_observation("reference", "answer")
        stale = await self.reply("那个问题有答案了吗？", "retracted")
        self.assertNotIn(ANSWER, stale)
        self.assertIn("缺少可核验的问题进展", stale)

    async def test_two_questions_on_one_material_require_disambiguation(self):
        await self.mention()
        await self.learn("大气条件", "应该怎样控制大气条件", "open", [self.source, self.answer])
        result = await self.reply("那个问题查得怎么样了？")
        self.assertIn("关联多个仍有效的问题", result)
        self.assertNotIn(QUESTION, result)
        self.assertNotIn("应该怎样控制大气条件", result)

    async def test_legacy_resolved_question_keeps_original_wording_and_reads_answer_evidence(self):
        await self.mention()
        await self.learn(TOPIC, QUESTION, "resolved", [self.source, self.answer])
        result = await self.reply("之前那个问题有答案了吗？")
        self.assertIn(QUESTION, result)
        self.assertIn(EVIDENCE, result)
        self.assertIn("旧问题记录可能仍是原来的问句", result)
        self.assertIn("标记解决本身不构成答案", result)

    async def test_minimum_recall_limits_keep_progress_bounded_with_material_qualification(self):
        await self.mention()
        await self.learn(TOPIC, ANSWER, "resolved", [self.source, self.answer])
        for limit in (1, 2):
            self.bot.config.unified_mind.recall_limit = limit
            result = await self.reply("之前那个问题有答案了吗？", "limited-"+str(limit))
            self.assertEqual(len(self.expression_memories), limit)
            self.assertEqual(self.expression_memories[-1]["kind"], "question")
            self.assertIn(ANSWER, result)
            self.assertNotIn(EVIDENCE, result)
            qualifier = "本轮未提供原文，不能只凭已解决状态解释具体答案" if limit == 1 else "本轮只提供有限原始材料"
            self.assertIn(qualifier, result)
            self.assertEqual(ORIGINAL in result, limit == 2)

    async def test_other_person_private_root_and_derived_answer_remain_internal(self):
        await self.mention()
        private_text = "另一个人私下说蓝色信封藏在书柜里。"
        private_answer = "关于望远镜口径的手稿藏在蓝色信封里"
        private = self.bot._tool_store.put_observation("cap", "other-private", private_text,
            "private:"+OTHER, subject_id=OTHER, sensitivity="personal")
        await self.learn(TOPIC, private_answer, "resolved", [self.source, private])
        result = await self.reply("之前那个问题有答案了吗？")
        internal = json.dumps(self.expression_memories, ensure_ascii=False)
        self.assertIn(private_text, internal)
        self.assertIn(private_answer, internal)
        self.assertNotIn(private_text, result)
        self.assertNotIn(private_answer, result)
        self.assertNotIn("蓝色信封", result)
        self.assertIn(ORIGINAL, result)
        self.assertEqual(self.bot._unified.last_counts["withheld"], 2)
