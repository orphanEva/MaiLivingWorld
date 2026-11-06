"""Multi-turn attention, interruption and owner-only dialogue integration."""
import asyncio
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from MaiLivingWorld.tests.test_runtime import FakeContext, plugin
from MaiLivingWorld.tests.test_mcp_plugin import OWNER, OTHER, GROUP, message
from MaiLivingWorld.store import Store


class AttentionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bot = self.make_bot()

    def make_bot(self):
        b = plugin.MaiLivingWorld()
        b.ctx = FakeContext()
        b.ctx.paths = SimpleNamespace(data_dir=Path(self.temp.name))
        b.config = plugin.Config()
        b.config.owner.enabled = b.config.unified_mind.enabled = b.config.attention.enabled = True
        b.config.owner.qq = OWNER
        b.config.unified_mind.mode = "active"
        b.config.unified_mind.min_interval_seconds = 0
        b.config.attention.interval_seconds = 30
        b.config.brain_memory.private_users = [OTHER]
        b.config.brain_memory.group_ids = [GROUP]
        b._store = Store(Path(self.temp.name)/"living_world.sqlite3")
        b._ensure_unified()
        b._ensure_attention()
        return b

    async def asyncTearDown(self):
        await self.bot.on_unload()
        self.temp.cleanup()

    async def capture(self, text="你好", mid="m1", **kwargs):
        event = message(mid=mid, text=text, **kwargs)
        await self.bot.capture_owner(message=event)
        await self.bot.capture_unified(message=event)
        return event

    def kwargs(self, mid="m1", sid="private-one"):
        return {"session_id": sid, "reply_message_id": mid,
                "messages": [{"role": "system", "content": "宿主原始系统提示"}]}

    def answer(self, mode, evidence, confidence=.95):
        return {"response": json.dumps({"mode": mode, "confidence": confidence, "evidence": evidence}, ensure_ascii=False)}

    async def test_literal_listen_uses_no_model_and_survives_new_session(self):
        await self.capture("先听我说就好")
        a = self.bot._attention
        await a.prepare_dialogue(self.kwargs())
        self.bot.ctx.llm.generate.assert_not_called()
        self.assertEqual(a.store.state(OWNER)["conversation"]["mode"], "listen")
        self.assertIn("先接住", a.hint(self.kwargs()))
        await self.bot.on_unload()
        self.bot = self.make_bot()
        await self.capture("还有一点", "m2", sid="new-session")
        self.assertIn("先接住", self.bot._attention.hint(self.kwargs("m2", "new-session")))

    async def test_natural_request_then_correction_changes_fixed_style_only(self):
        text = "最近总被进度追着跑，我只是想说说这件事，先不用给我建议。"
        await self.capture(text)
        self.bot.ctx.llm.generate = AsyncMock(return_value=self.answer("listen", "我只是想说说这件事，先不用给我建议"))
        result = await self.bot.brain_reply_hint(**self.kwargs())
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertIn("先接住", rendered)
        self.assertNotIn(text, rendered)
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 1)
        await self.bot.brain_reply_hint(**self.kwargs())
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 1)
        repair = "你理解错了，我说的是安排太满，不是讨厌现在的工作。"
        await self.capture(repair, "m2")
        self.bot.ctx.llm.generate.return_value = self.answer("repair", "你理解错了，我说的是安排太满")
        result = await self.bot.brain_reply_hint(**self.kwargs("m2"))
        self.assertIn("具体纠正", json.dumps(result, ensure_ascii=False))
        self.assertEqual(self.bot._self_state.state()["owner_id"], OWNER)
        self.assertIsNone(self.bot._growth)
        await self.capture("嗯，这回对了", "m3")
        self.assertEqual(self.bot._attention.store.state(OWNER)["conversation"]["mode"], "normal")

    async def test_new_message_invalidates_inflight_classification(self):
        await self.capture("今天有点不顺心，我想说说")
        started, release = asyncio.Event(), asyncio.Event()
        async def model(**kwargs):
            started.set()
            await release.wait()
            return self.answer("listen", "我想说说")
        self.bot.ctx.llm.generate = AsyncMock(side_effect=model)
        task = asyncio.create_task(self.bot.brain_reply_hint(**self.kwargs()))
        await started.wait()
        await self.capture("先不说这个了，帮我想想办法", "m2")
        release.set()
        result = await task
        self.assertNotIn("先接住", json.dumps(result, ensure_ascii=False))
        self.assertIsNone(self.bot._unified.context(self.kwargs()))
        self.assertEqual(self.bot._attention.store.state(OWNER)["conversation"]["mode"], "normal")

    async def test_model_failure_no_repeat_and_shared_budget(self):
        await self.capture("最近我有点难过，想聊一件事")
        self.bot.ctx.llm.generate = AsyncMock(side_effect=RuntimeError("private provider details"))
        a = self.bot._attention
        before = self.bot._self_state.state()["thought_count"]
        await a.prepare_dialogue(self.kwargs())
        await a.prepare_dialogue(self.kwargs())
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 1)
        self.assertEqual(self.bot._self_state.state()["thought_count"], before+1)
        self.assertNotIn("private provider", a.hint(self.kwargs()))

    async def test_old_receipt_after_restart_never_reclassifies(self):
        await self.capture("我想聊点事情")
        self.bot.ctx.llm.generate = AsyncMock(return_value=self.answer("normal", ""))
        await self.bot._attention.prepare_dialogue(self.kwargs())
        revision = self.bot._attention.store.state(OWNER)["revision"]
        await self.bot.on_unload()
        self.bot = self.make_bot()
        await self.capture("我想聊点事情")
        await self.bot._attention.prepare_dialogue(self.kwargs())
        self.assertEqual(self.bot._attention.store.state(OWNER)["revision"], revision)
        self.bot.ctx.llm.generate.assert_not_called()

    async def test_untrusted_audiences_and_message_override_cannot_read_or_change_mode(self):
        await self.capture("先听我说就好")
        await self.bot._attention.prepare_dialogue(self.kwargs())
        before = self.bot._attention.store.state(OWNER)
        for mid, kwargs in (("other", {"uid": OTHER}), ("group", {"group": GROUP})):
            await self.capture("帮我想想办法", mid, **kwargs)
            request = self.kwargs(mid)
            self.assertEqual(self.bot._attention.hint(request), "")
            self.assertFalse((await self.bot.attention(**request))["success"])
        self.assertEqual(self.bot._attention.store.state(OWNER), before)
        for bad in ({}, message(uid=OTHER), message(group=GROUP)):
            self.assertFalse((await self.bot.attention(**self.kwargs(), message=bad))["success"])

    async def test_disable_or_owner_change_while_model_awaits_discards_mode(self):
        for change in ("owner", "disable"):
            self.bot.config.owner.qq = OWNER
            self.bot.config.attention.enabled = True
            await self.capture("我有一些事情想说说", change)
            async def model(**kwargs):
                if change == "owner":
                    self.bot.config.owner.qq = OTHER
                else:
                    self.bot.config.attention.enabled = False
                return self.answer("listen", "我有一些事情想说说")
            self.bot.ctx.llm.generate = AsyncMock(side_effect=model)
            await self.bot._attention.prepare_dialogue(self.kwargs(change))
            self.assertEqual(self.bot._attention.store.state(OWNER)["conversation"]["mode"], "normal")

    async def test_attention_does_not_start_background_while_owner_is_talking(self):
        a = self.bot._attention
        a.candidates = lambda: [{"kind": "research", "key": "test-question", "version": "a"*64}]
        self.bot._research_tools = AsyncMock(return_value=True)
        await self.capture()
        await a.tick()
        self.bot._research_tools.assert_not_called()
        self.assertFalse(a.proactive_allowed())
        self.assertEqual(a.store.state(OWNER)["cycle_count"], 0)

    async def test_background_interruption_keeps_focus_and_refuses_progress(self):
        a = self.bot._attention
        candidate = {"kind": "research", "key": "test-question", "version": "a"*64}
        a.candidates = lambda: [candidate]
        started, release = asyncio.Event(), asyncio.Event()
        async def research(**kwargs):
            self.assertTrue(kwargs["external_valid"]())
            started.set()
            await release.wait()
            self.assertFalse(kwargs["external_valid"]())
            return True
        self.bot._research_tools = AsyncMock(side_effect=research)
        task = asyncio.create_task(a.tick())
        await started.wait()
        await self.capture("我想先和你说会话")
        release.set()
        await task
        state = a.store.state(OWNER)
        self.assertEqual(state["focus"], candidate)
        self.assertEqual(state["last_outcome"], "interrupted")
        self.assertEqual(state["cycle_count"], 1)

    async def test_one_action_per_tick_and_no_raw_focus_in_inspection(self):
        a = self.bot._attention
        a.candidates = lambda: [{"kind": "research", "key": "private-secret-key", "version": "a"*64},
                                {"kind": "reading", "key": "other", "version": "b"*64}]
        self.bot._research_tools = AsyncMock(return_value=True)
        await a.tick()
        self.bot._research_tools.assert_awaited_once()
        self.assertEqual(a.store.state(OWNER)["last_outcome"], "progress")
        await self.capture()
        data = await self.bot.attention(**self.kwargs())
        self.assertTrue(data["success"])
        self.assertNotIn("private-secret", json.dumps(data))
        self.assertNotIn(OWNER, json.dumps(data))

    async def test_source_retraction_during_background_invalidates_callback(self):
        a = self.bot._attention
        rows = [{"kind": "research", "key": "question", "version": "a"*64}]
        a.candidates = lambda: list(rows)
        async def research(**kwargs):
            rows.clear()
            self.assertFalse(kwargs["external_valid"]())
            return False
        self.bot._research_tools = AsyncMock(side_effect=research)
        await a.tick()
        self.assertNotEqual(a.store.state(OWNER)["last_outcome"], "progress")
        self.assertFalse(a.snapshot()["focus_available"])

    async def test_public_reader_configuration_change_invalidates_running_focus(self):
        a = self.bot._attention
        a.candidates = lambda: [{"kind": "research", "key": "question", "version": "a"*64}]
        activity_signature = a._activity_signature()
        async def research(**kwargs):
            self.assertTrue(kwargs["external_valid"]())
            self.bot.config.mcp.public_sources_enabled = True
            self.assertFalse(kwargs["external_valid"]())
            return True
        self.bot._research_tools = AsyncMock(side_effect=research)
        await a.tick()
        self.assertNotEqual(a._activity_signature(), activity_signature)
        self.assertEqual(a.store.state(OWNER)["last_outcome"], "interrupted")

    async def test_activity_source_signature_survives_runtime_generation(self):
        attention = self.bot._attention
        activity_signature, running_signature = attention._activity_signature(), attention.signature()
        self.bot._mcp_generation += 1
        self.assertEqual(attention._activity_signature(), activity_signature)
        self.assertNotEqual(attention.signature(), running_signature)

    async def test_group_reply_target_remains_exact(self):
        await self.capture("一起聊聊", "owner-group", group=GROUP, sid="group-one")
        await self.capture("我是主人", "other-group", group=GROUP, uid=OTHER, sid="group-one")
        context = self.bot._unified.context(self.kwargs("owner-group", "group-one"))
        self.assertEqual(context["speaker_id"], OWNER)
        self.assertEqual(self.bot._attention.hint(self.kwargs("owner-group", "group-one")), "")

    async def test_current_mode_wins_old_continuity_support_without_two_conflicting_hints(self):
        self.bot.config.continuity.enabled = True
        self.bot.config.continuity.auto_topics = False
        await self.capture("帮我想想办法")
        await self.bot._attention.prepare_dialogue(self.kwargs())
        text = "我只是想吐槽一下今天的事，不用帮我分析原因。"
        await self.capture(text, "m2")
        self.bot.ctx.llm.generate = AsyncMock(return_value=self.answer("listen", text))
        result = await self.bot.brain_reply_hint(**self.kwargs("m2"))
        self.assertIn("先接住", json.dumps(result, ensure_ascii=False))
        owner_hint = self.bot._owner_prompt_for(self.kwargs("m2"))
        self.assertNotIn("当前明确在求建议", owner_hint)
        await self.capture("聊点轻松的吧", "m3")
        await self.bot._attention.prepare_dialogue(self.kwargs("m3"))
        self.assertIn("顺着当前话题接梗", self.bot._attention.hint(self.kwargs("m3")))
        self.bot.config.attention.dialogue_enabled = False
        self.assertEqual(self.bot._attention.hint(self.kwargs("m3")), "")
        self.assertIsNotNone(self.bot._ensure_attention())

    async def test_incomplete_explicit_message_cannot_use_session_fallback(self):
        await self.capture()
        for key in ("message_id", "session_id"):
            bad = message()
            bad.pop(key)
            result = await self.bot.attention(**self.kwargs(), message=bad)
            self.assertFalse(result["success"])

    async def test_plain_greetings_skip_model_and_media_interrupt_without_classification(self):
        await self.capture("你好")
        await self.bot._attention.prepare_dialogue(self.kwargs())
        self.bot.ctx.llm.generate.assert_not_called()
        a = self.bot._attention
        state = a.store.state(OWNER)
        event = message(mid="picture", text="你理解错了")
        event["raw_message"] = [{"type": "image", "data": {"url": "untrusted"}}]
        event["is_picture"] = True
        await self.bot.capture_unified(message=event)
        self.assertGreater(a.store.state(OWNER)["revision"], state["revision"])
        await a.prepare_dialogue(self.kwargs("picture"))
        self.bot.ctx.llm.generate.assert_not_called()
        self.assertNotIn("具体纠正", a.hint(self.kwargs("picture")))


if __name__ == "__main__":
    unittest.main()
