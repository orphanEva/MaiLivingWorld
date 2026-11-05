"""Real activity begins at provider dispatch after budgets and permission checks."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from MaiLivingWorld.mcp_runtime import MCPBridge
from MaiLivingWorld.tool_store import ToolStore
from MaiLivingWorld.tests.test_mcp_bridge import OWNER, config, context, FakeClient
from MaiLivingWorld.tests.test_projects_model import SOURCE, VALUE, response
from MaiLivingWorld.projects_model import understand_project
from MaiLivingWorld.attention_runtime import AttentionRuntime
from MaiLivingWorld.mcp_runtime import day_start
from MaiLivingWorld.tests import test_projects_runtime as project_fixtures


class ActivityDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ToolStore(Path(self.temp.name)/"tools.sqlite3")
        self.bridge = MCPBridge(self.store)
        self.client = FakeClient()

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def execute(self, dispatch, *, cfg=None, valid=lambda: True, mid="m1"):
        return await self.bridge.execute("pixiv", "get_library_work", {"kind": "novel", "pixiv_id": 123},
            config=cfg or config(), context=context(mid=mid), owner=OWNER, client=self.client,
            valid=valid, timezone="Asia/Shanghai", on_dispatch=dispatch)

    async def test_no_budget_or_duplicate_never_marks_dispatch(self):
        callback = Mock(return_value=True)
        await self.execute(callback, cfg=config(max_daily_calls=0))
        callback.assert_not_called()
        self.assertEqual(self.client.calls, [])
        await self.execute(callback, mid="m2")
        callback.assert_called_once()
        await self.execute(callback, mid="m2")
        callback.assert_called_once()
        self.assertEqual(len(self.client.calls), 1)

    async def test_dispatch_denial_or_context_change_prevents_actual_tool_call(self):
        callback = Mock(return_value=False)
        await self.execute(callback)
        callback.assert_called_once()
        self.assertEqual(self.client.calls, [])
        valid = True
        def changed():
            nonlocal valid
            valid = False
            return True
        await self.execute(changed, valid=lambda: valid, mid="m2")
        self.assertEqual(self.client.calls, [])

    async def test_dispatch_occurs_after_schema_check_and_immediately_before_tool(self):
        order = []
        async def before_list():
            order.append("list")
        async def before_call():
            order.append("call")
        self.client.on_list, self.client.on_call = before_list, before_call
        await self.execute(lambda: order.append("dispatch") or True)
        self.assertEqual(order, ["list", "dispatch", "call"])
        self.client.schemas.pop("get_library_work")
        callback = Mock(return_value=True)
        await self.execute(callback, mid="absent")
        callback.assert_not_called()

    async def test_project_model_dispatch_follows_budget_and_precedes_each_real_call(self):
        model = AsyncMock(side_effect=[response(VALUE), response({"supported": True})])
        callback = Mock(return_value=True)
        options = dict(generate=model, valid=lambda: True, task_name="test", on_dispatch=callback)
        self.assertIsNone(await understand_project("天文主题", [SOURCE], reserve=lambda: False, **options))
        callback.assert_not_called()
        model.assert_not_awaited()
        self.assertEqual(await understand_project("天文主题", [SOURCE], reserve=lambda: True, **options), VALUE)
        self.assertEqual(callback.call_count, 2)
        self.assertEqual(model.await_count, 2)
        callback.return_value = False
        self.assertIsNone(await understand_project("天文主题", [SOURCE], reserve=lambda: True, **options))
        self.assertEqual(model.await_count, 2)


class ActualProjectActivityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bot, self.runtime = project_fixtures.ProjectsRuntimeTests.make_bot(self)
        self.source = project_fixtures.ProjectsRuntimeTests.seed_growth(self)
        self.bot.config.attention.enabled = self.bot.config.attention.activity_awareness = True
        self.bot.config.attention.interval_seconds = 0
        self.attention = self.bot._ensure_attention()
        self.runtime.prepare()

    async def asyncTearDown(self):
        await self.bot.on_unload()
        self.temp.cleanup()

    async def test_real_gather_and_inconclusive_review_are_verified_steps_not_finished_claims(self):
        await self.attention.tick()
        self.assertEqual(self.attention.last_status, "progress")
        first = self.attention.store.state(OWNER)
        self.assertGreater(first["activity"]["load"], .24)
        project_fixtures.ProjectsRuntimeTests.model(self)
        now = max(time.time(), self.runtime.store.state(OWNER)["last_action"]+302)
        with patch("time.time", return_value=now):
            await self.attention.tick()
            row = self.runtime.store.items(OWNER)[0]
            self.assertEqual(row["status"], "unresolved")
            self.assertEqual(self.attention.last_status, "progress")
            self.assertGreater(self.attention.store.state(OWNER)["activity"]["load"], .45)
            finishes = [e for e in self.attention.store.events(OWNER) if e["action"] == "finish"]
            self.assertEqual(len(finishes), 2)
            self.assertTrue(all(e["activity"]["proof"] for e in finishes))

    async def test_project_own_budget_and_tool_budget_leave_no_actual_start(self):
        self.bot.config.projects.max_daily_actions = 0
        await self.attention.tick()
        self.assertFalse(any(e["action"] == "activity_start" for e in self.attention.store.events(OWNER)))
        self.bot.config.projects.max_daily_actions = 6
        self.bot.config.mcp.max_daily_calls = 0
        await self.attention.tick()
        self.assertFalse(any(e["action"] == "activity_start" for e in self.attention.store.events(OWNER)))
        self.assertEqual(self.attention.store.state(OWNER)["activity"]["load"], 0)
        self.assertEqual(self.bot._mcp_clients["pixiv"].calls, [])

    async def test_snapshot_needs_actual_dispatch_and_does_not_inherit_working_after_restart(self):
        attention = self.attention
        claim = attention.store.claim(OWNER, candidates=attention.candidates(), daily_limit=20,
            interval_seconds=0, day_start=day_start(self.bot.config.general.timezone), activity_awareness=True)
        self.assertIsNotNone(claim)
        self.assertFalse(attention.snapshot()["working"])
        record = attention._activity_record(claim["focus"])
        attention.store.start_activity(OWNER, claim["token"], proof=record["proof"], signature=attention._activity_signature())
        attention._active_activity = attention.store.state(OWNER)["lease"]["activity"]["token_hash"]
        self.assertTrue(attention.snapshot()["working"])
        reopened = AttentionRuntime(self.bot)
        try:
            self.assertFalse(reopened.snapshot()["working"])
        finally:
            reopened.close()
