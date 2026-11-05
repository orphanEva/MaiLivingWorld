"""Scheduler routing: an attempted project consumes one background slot."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from MaiLivingWorld.tests.test_runtime import FakeContext, plugin


class BackgroundProjectsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = plugin.MaiLivingWorld()
        self.bot.ctx = FakeContext()
        self.bot.config = plugin.Config()
        self.bot.config.reading.enabled = self.bot.config.reading.auto_continue = True
        self.bot._ensure_attention = Mock(return_value=None)
        self.state, self.version, self.busy = {"last_action": 0}, 1, False
        self.projects = SimpleNamespace(active_reading_ids=lambda: {"claimed"}, signature=lambda: self.version,
            store=SimpleNamespace(state=lambda owner: dict(self.state)), owner=lambda: "12345678",
            engaged=lambda: self.busy, tick=AsyncMock(return_value=None))
        self.bot._ensure_projects = Mock(return_value=self.projects)
        self.companion = SimpleNamespace(auto_read=AsyncMock(), store=SimpleNamespace(readings=Mock(return_value=[
            {"id": "claimed"}, {"id": "ordinary"}])))
        self.bot._ensure_companion = Mock(return_value=self.companion)
        self.bot._research_tools = AsyncMock(return_value=None)

    async def test_real_attempt_prevents_second_read_even_if_interrupted(self):
        async def attempt():
            self.state["last_action"] = 1
            return None
        self.projects.tick.side_effect = attempt
        await self.bot._advance_background()
        self.companion.auto_read.assert_not_awaited()

    async def test_success_or_failure_each_uses_the_slot(self):
        for success in (True, False):
            self.projects.tick.return_value = {"success": success}
            await self.bot._advance_background()
        self.companion.auto_read.assert_not_awaited()

    async def test_waiting_project_leaves_unclaimed_reading_and_interrupt_guard(self):
        async def read(**kwargs):
            self.assertEqual(kwargs["reading_id"], "ordinary")
            self.assertTrue(kwargs["external_valid"]())
            self.version += 1
            self.busy = True
            self.assertFalse(kwargs["external_valid"]())
        self.companion.auto_read.side_effect = read
        await self.bot._advance_background()
        self.companion.auto_read.assert_awaited_once()

    async def test_current_owner_conversation_or_only_claimed_reading_blocks_fallback(self):
        self.busy = True
        await self.bot._advance_background()
        self.busy = False
        self.companion.store.readings.return_value = [{"id": "claimed"}]
        await self.bot._advance_background()
        self.companion.auto_read.assert_not_awaited()

    async def test_attention_is_the_only_dispatcher_when_enabled(self):
        attention = SimpleNamespace(tick=AsyncMock())
        self.bot._ensure_attention.return_value = attention
        await self.bot._advance_background()
        attention.tick.assert_awaited_once()
        self.projects.tick.assert_not_awaited()
        self.companion.auto_read.assert_not_awaited()

    async def test_ordinary_research_only_uses_an_idle_slot_and_blocks_extra_read(self):
        self.bot._research_tools.return_value = True
        await self.bot._advance_background()
        self.bot._research_tools.assert_awaited_once()
        check = self.bot._research_tools.call_args.kwargs["external_valid"]
        self.assertTrue(check())
        self.busy = True
        self.assertFalse(check())
        self.companion.auto_read.assert_not_awaited()
        self.bot._research_tools.reset_mock()
        await self.bot._advance_background()
        self.bot._research_tools.assert_not_awaited()

    def test_project_backlog_cannot_displace_relevant_existing_interest(self):
        growth = {"id": "interest", "text": "我喜欢星空摄影", "score": 0}
        self.bot._collect_roots = Mock(return_value=[{"id": "original", "text": "原始证据"}])
        self.bot._derived_memories = Mock(return_value=[growth])
        self.projects.records = Mock(return_value=[{"id": str(i), "text": "森林旅人查阅结果", "score": 999}
                                                  for i in range(12)])
        self.assertEqual(self.bot._collect_unified("星空摄影")[0], growth)
        self.assertNotEqual(self.bot._collect_unified("森林旅人")[0], growth)

    async def test_newly_prepared_project_protects_its_reading_during_cooldown(self):
        async def prepare_then_wait():
            self.projects.active_reading_ids = lambda: {"claimed", "ordinary"}
        self.projects.tick.side_effect = prepare_then_wait
        await self.bot._advance_background()
        self.companion.auto_read.assert_not_awaited()
