"""A persistent personality can have tradeoffs without fabricated events or random errors."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from MaiLivingWorld.growth_runtime import style_hint
from MaiLivingWorld.tests.test_runtime import FakeContext, plugin
from MaiLivingWorld.tests.test_mcp_plugin import OWNER, GROUP, message


class PersonalityTensionsTests(unittest.TestCase):
    def test_existing_traits_choose_stable_bounded_tradeoffs_without_private_details(self):
        state = {"traits": {"curiosity": .8, "sociability": .6, "patience": .6, "caution": .1},
                 "mood": {}, "reason": "私下的原因不应被抄写", "owner_id": OWNER}
        before = copy.deepcopy(state)
        hint = style_hint(state, personality_tensions=True)
        self.assertIn("更容易被新细节吸引", hint)
        self.assertIn("较愿意先给出判断", hint)
        self.assertNotIn("有时会想多说几句", hint)
        self.assertNotIn("私下的原因", hint)
        self.assertNotIn(OWNER, hint)
        self.assertNotIn("0.8", hint)
        self.assertIn("当前明确请求、纠正、倾诉与相处边界优先", hint)
        self.assertIn("不为表现缺点而故意犯错", hint)
        self.assertEqual(style_hint(state, personality_tensions=True), hint)
        self.assertEqual(state, before)

    def test_neutral_or_invalid_values_do_not_invent_a_personality(self):
        for invalid in (None, {}, {"traits": []}, {"traits": {"curiosity": True, "patience": float("nan"),
                       "caution": float("inf"), "sociability": "恶意文本"}}, {"traits": {"curiosity": 10**400}}):
            self.assertEqual(style_hint(invalid, personality_tensions=True), "")
        self.assertNotIn("personality-tensions", style_hint({"traits": {"curiosity": .8}}))

    def test_introverted_cautious_style_has_strengths_and_limits(self):
        hint = style_hint({"traits": {"sociability": .1, "caution": .9}}, personality_tensions=True)
        self.assertIn("表达更内敛", hint)
        self.assertIn("不容易马上拿定主意", hint)
        self.assertIn("犹豫和没把握须来自实际信息不足", hint)
        self.assertNotIn("curiosity", hint)


class PersonalityPluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bot = self.make_bot()
        self.bot._growth.state()
        row = self.bot._growth.con.execute("SELECT payload FROM growth_state WHERE slot=1").fetchone()
        state = json.loads(row[0])
        state["traits"]["curiosity"] = .8
        state["traits"]["caution"] = .7
        # A synthetic previously learned profile; no user state or paid model.
        with self.bot._growth.con:
            self.bot._growth.con.execute("UPDATE growth_state SET payload=? WHERE slot=1", (json.dumps(state),))

    def make_bot(self):
        bot = plugin.MaiLivingWorld()
        bot.ctx = FakeContext()
        bot.ctx.paths = SimpleNamespace(data_dir=Path(self.temp.name))
        bot.config = plugin.Config()
        bot.config.owner.enabled = bot.config.unified_mind.enabled = bot.config.growth.enabled = True
        bot.config.owner.qq = OWNER
        bot.config.unified_mind.mode = bot.config.growth.mode = "active"
        bot.config.growth.personality_tensions = True
        bot.config.unified_mind.max_daily_model_calls = 0
        bot.config.brain_memory.group_ids = [GROUP]
        bot._ensure_growth()
        return bot

    async def asyncTearDown(self):
        await self.bot.on_unload()
        self.temp.cleanup()

    async def reply(self, mid, text):
        event = message(mid=mid, text=text)
        await self.bot.capture_owner(message=event)
        await self.bot.capture_unified(message=event)
        return json.dumps(await self.bot.brain_reply_hint(session_id="private-one", reply_message_id=mid,
            messages=[{"role": "system", "content": "宿主提示"}]), ensure_ascii=False)

    async def test_actual_reply_restart_and_opt_out_preserve_traits_without_model_calls(self):
        before = self.bot._growth.state()["traits"]
        first = await self.reply("one", "只说重点就好")
        self.assertIn("personality-tensions", first)
        self.assertIn("当前明确请求", first)
        self.bot.ctx.llm.generate.assert_not_awaited()
        await self.bot.on_unload()
        self.bot = self.make_bot()
        self.assertIn("personality-tensions", await self.reply("two", "继续聊吧"))
        self.assertEqual(self.bot._growth.state()["traits"], before)
        self.bot.config.growth.personality_tensions = False
        self.assertNotIn("personality-tensions", await self.reply("three", "你好"))
        self.assertEqual(self.bot._growth.state()["traits"], before)
        self.bot.ctx.llm.generate.assert_not_awaited()

    async def test_observe_or_unverified_message_does_not_inject_personality(self):
        self.bot.config.growth.mode = "observe"
        self.assertNotIn("personality-tensions", await self.reply("one", "你好"))
        self.bot.config.growth.mode = "active"
        result = await self.bot.brain_reply_hint(session_id="unknown", reply_message_id="unseen",
            messages=[{"role": "system", "content": "宿主提示"}])
        self.assertNotIn("personality-tensions", json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
