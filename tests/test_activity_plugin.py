"""Real plugin, reading ledger and offline MCP activity, without a paid model or QQ."""
import asyncio
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from MaiLivingWorld.tests.test_runtime import FakeContext, plugin
from MaiLivingWorld.tests.test_mcp_plugin import message, OWNER, OTHER, GROUP
from MaiLivingWorld.tests.test_companion_runtime import NovelMCP


class ActivityPluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = time.time()
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
        b.config.attention.activity_awareness = True
        b.config.attention.interval_seconds = 30
        b.config.mcp.enabled = b.config.mcp_pixiv.enabled = b.config.reading.enabled = True
        b.config.reading.auto_continue = True
        b.config.reading.interval_seconds = 60
        b.config.reading.excerpt_chars = 100
        b.config.brain_memory.private_users = [OTHER]
        b.config.brain_memory.group_ids = [GROUP]
        b._mcp_clients["pixiv"] = NovelMCP()
        b._ensure_unified()
        b._ensure_attention()
        return b

    async def asyncTearDown(self):
        await self.bot.on_unload()
        self.temp.cleanup()

    async def capture(self, mid, text="你在忙什么", extra=None, **kw):
        with patch("time.time", return_value=self.now):
            event = message(mid=mid, text=text, **kw)
            event.update(extra or {})
            await self.bot.capture_owner(message=event)
            await self.bot.capture_unified(message=event)
        return {"session_id": kw.get("sid", "private-one"), "reply_message_id": mid,
                "messages": [{"role": "system", "content": "宿主原始提示"}]}

    async def opened(self):
        await self.capture("open", "读点资料吧")
        with patch("time.time", return_value=self.now):
            result = await self.bot._ensure_companion().read("open", "pixiv", "novel", "123", "", {"session_id": "private-one"})
        self.assertTrue(result["success"], result)
        return result["data"]["id"]

    async def tick(self):
        self.now += 100
        with patch("time.time", return_value=self.now):
            await self.bot._attention.tick()

    async def ask(self, mid="ask", text="你在忙什么", **kw):
        kwargs = await self.capture(mid, text, **kw)
        with patch("time.time", return_value=self.now):
            return json.dumps(await self.bot.brain_reply_hint(**kwargs), ensure_ascii=False)

    async def test_real_reading_progress_survives_restart_without_exposing_work_content(self):
        rid = await self.opened()
        await self.tick()
        self.assertEqual(self.bot._companion.store.reading(rid)["cursor"], 100)
        self.assertEqual(self.bot._attention.store.state(OWNER, now=self.now)["activity"]["load"], .25)
        result = await self.ask()
        self.assertIn("最近完成了一步处理", result)
        self.assertIn("阅读资料", result)
        for private in ("森林旅人", "旅人走过森林", rid, OWNER):
            self.assertNotIn(private, result)
        self.bot.ctx.llm.generate.assert_not_awaited()
        await self.bot.on_unload()
        self.bot = self.make_bot()
        self.assertIn("最近完成了一步处理", await self.ask("restart"))
        self.bot.ctx.llm.generate.assert_not_awaited()

    async def test_current_activity_question_overrides_old_listen_and_then_restores_it(self):
        kwargs = await self.capture("listen", "先听我说就好")
        with patch("time.time", return_value=self.now):
            await self.bot._attention.prepare_dialogue(kwargs)
        result = await self.ask()
        self.assertIn("没有可核验的近期活动", result)
        self.assertNotIn("先接住对方具体说出的感受", result)
        later = await self.capture("later", "还有一些事情")
        with patch("time.time", return_value=self.now):
            self.assertEqual(self.bot._attention.effective_mode(later), "listen")
        self.bot.ctx.llm.generate.assert_not_awaited()

    async def test_queued_work_or_success_without_cursor_progress_is_not_completed_activity(self):
        await self.opened()
        async def read(**kwargs):
            self.assertTrue(kwargs["on_dispatch"]())
            return {"success": True}
        self.bot._companion.auto_read = read
        await self.tick()
        self.assertEqual(self.bot._attention.last_status, "blocked")
        self.assertEqual(self.bot._attention.store.state(OWNER, now=self.now)["activity"]["load"], 0)
        result = await self.ask()
        self.assertIn("没有确认推进成功", result)
        self.assertNotIn("最近完成了一步处理", result)

    async def test_current_owner_interrupts_started_work_without_blame_or_progress(self):
        await self.opened()
        started, release = asyncio.Event(), asyncio.Event()
        async def read(**kwargs):
            self.assertTrue(kwargs["on_dispatch"]())
            started.set()
            await release.wait()
            return {"success": True}
        self.bot._companion.auto_read = read
        self.now += 100
        with patch("time.time", return_value=self.now):
            task = asyncio.create_task(self.bot._attention.tick())
            try:
                await asyncio.wait_for(started.wait(), 3)
                result = await self.ask()
                self.assertIn("这一步已暂停或中断", result)
            finally:
                release.set()
                await task
        self.assertEqual(self.bot._attention.store.state(OWNER, now=self.now)["activity"]["load"], 0)
        self.assertIn("这一步已暂停或中断", await self.ask("again"))

    async def test_real_child_budget_rejection_is_not_an_activity_start(self):
        self.bot.config.reading.max_daily_units = 0
        await self.opened()
        before_calls = len(self.bot._mcp_clients["pixiv"].calls)
        await self.tick()
        self.assertEqual(len(self.bot._mcp_clients["pixiv"].calls), before_calls)
        self.assertFalse(any(e["action"] == "activity_start" for e in self.bot._attention.store.events(OWNER)))
        self.assertEqual(self.bot._attention.store.state(OWNER, now=self.now)["activity"]["load"], 0)
        self.assertIn("没有可核验的近期活动", await self.ask())

    async def test_unknown_media_quotes_group_and_other_person_get_no_activity_hint(self):
        await self.opened()
        await self.tick()
        for mid, text, kw in (("quote", "他说你在忙什么", {}), ("ambiguous", "进展怎么样", {}),
                              ("picture", "你在忙什么", {"extra": {"is_picture": True}}),
                              ("other", "你在忙什么", {"uid": OTHER, "sid": "other-private"}),
                              ("group", "你在忙什么", {"group": GROUP, "sid": "group-stream"})):
            with self.subTest(mid=mid):
                self.assertNotIn("actual-activity", await self.ask(mid, text, **kw))

    async def test_retracted_source_expiry_and_disabled_provider_hide_recent_activity(self):
        rid = await self.opened()
        await self.tick()
        self.now += 1801
        self.assertIn("没有可核验的近期活动", await self.ask("expired"))
        self.now -= 1801
        self.bot.config.mcp_pixiv.enabled = False
        self.assertIn("没有可核验的近期活动", await self.ask("provider-off"))
        self.bot.config.mcp_pixiv.enabled = True
        with self.bot._companion.store.con:
            self.bot._companion.store.con.execute("DELETE FROM companion_readings WHERE id=?", (rid,))
        self.assertIn("没有可核验的近期活动", await self.ask("removed"))

    async def test_high_load_stops_background_but_does_not_block_a_current_reply(self):
        await self.opened()
        await self.tick()
        # A synthetic high-load snapshot with a real, still-available reading.
        a = self.bot._attention
        state = a.store.state(OWNER, now=self.now)
        self.assertEqual(state["activity"]["load"], .25)
        self.assertTrue(a.candidates())
        with a.store.con:
            row = a.store.con.execute("SELECT payload FROM attention_control WHERE owner_id=?", (OWNER,)).fetchone()
            payload = json.loads(row[0])
            payload["activity"] = {"load": 1., "at": self.now}
            a.store.con.execute("UPDATE attention_control SET payload=? WHERE owner_id=?", (json.dumps(payload), OWNER))
        calls = len(self.bot._mcp_clients["pixiv"].calls)
        await self.tick()
        self.assertEqual(len(self.bot._mcp_clients["pixiv"].calls), calls)
        result = await self.ask()
        self.assertIn("actual-activity", result)
        self.assertIn("当前对话照常", result)
        self.bot.ctx.llm.generate.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
