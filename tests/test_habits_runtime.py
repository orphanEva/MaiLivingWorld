"""Offline runtime integration using real trusted UnifiedRuntime contexts and ledgers."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from MaiLivingWorld.tests.test_runtime import FakeContext, plugin
from MaiLivingWorld.tests.test_mcp_plugin import OWNER, OTHER, GROUP, message
from MaiLivingWorld.habits_runtime import HabitsRuntime
from MaiLivingWorld.store import Store


class Settings(SimpleNamespace):
    def model_dump(self):
        return dict(vars(self))


class HabitsRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bot, self.runtime = self.make_bot()

    def make_bot(self):
        bot = plugin.MaiLivingWorld()
        bot.ctx = FakeContext()
        bot.ctx.paths = SimpleNamespace(data_dir=Path(self.temp.name))
        bot.config = plugin.Config()
        if not hasattr(bot.config, "habits"):
            object.__setattr__(bot.config, "habits", Settings(enabled=False, learn_from_feedback=True,
                                                            interval_seconds=300, max_daily_batches=4))
        bot.config.owner.enabled = bot.config.unified_mind.enabled = bot.config.habits.enabled = True
        bot.config.owner.qq = OWNER
        bot.config.unified_mind.mode = "active"
        bot.config.unified_mind.min_interval_seconds = 0
        bot.config.brain_memory.private_users = [OTHER]
        bot.config.brain_memory.group_ids = [GROUP]
        bot._store = Store(Path(self.temp.name)/"living_world.sqlite3")
        bot._ensure_unified()
        return bot, HabitsRuntime(bot)

    async def asyncTearDown(self):
        self.runtime.close()
        await self.bot.on_unload()
        self.temp.cleanup()

    def capture(self, text="你好", mid="m1", **changes):
        event = message(mid=mid, text=text, **changes)
        self.bot._unified.capture(event, private_users=[OWNER, OTHER], group_ids=[GROUP])
        self.runtime.capture(event)
        return event

    def kwargs(self, mid="m1", sid="private-one"):
        return {"session_id": sid, "reply_message_id": mid}

    def command(self, text="/习惯 设置 默认 长度 简短", mid="cmd"):
        return self.runtime.capture(message(mid=mid, text=text))

    def model(self, *, on_first=None, approved=True):
        async def generate(prompt, **kwargs):
            data = json.loads(prompt.rsplit("\n", 1)[1])
            if "updates" in data:
                return {"response": json.dumps({"reviews": [{"index": 0, "supported": approved}]})}
            if on_first:
                on_first()
            return {"response": json.dumps({"updates": [{"situation": "general", "dimension": "length", "value": "short",
                               "evidence": "explicit", "quote": data["input"]["text"]}]}, ensure_ascii=False)}
        self.bot.ctx.llm.generate = AsyncMock(side_effect=generate)

    async def test_command_no_model_and_restart_default_with_clear_replay_protection(self):
        self.assertEqual(self.command(), (True, False))
        self.capture("你好")
        self.assertIn("默认简短", self.runtime.hint(self.kwargs()))
        self.bot.ctx.llm.generate.assert_not_awaited()
        self.runtime.close()
        await self.bot.on_unload()
        self.bot, self.runtime = self.make_bot()
        self.capture("聊聊天", "new", sid="new-session")
        self.assertIn("默认简短", self.runtime.hint(self.kwargs("new", "new-session")))
        self.command("/习惯 清除 默认 长度", "clear")
        self.command("/习惯 设置 默认 长度 详细", "new-setting")
        self.assertEqual(self.command("/习惯 清除 默认 长度", "clear"), (False, False))
        self.assertIn("较详细", self.runtime.hint(self.kwargs("new", "new-session")))

    async def test_natural_learning_two_passes_shared_budget_and_safe_inspect(self):
        text = "以后回复请简短一些。"
        self.capture(text)
        self.model()
        before = self.bot._self_state.state()["thought_count"]
        await self.runtime.process()
        self.assertEqual(self.runtime.last_status, "updated")
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 2)
        self.assertEqual(self.bot._self_state.state()["thought_count"], before+2)
        self.capture("好的", "later")
        result = self.runtime.inspect(self.kwargs("later"))
        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["preferences"][0]["evidence_count"], 1)
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(text, rendered)
        self.assertNotIn(OWNER, rendered)
        self.assertNotIn("body_digest", rendered)
        self.capture(text)
        await self.runtime.process()
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 2)

    async def test_group_other_sender_forward_media_and_bot_cannot_learn(self):
        text = "以后回复请简短一些。"
        events = [message(uid=OTHER, text=text), message(group=GROUP, text=text),
                  {**message(text=text), "account_id": OWNER}, {**message(text=text), "is_picture": True},
                  {**message(text=text), "raw_message": [{"type": "forward", "data": text}]}]
        for event in events:
            with self.assertRaises(ValueError):
                self.runtime.capture(event)
        self.assertEqual(self.runtime.store.status(OWNER)["queued_inputs"], 0)
        self.bot._unified.capture(message(uid=OTHER, text="你好"), private_users=[OWNER, OTHER], group_ids=[])
        self.assertEqual(self.runtime.hint(self.kwargs()), "")

    async def test_stale_private_message_never_receives_habit_context(self):
        self.command()
        self.capture("你好", "first")
        self.capture("还在吗", "second")
        self.bot._unified.latest_private_only = False
        self.assertEqual(self.runtime.hint(self.kwargs("first")), "")
        self.assertIn("默认简短", self.runtime.hint(self.kwargs("second")))

    async def test_situation_override_and_temporary_support_keep_compatible_dimensions(self):
        self.command()
        self.command("/习惯 设置 技术 长度 详细", "tech")
        self.command("/习惯 设置 默认 回应 倾听", "listen")
        self.command("/习惯 设置 默认 追问 少问", "fewer")
        self.capture("今天的技术问题我们继续聊吧")
        hint = self.runtime.hint(self.kwargs(), current_mode="advice")
        self.assertIn("较详细", hint)
        self.assertIn("少追问", hint)
        self.assertNotIn("先倾听", hint)
        self.assertNotIn("默认简短", hint)
        self.assertNotIn("先倾听", self.runtime.hint(self.kwargs(), current_mode="explicit_normal"))
        self.capture("今天的天气很好", "general")
        self.assertIn("默认简短", self.runtime.hint(self.kwargs("general")))

    async def test_current_length_or_question_request_overrides_only_that_dimension(self):
        self.command()
        self.command("/习惯 设置 默认 追问 少问", "fewer")
        self.capture("这次请详细解释一下")
        hint = self.runtime.hint(self.kwargs())
        self.assertNotIn("默认简短", hint)
        self.assertIn("少追问", hint)
        self.capture("现在你可以提问", "questions")
        hint = self.runtime.hint(self.kwargs("questions"))
        self.assertIn("默认简短", hint)
        self.assertNotIn("少追问", hint)

    async def test_disable_or_new_owner_never_receives_old_owner_defaults(self):
        self.command()
        self.capture("你好")
        self.bot.config.habits.enabled = False
        self.assertEqual(self.runtime.hint(self.kwargs()), "")
        self.assertEqual(self.runtime.capture(message(text="以后回复请详细一些。")), (False, False))
        self.bot.config.habits.enabled = True
        self.bot.config.owner.qq = OTHER
        self.bot._self_state.configure_owner(OTHER)
        self.capture("你好", "new-owner", uid=OTHER)
        self.assertEqual(self.runtime.hint(self.kwargs("new-owner")), "")
        self.assertEqual(self.runtime.store.preferences(OTHER), [])

    async def test_command_during_model_await_cancels_old_proposal(self):
        self.capture("以后回复请简短一些。")
        self.model(on_first=lambda: self.command("/习惯 清除 默认 长度", "clear"))
        await self.runtime.process()
        self.assertEqual(self.runtime.store.preferences(OWNER)[0]["status"], "cleared")
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 1)
        self.assertEqual(self.runtime.last_status, "context_changed")

    async def test_config_change_during_model_await_cancels_commit(self):
        self.capture("以后回复请简短一些。")
        self.model(on_first=lambda: setattr(self.bot.config.habits, "learn_from_feedback", False))
        await self.runtime.process()
        self.assertEqual(self.runtime.store.preferences(OWNER), [])
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 1)

    async def test_owner_change_during_model_await_cancels_commit(self):
        self.capture("以后回复请简短一些。")
        self.model(on_first=lambda: setattr(self.bot.config.owner, "qq", OTHER))
        await self.runtime.process()
        self.assertEqual(self.runtime.store.preferences(OWNER), [])
        self.assertEqual(self.runtime.store.preferences(OTHER), [])
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 1)

    async def test_background_does_not_spend_reserved_reply_capacity(self):
        self.capture("以后回复请简短一些。")
        self.bot.config.unified_mind.max_daily_model_calls = 12
        self.bot.config.unified_mind.reserved_reply_calls = 12
        self.model()
        await self.runtime.process()
        self.bot.ctx.llm.generate.assert_not_awaited()
        self.assertEqual(self.runtime.last_status, "model_budget")
        self.assertEqual(self.runtime.store.status(OWNER)["inputs"], {"pending": 1})
        self.assertEqual(self.runtime.store._read("SELECT attempts FROM habits_inputs")[0]["attempts"], 0)

    async def test_no_automatic_learning_when_disabled_but_explicit_commands_still_work(self):
        self.bot.config.habits.learn_from_feedback = False
        self.capture("以后回复请简短一些。")
        self.assertEqual(self.runtime.store.status(OWNER)["queued_inputs"], 0)
        await self.runtime.process()
        self.bot.ctx.llm.generate.assert_not_awaited()
        self.assertTrue(self.command()[0])

    async def test_real_plugin_capture_and_reply_hook_apply_only_compatible_defaults(self):
        self.bot._habits = self.runtime
        self.bot._process_habits = AsyncMock()
        for mid, text in (("cmd", "/习惯 设置 技术 长度 详细"), ("question", "这段代码请帮我想办法")):
            event = message(mid=mid, text=text)
            await self.bot.capture_owner(message=event)
            await self.bot.capture_unified(message=event)
        result = await self.bot.brain_reply_hint(**self.kwargs("question"), messages=[{"role": "system", "content": "host"}])
        self.assertIn("默认给出较详细", json.dumps(result, ensure_ascii=False))
        event = message(mid="current", text="这段代码请简短解释一下")
        await self.bot.capture_owner(message=event)
        await self.bot.capture_unified(message=event)
        if self.bot._memory_jobs:
            await asyncio.gather(*tuple(self.bot._memory_jobs))
        result = await self.bot.brain_reply_hint(**self.kwargs("current"), messages=[{"role": "system", "content": "host"}])
        self.assertNotIn("默认给出较详细", json.dumps(result, ensure_ascii=False))
        self.assertTrue((await self.bot.habits(**self.kwargs("current")))["success"])

    async def test_real_plugin_hook_schedules_two_passes_and_persists_the_preference(self):
        self.bot._habits = self.runtime
        self.model()
        event = message(text="以后回复请简短一些。")
        await self.bot.capture_owner(message=event)
        await self.bot.capture_unified(message=event)
        if self.bot._memory_jobs:
            await asyncio.gather(*tuple(self.bot._memory_jobs))
        self.assertEqual(self.runtime.last_status, "updated")
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 2)
        self.assertEqual(self.runtime.store.preferences(OWNER)[0]["value"], "short")


if __name__ == "__main__":
    unittest.main()
