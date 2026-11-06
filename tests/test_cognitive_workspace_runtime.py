"""Workspace integration through real owner receipts, reading and memory stores."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from MaiLivingWorld.tests import test_activity_plugin as activity
from MaiLivingWorld.tests import test_followthrough_plugin as followthrough
from MaiLivingWorld.tests import test_projects_runtime as projects
from MaiLivingWorld.tests.test_mcp_plugin import OWNER, OTHER, GROUP


class WorkspaceRuntimeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = activity.ActivityPluginTests.asyncSetUp
    asyncTearDown = activity.ActivityPluginTests.asyncTearDown
    capture = activity.ActivityPluginTests.capture
    opened = activity.ActivityPluginTests.opened
    tick = activity.ActivityPluginTests.tick

    def make_bot(self):
        bot = activity.ActivityPluginTests.make_bot(self)
        bot.config.attention.integrated_workspace = True
        bot.config.attention.adaptive_motivation = True
        return bot

    async def test_real_reading_receipt_and_workspace_survive_new_session_and_restart(self):
        rid = await self.opened()
        await self.tick()
        runtime = self.bot._attention
        history = runtime.store.state(OWNER, self.now)["capabilities"]["reading"]
        self.assertEqual(history[0]["outcome"], "progress")
        self.assertEqual(runtime.snapshot()["capabilities"]["reading"]["attempts"], 1)
        await self.bot.on_unload()
        self.bot = self.make_bot()
        kwargs = await self.capture("next", "接着聊一会", sid="private-two")
        with patch("time.time", return_value=self.now):
            view = self.bot._attention.workspace(kwargs)
            hint = self.bot._attention.hint(kwargs)
        self.assertEqual(view["focus"]["key"], rid)
        self.assertEqual(view["current_goal"]["message_id"], "next")
        self.assertEqual(view["capabilities"]["reading"]["attempts"], 1)
        self.assertEqual(view["pause_reason"], "owner_message")
        self.assertIn("working-memory", hint)
        self.assertNotIn("森林旅人", hint)
        self.assertNotIn(rid, hint)
        raw = self.bot._attention.store.con.execute("SELECT payload FROM attention_control WHERE owner_id=?", (OWNER,)).fetchone()[0]
        self.assertNotIn("接着聊一会", raw)

    async def test_source_retraction_hides_focus_and_experience_and_other_audiences(self):
        await self.opened()
        await self.tick()
        kwargs = await self.capture("next")
        self.bot._companion._current_refs = lambda row: []
        with patch("time.time", return_value=self.now):
            view = self.bot._attention.workspace(kwargs)
        self.assertIsNone(view["focus"])
        self.assertEqual(view["capabilities"]["reading"]["attempts"], 0)
        other = await self.capture("other", uid=OTHER, sid="other")
        group = await self.capture("group", group=GROUP, sid="group")
        self.assertIsNone(self.bot._attention.workspace(other))
        self.assertIsNone(self.bot._attention.workspace(group))

    async def test_owner_interruption_during_real_dispatch_does_not_teach_failure(self):
        await self.opened()
        started, release = asyncio.Event(), asyncio.Event()
        runtime = self.bot._attention
        async def blocked_read(**kwargs):
            self.assertTrue(kwargs["on_dispatch"]())
            started.set()
            await release.wait()
            raise RuntimeError("simulated connection failure after owner spoke")
        self.bot._companion.auto_read = blocked_read
        task = asyncio.create_task(self.tick())
        await asyncio.wait_for(started.wait(), 3)
        await self.capture("interrupt", "先陪我聊聊")
        release.set()
        await task
        self.assertEqual(runtime.store.state(OWNER, self.now)["capabilities"], {})

    async def test_research_transport_success_without_evidence_is_blocked(self):
        b = self.bot
        b.config.growth.enabled = True
        b.config.growth.mode = "active"
        b._research_available = lambda question=None: True
        b._growth_snapshot = lambda: {"questions": [{"key": "question:game", "topic": "游戏", "status": "open"}]}
        async def research(**kwargs):
            self.assertTrue(kwargs.get("with_evidence"))
            self.assertTrue(kwargs["on_dispatch"]())
            return {"success": True, "records": []}
        b._research_tools = research
        await self.tick()
        history = b._attention.store.state(OWNER, self.now)["capabilities"]["research"]
        self.assertEqual(history[0]["outcome"], "blocked")
        self.assertEqual(b._attention.store.state(OWNER, self.now)["activity"]["load"], 0)

    async def test_validated_emotions_change_ranking_and_expiry_reverts(self):
        b = self.bot
        b.config.growth.enabled = True
        b.config.growth.mode = "active"
        b._research_available = lambda question=None: True
        state = {"questions": [{"key": "a", "topic": "森林", "status": "open"},
                               {"key": "b", "topic": "游戏", "status": "open"}],
                 "emotions": [{"topic": "游戏", "feeling": "curious", "intensity": .5}]}
        b._growth_snapshot = lambda: state
        self.assertEqual(b._attention.candidates()[0]["key"], "b")
        self.assertIn("emotion", b._attention._assessment["research:b"]["reasons"])
        state["emotions"] = []
        self.assertEqual(b._attention.candidates()[0]["key"], "a")

    async def test_three_real_blocked_attempts_offer_sourced_method_review(self):
        b = self.bot
        b.config.growth.enabled = True
        b.config.growth.mode = "active"
        b._research_available = lambda question=None: True
        question = {"key": "question:game", "topic": "游戏", "status": "open"}
        b._growth_snapshot = lambda: {"questions": [question]}
        async def research(**kwargs):
            self.assertTrue(kwargs["on_dispatch"]())
            return {"success": True, "records": []}
        b._research_tools = research
        for _ in range(3):
            await self.tick()
        kwargs = await self.capture("review", "你最近在忙什么")
        with patch("time.time", return_value=self.now):
            snapshot = b._attention.workspace(kwargs)
            hint = b._attention.workspace_hint(kwargs)
        self.assertEqual(snapshot["review_kinds"], ["research"])
        self.assertEqual(len(snapshot["capabilities"]["research"]["recent_receipts"]), 3)
        self.assertIn("换方法", hint)
        question["topic"] = "换了版本的问题"
        self.assertEqual(b._attention.workspace(kwargs)["capabilities"]["research"]["attempts"], 0)

    async def test_media_is_current_goal_and_untrusted_capture_is_ignored(self):
        kwargs = await self.capture("media", text="看看这段视频", extra={"raw_message": [{"type": "video", "file": "opaque-media"}]})
        with patch("time.time", return_value=self.now):
            self.assertEqual(self.bot._attention.workspace(kwargs)["current_goal"]["intent"], "media")
        self.assertFalse(self.bot._attention.capture(None))

    async def test_budget_refusal_and_false_success_without_dispatch_do_not_teach(self):
        await self.opened()
        self.bot._companion.auto_read = AsyncMock(return_value={"success": True})
        await self.tick()
        self.assertEqual(self.bot._attention.store.state(OWNER, self.now)["capabilities"], {})
        self.assertEqual(self.bot._attention.last_status, "blocked")


class WorkspaceCommitmentTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = followthrough.FollowthroughPluginTests.asyncSetUp
    asyncTearDown = followthrough.FollowthroughPluginTests.asyncTearDown
    capture = followthrough.FollowthroughPluginTests.capture
    draft = followthrough.FollowthroughPluginTests.draft
    sent = followthrough.FollowthroughPluginTests.sent

    def make_bot(self):
        bot = followthrough.FollowthroughPluginTests.make_bot(self)
        bot.config.attention.enabled = bot.config.attention.integrated_workspace = True
        return bot

    async def test_only_actual_delivered_current_promises_enter_workspace(self):
        result = await self.draft()
        rt = self.bot._attention
        kwargs = {"session_id": "private-one", "reply_message_id": "m1"}
        self.assertEqual(rt.workspace(kwargs)["pending_commitments"], 0)
        await self.sent(result["delivery_text"], sent=False)
        self.assertEqual(rt.workspace(kwargs)["pending_commitments"], 0)
        await self.sent(result["delivery_text"])
        self.assertEqual(rt.workspace(kwargs)["pending_commitments"], 1)
        self.assertEqual(rt.workspace(kwargs)["commitments"][0]["id"], result["data"]["id"])
        self.bot.config.mcp_cap.url = "http://replacement-service:8000/mcp"
        self.assertEqual(rt.workspace(kwargs)["pending_commitments"], 0)


class WorkspaceProjectTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = projects.ProjectsRuntimeTests.asyncSetUp
    asyncTearDown = projects.ProjectsRuntimeTests.asyncTearDown
    seed_growth = projects.ProjectsRuntimeTests.seed_growth
    capture = projects.ProjectsRuntimeTests.capture

    def make_bot(self):
        bot, runtime = projects.ProjectsRuntimeTests.make_bot(self)
        bot.config.attention.enabled = bot.config.attention.integrated_workspace = True
        bot.config.projects.adaptive_plans = True
        bot._ensure_attention()
        return bot, runtime

    async def test_project_pending_plan_and_pause_share_workspace_without_raw_content(self):
        self.capture()
        project = self.runtime.prepare()
        self.assertIsNotNone(project)
        kwargs = {"session_id": "private-one", "reply_message_id": "m1"}
        rt = self.bot._attention
        view = rt.workspace(kwargs)
        self.assertEqual(view["projects"][0]["ref"]["key"], project["id"])
        self.assertNotIn(project["seed"]["question"], str(view["projects"]))
        self.runtime.store.command(OWNER, project["id"], project["revision"], "pause", "a"*64, "b"*64)
        view = rt.workspace(kwargs)
        self.assertEqual(view["projects"][0]["status"], "paused")
        self.assertFalse(any(row["kind"] == "project" for row in view["pending"]))
        self.bot._resolve_growth_roots = lambda ids: {}
        self.assertEqual(rt.workspace(kwargs)["projects"], [])
