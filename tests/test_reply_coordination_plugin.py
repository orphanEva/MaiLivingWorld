"""The real foreground hooks coordinate hints without changing task state."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from MaiLivingWorld.tests.test_runtime import FakeContext, plugin
from MaiLivingWorld.tests.test_mcp_plugin import OWNER, OTHER, GROUP, message
from MaiLivingWorld.reply_policy import MARKER


TASK = "[verified-test-task]已有一项任务结果待主人查看"


class ReplyCoordinationPluginTests(unittest.IsolatedAsyncioTestCase):
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
        b.config.unified_mind.max_daily_model_calls = 0
        b.config.attention.reply_coordination = True
        b.config.advice.enabled = True
        b.config.brain_memory.private_users = [OTHER]
        b.config.brain_memory.group_ids = [GROUP]
        b._ensure_unified()
        return b

    async def asyncTearDown(self):
        await self.bot.on_unload()
        self.temp.cleanup()

    async def capture(self, text, mid="m1", **kw):
        event = message(mid=mid, text=text, **kw)
        await self.bot.capture_owner(message=event)
        await self.bot.capture_unified(message=event)
        return {"session_id": event["session_id"], "reply_message_id": mid,
                "messages": [{"role": "system", "content": "宿主原始系统提示"}]}

    async def output(self, request, *, planner=False):
        def task(kwargs):
            return TASK if self.bot._attention.trusted(kwargs) else ""
        with patch.object(self.bot, "_followthrough_hint", side_effect=task):
            value = (await self.bot.owner_planner_prompt(**request) if planner
                     else await self.bot.brain_reply_hint(**request))
        return json.dumps(value, ensure_ascii=False)

    async def test_listening_defers_optional_interruptions_then_current_advice_wins(self):
        request = await self.capture("先听我说就好")
        heard = await self.output(request)
        self.assertIn(MARKER, heard)
        self.assertIn("本轮以倾听和陪伴为主", heard)
        self.assertNotIn(TASK, heard)
        self.assertNotIn("[MaiLivingWorld:advice]", heard)
        self.assertIn("continuous-self", heard)
        self.assertIn("宿主原始系统提示", heard)
        changed = await self.output(await self.capture("帮我想想办法", "m2"))
        self.assertIn("本轮以协助解决当前问题为主", changed)
        self.assertIn("[MaiLivingWorld:advice]", changed)
        self.assertNotIn(TASK, changed)
        self.bot.ctx.llm.generate.assert_not_awaited()

    async def test_planner_uses_same_verified_intent_without_marking_anything_sent(self):
        request = await self.capture("先听我说就好")
        await self.bot._attention.prepare_dialogue(request)
        state = self.bot._attention.store.state(OWNER)
        result = await self.output(request, planner=True)
        self.assertIn("本轮以倾听和陪伴为主", result)
        self.assertNotIn(TASK, result)
        self.assertNotIn("[MaiLivingWorld:advice]", result)
        self.assertEqual(self.bot._attention.store.state(OWNER), state)
        self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()

    async def test_old_listen_is_overridden_only_for_current_news_or_activity_question(self):
        request = await self.capture("先听我说就好")
        await self.output(request)
        result = await self.output(await self.capture("我考试通过了", "news"))
        self.assertIn("本轮先回应主人明确分享的好消息", result)
        self.assertNotIn(TASK, result)
        self.bot.config.attention.activity_awareness = True
        result = await self.output(await self.capture("你在忙什么", "activity"))
        self.assertIn("本轮先回答主人对近期活动的明确询问", result)
        self.assertIn(TASK, result)
        result = await self.output(await self.capture("还有一点想说", "again"))
        self.assertIn("本轮以倾听和陪伴为主", result)
        self.assertNotIn(TASK, result)

    async def test_default_off_preserves_previous_hints_and_observe_does_not_inject(self):
        self.bot.config.attention.reply_coordination = False
        request = await self.capture("先听我说就好")
        result = await self.output(request)
        self.assertNotIn(MARKER, result)
        self.assertIn(TASK, result)
        self.assertIn("[MaiLivingWorld:advice]", result)
        self.bot.config.attention.reply_coordination = True
        self.bot.config.unified_mind.mode = "observe"
        result = await self.output(request)
        self.assertNotIn(MARKER, result)
        self.assertNotIn(TASK, result)

    async def test_group_other_owner_old_target_and_unknown_context_have_no_coordination(self):
        old = await self.capture("先听我说就好", "old")
        await self.capture("帮我想想办法", "new")
        requests = [old, {"session_id": "unknown"}]
        requests.append(await self.capture("先听我说就好", "other", uid=OTHER, sid="other-private"))
        requests.append(await self.capture("先听我说就好", "group", group=GROUP, sid="group-one"))
        for request in requests:
            result = await self.output(request)
            self.assertNotIn(MARKER, result)
            self.assertNotIn(TASK, result)

    async def test_owner_input_change_during_recall_discards_old_composition(self):
        request = await self.capture("先听我说就好", "old")
        async def recalling(*args, **kw):
            await self.capture("帮我想想办法", "new")
            return "OLD_PRIVATE_CONTEXT"
        with patch.object(self.bot._unified, "reply_hint", new=AsyncMock(side_effect=recalling)):
            result = await self.output(request)
        self.assertNotIn(MARKER, result)
        self.assertNotIn("OLD_PRIVATE_CONTEXT", result)
        self.assertNotIn(TASK, result)

    async def test_boundary_hints_survive_listening_and_restart(self):
        request = await self.capture("先听我说就好", "before")
        await self.output(request)
        await self.bot.on_unload()
        self.bot = self.make_bot()
        self.bot.config.shared_reading.enabled = self.bot.config.repair.enabled = True
        shared = self.bot._ensure_shared_reading()
        repair = self.bot._ensure_repair()
        request = await self.capture("还有一点想说", "after", sid="new-private")
        with patch.object(shared, "hint", return_value="[test-boundary]不透露已确认阅读位置后的内容"), \
             patch.object(repair, "hint", return_value="[test-repair]此处是已核验的具体修正依据"):
            result = await self.output(request)
        self.assertIn("本轮以倾听和陪伴为主", result)
        self.assertIn("[test-boundary]", result)
        self.assertIn("[test-repair]", result)
        self.assertNotIn(TASK, result)


if __name__ == "__main__":
    unittest.main()
