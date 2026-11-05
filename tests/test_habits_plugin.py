"""Actual plugin hooks enforce current-turn precedence over durable defaults."""
from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock

import test_habits_runtime as helpers
from MaiLivingWorld.tests.test_mcp_plugin import message


class HabitsPluginTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = helpers.HabitsRuntimeTests.asyncSetUp
    asyncTearDown = helpers.HabitsRuntimeTests.asyncTearDown
    make_bot = helpers.HabitsRuntimeTests.make_bot
    kwargs = helpers.HabitsRuntimeTests.kwargs

    async def receive(self, text, mid):
        self.bot._habits = self.runtime
        self.bot.config.habits.learn_from_feedback = False
        event = message(mid=mid, text=text)
        await self.bot.capture_owner(message=event)
        await self.bot.capture_unified(message=event)

    async def reply(self, mid):
        result = await self.bot.brain_reply_hint(**self.kwargs(mid), messages=[{"role": "system", "content": "host"}])
        return json.dumps(result, ensure_ascii=False)

    async def defaults(self):
        await self.receive("/习惯 设置 默认 回应 倾听", "listen")
        await self.receive("/习惯 设置 默认 长度 详细", "length")
        await self.receive("/习惯 设置 技术 长度 详细", "technical")

    async def test_current_normal_without_attention_or_continuity_only_suppresses_support(self):
        await self.defaults()
        self.assertFalse(self.bot.config.attention.enabled)
        self.assertFalse(self.bot.config.continuity.enabled)
        for mid, text in (("normal", "正常聊天吧"), ("no-comfort", "不用一直安慰我"), ("casual", "随便聊聊")):
            await self.receive(text, mid)
            result = await self.reply(mid)
            self.assertNotIn("默认先倾听", result)
            self.assertIn("默认给出较详细", result)

    async def test_attention_current_normal_does_not_remove_length_default(self):
        await self.defaults()
        self.bot.config.attention.enabled = True
        await self.receive("正常聊天吧", "normal")
        result = await self.reply("normal")
        self.assertNotIn("默认先倾听", result)
        self.assertIn("默认给出较详细", result)
        self.assertEqual(self.bot._attention.effective_mode(self.kwargs("normal")), "explicit_normal")

    async def test_attention_advice_keeps_technical_detail_and_suppresses_old_listen(self):
        await self.defaults()
        self.bot.config.attention.enabled = True
        self.bot.ctx.llm.generate = AsyncMock(return_value={"response": json.dumps(
            {"mode": "advice", "confidence": .98, "evidence": "帮我想办法"})})
        await self.receive("这段代码有问题，帮我想办法", "advice")
        result = await self.reply("advice")
        self.assertNotIn("默认先倾听", result)
        self.assertIn("默认给出较详细", result)
        self.assertEqual(self.bot._attention.effective_mode(self.kwargs("advice")), "advice")

    async def test_clear_takes_effect_in_real_reply_and_old_message_context_is_rejected(self):
        await self.receive("/习惯 设置 默认 长度 简短", "set")
        await self.receive("你好", "first")
        self.assertIn("默认简短", await self.reply("first"))
        await self.receive("/习惯 清除 默认 长度", "clear")
        await self.receive("我又来了", "latest")
        self.assertNotIn("默认简短", await self.reply("latest"))
        self.assertEqual(self.runtime.hint(self.kwargs("first")), "")
        self.assertFalse((await self.bot.habits(**self.kwargs("first")))["success"])


if __name__ == "__main__":
    unittest.main()
