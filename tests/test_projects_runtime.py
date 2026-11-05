"""Actual growth, project ledgers and MCP reading/research with offline transports."""
from __future__ import annotations

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
from MaiLivingWorld.tests.test_mcp_research import LibraryClient
from MaiLivingWorld.tests.test_companion_runtime import NovelMCP, NOTE
from MaiLivingWorld.growth_model import empty_growth
from MaiLivingWorld.projects_runtime import ProjectsRuntime
from MaiLivingWorld.store import Store
from MaiLivingWorld.unified_expression import parse_expression


class Settings(SimpleNamespace):
    def model_dump(self):
        return dict(vars(self))


class ForestLibrary(LibraryClient):
    async def call_tool(self, name, arguments):
        value = await super().call_tool(name, arguments)
        if name == "get_library_work":
            value["title"] = "森林旅人"
            value["tags"] = ["森林", "旅途"]
        return value


class ProjectsRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bot, self.runtime = self.make_bot()
        self.source = self.seed_growth()

    def make_bot(self):
        b = plugin.MaiLivingWorld()
        b.ctx = FakeContext()
        b.ctx.paths = SimpleNamespace(data_dir=Path(self.temp.name))
        b.config = plugin.Config()
        if not hasattr(b.config, "projects"):
            object.__setattr__(b.config, "projects", Settings(enabled=False, interval_seconds=300, max_daily_actions=6, max_active_projects=2))
        b.config.owner.enabled = b.config.unified_mind.enabled = b.config.projects.enabled = b.config.growth.enabled = True
        b.config.owner.qq = OWNER
        b.config.unified_mind.mode = b.config.growth.mode = "active"
        b.config.projects.interval_seconds = 300
        b.config.mcp.enabled = b.config.mcp_pixiv.enabled = b.config.mcp.research_enabled = True
        b.config.mcp.research_interval_seconds = 60
        b.config.brain_memory.private_users = [OTHER]
        b.config.brain_memory.group_ids = [GROUP]
        b.config.unified_mind.min_interval_seconds = 0
        b._store = Store(Path(self.temp.name)/"living_world.sqlite3")
        b._ensure_unified()
        b._ensure_growth()
        b._mcp_clients["pixiv"] = ForestLibrary()
        runtime = ProjectsRuntime(b)
        b._projects = runtime
        if not hasattr(b, "_expression_records"):
            b._expression_records = lambda rows, context=None: rows
        return b, runtime

    async def asyncTearDown(self):
        if getattr(self.bot, "_projects", None) is not self.runtime:
            self.runtime.close()
        await self.bot.on_unload()
        self.temp.cleanup()

    def seed_growth(self, *, source_scope=None):
        source = self.bot._ensure_tool_store().put_observation("cap", "album:other" if source_scope else "album:original", "作品元信息包含森林旅人和旅途主题。",
            source_scope or "private:"+OWNER, subject_id=OTHER if source_scope else OWNER)
        data = empty_growth()
        data["interests"] = [{"topic": "森林旅人", "delta": .08, "evidence_ids": [source["id"]]}]
        data["questions"] = [{"topic": "森林旅人", "question": "森林旅人如何描写旅途？", "status": "open", "evidence_ids": [source["id"]]}]
        store = self.bot._growth
        store.enqueue([source])
        job = store.claim()
        store.complete(job["id"], job["token"], data, [source], expected_revision=store.state()["revision"])
        return source

    def capture(self, **kw):
        event = message(**kw)
        self.bot._unified.capture(event, private_users=[OWNER, OTHER], group_ids=[GROUP])
        return event

    def three_open_questions(self, *, select=True):
        for index, topic in enumerate(("森林旅人色彩", "森林旅人作者")):
            source = self.bot._tool_store.put_observation("cap", "album:question:"+str(index),
                "作品资料提到了"+topic, "private:"+OWNER, subject_id=OWNER)
            proposal = empty_growth()
            proposal["questions"] = [{"topic": topic, "question": topic+"提供了哪些线索？", "status": "open", "evidence_ids": [source["id"]]}]
            self.bot._growth.enqueue([source])
            job = self.bot._growth.claim()
            self.bot._growth.complete(job["id"], job["token"], proposal, [source], expected_revision=self.bot._growth.state()["revision"])
        self.assertEqual(len(self.bot._growth_snapshot()["questions"]), 3)
        if not select:
            return []
        self.runtime.prepare()
        self.runtime.prepare()
        rows = self.runtime.store.items(OWNER)
        self.assertEqual(len(rows), 2)
        return rows

    def model(self, *, approved=True, after_first=None):
        async def generate(prompt, **kw):
            data = json.loads(prompt.rsplit("\n", 1)[1])
            if "proposal" in data:
                return {"response": json.dumps({"supported": approved})}
            if after_first:
                after_first()
            record = data["records"][0]
            return {"response": json.dumps({"outcome": "insufficient", "conclusion": "本次有限资料不足以判断完整情节。",
                "quotes": [{"source_id": record["id"], "quote": record["text"][:100]}]}, ensure_ascii=False)}
        self.bot.ctx.llm.generate = AsyncMock(side_effect=generate)

    async def tick_later(self, *, project_id=None, external_valid=None):
        state = self.runtime.store.state(OWNER)
        now = max(time.time(), state["last_action"]+self.bot.config.projects.interval_seconds+2,
                  max((row["next_action"]+1 for row in self.runtime.store.items(OWNER)), default=0))
        with patch("time.time", return_value=now):
            return await self.runtime.tick(project_id=project_id, external_valid=external_valid)

    async def gathered(self):
        self.runtime.prepare()
        self.assertEqual(len(self.runtime.candidates()), 1)
        outcome = await self.runtime.tick()
        self.assertTrue(outcome["success"], (outcome, self.runtime.last_status))
        row = self.runtime.store.items(OWNER)[0]
        self.assertEqual(row["stage"], "review")
        return row

    async def test_autonomous_seed_real_research_two_pass_result_and_restart(self):
        row = await self.gathered()
        self.assertEqual([call[0] for call in self.bot._mcp_clients["pixiv"].calls], ["search_library", "get_library_work"])
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 0)
        self.model()
        outcome = await self.tick_later()
        self.assertTrue(outcome["success"])
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 2)
        records = self.runtime.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["evidence_status"], "inferred")
        self.assertTrue(records[0]["derived"])
        with self.assertRaises(ValueError):
            self.bot._resolve_roots([records[0]["id"]])
        await self.bot.on_unload()
        self.bot, self.runtime = self.make_bot()
        self.assertEqual(self.runtime.store.get(OWNER, row["id"])["status"], "unresolved")
        self.assertEqual(len(self.runtime.records()), 1)
        self.runtime.prepare()
        self.assertEqual(len(self.runtime.store.items(OWNER)), 1)

    async def test_empty_search_is_not_evidence_or_a_completed_project(self):
        self.bot._research_tools = AsyncMock(return_value={"success": True, "records": []})
        self.runtime.prepare()
        outcome = await self.runtime.tick()
        self.assertFalse(outcome["success"])
        row = self.runtime.store.items(OWNER)[0]
        self.assertEqual((row["stage"], row["result"], row["failures"]), ("gather", {}, 1))
        self.assertEqual(self.runtime.records(), [])
        self.bot.ctx.llm.generate.assert_not_called()

    async def test_existing_seed_evidence_cannot_be_relabelled_as_new_research(self):
        self.bot._research_tools = AsyncMock(return_value={"success": True, "records": [self.source]})
        await self.runtime.tick()
        self.assertEqual(self.runtime.store.items(OWNER)[0]["stage"], "gather")

    async def test_new_message_during_tool_or_model_blocks_commit(self):
        self.bot._mcp_clients["pixiv"].callback = lambda: self.capture(mid="interrupt")
        await self.runtime.tick()
        self.assertEqual(self.runtime.store.items(OWNER)[0]["stage"], "gather")
        self.bot._mcp_clients["pixiv"].callback = None
        await self.tick_later()
        self.model(after_first=lambda: self.capture(mid="model-interrupt"))
        await self.tick_later()
        self.assertEqual(self.runtime.store.items(OWNER)[0]["status"], "active")
        self.assertEqual(self.runtime.records(), [])

    async def test_source_retraction_invalidates_completed_result_and_active_focus(self):
        row = await self.gathered()
        self.model()
        await self.tick_later()
        self.bot._tool_store.retract_observation("cap", "album:original")
        self.assertEqual(self.runtime.records(), [])
        self.capture()
        item = self.runtime.inspect({"session_id": "private-one"})["data"][0]
        self.assertFalse(item["current"])
        self.assertNotIn("森林旅人", item["topic"])

    async def test_current_owner_and_private_commands_control_only_local_project(self):
        row = self.runtime.prepare()
        text = f"/项目 暂停 {row['id']} {row['revision']}"
        for change in ({"uid": OTHER}, {"group": GROUP}, {"text": "引用："+text}):
            self.assertFalse(self.runtime.capture(message(**({"text": text}|change))))
        media = message(text=text)
        media["raw_message"] = [{"type": "image"}]
        self.assertFalse(self.runtime.capture(media))
        self.assertTrue(self.runtime.capture(message(text=text, mid="pause")))
        self.assertEqual(self.runtime.candidates(), [])
        paused = self.runtime.store.get(OWNER, row["id"])
        self.assertTrue(self.runtime.capture(message(text=f"/项目 继续 {row['id']} {paused['revision']}", mid="resume")))
        self.assertFalse(self.runtime.capture(message(text=text, mid="pause")))
        self.bot.config.owner.qq = OTHER
        self.assertFalse(self.runtime.enabled())
        self.assertEqual(self.runtime.records(), [])

    async def test_attention_engagement_and_reserved_budget_leave_progress_pending(self):
        row = self.runtime.prepare()
        self.bot.config.attention.enabled = True
        event = self.capture()
        self.bot._ensure_attention().capture(event)
        await self.runtime.tick()
        self.assertEqual(self.runtime.store.state(OWNER)["daily_actions"], 0)
        self.bot.config.attention.enabled = False
        await self.runtime.tick()
        self.bot.config.unified_mind.max_daily_model_calls = self.bot.config.unified_mind.reserved_reply_calls
        self.model()
        await self.tick_later()
        self.bot.ctx.llm.generate.assert_not_called()
        current = self.runtime.store.get(OWNER, row["id"])
        self.assertEqual((current["status"], current["stage"], current["failures"]), ("active", "review", 0))

    async def test_private_media_occupies_attention_without_attention_feature_or_budget(self):
        self.assertFalse(self.bot.config.attention.enabled)
        event = message(text="", mid="image")
        event["raw_message"] = [{"type": "image", "data": "never-learn-this-media"}]
        signature = self.runtime.signature()
        self.assertFalse(self.runtime.capture(event))
        self.assertNotEqual(self.runtime.signature(), signature)
        self.assertIsNone(self.runtime.prepare())
        self.assertIsNone(await self.runtime.tick())
        self.assertEqual(self.runtime.store.items(OWNER), [])
        self.assertEqual(self.runtime.store.state(OWNER)["daily_actions"], 0)
        self.assertEqual(self.bot._mcp_clients["pixiv"].calls, [])
        self.bot.ctx.llm.generate.assert_not_called()
        token = self.runtime._activity_token
        self.runtime.capture(event)
        self.assertEqual(self.runtime._activity_token, token)
        with patch("time.monotonic", return_value=self.runtime._engaged_until+1):
            self.assertTrue((await self.runtime.tick())["success"])

    async def test_private_media_during_tool_invalidates_without_unified_text_capture(self):
        event = message(text="", mid="media-interrupt")
        event["is_voice"] = True
        self.bot._mcp_clients["pixiv"].callback = lambda: self.runtime.capture(event)
        self.assertIsNone(await self.runtime.tick())
        row = self.runtime.store.items(OWNER)[0]
        self.assertEqual((row["stage"], row["evidence_refs"], row["failures"]), ("gather", [], 0))
        self.assertEqual(row["lease_token"], "")

    async def test_nonowner_group_and_outbound_messages_do_not_occupy_attention(self):
        signature = self.runtime.signature()
        events = [message(uid=OTHER), message(group=GROUP), message() | {"account_id": OWNER}]
        for event in events:
            self.assertFalse(self.runtime.capture(event))
        self.assertEqual(self.runtime.signature(), signature)
        self.assertTrue((await self.runtime.tick())["success"])

    async def test_actual_existing_novel_unit_supplies_evidence_without_metadata_search(self):
        b = self.bot
        b.config.reading.enabled = b.config.reading.auto_continue = True
        b.config.reading.excerpt_chars = 100
        b._mcp_clients["pixiv"] = NovelMCP()
        self.capture()
        opened = await b._ensure_companion().read("open", "pixiv", "novel", "123", "", {"session_id": "private-one"})
        self.assertTrue(opened["success"])
        b.ctx.llm.generate = AsyncMock(side_effect=[{"response": json.dumps(NOTE)}, {"response": '{"supported":true}'}])
        row = await self.gathered()
        self.assertEqual(b._companion.store.reading(opened["data"]["id"])["cursor"], 100)
        originals = b._resolve_roots([ref["id"] for ref in row["evidence_refs"]])
        self.assertTrue(all("作品原文" in rec["text"] for rec in originals.values()))
        self.assertNotIn("search_library", [call[0] for call in b._mcp_clients["pixiv"].calls])

    async def test_focus_survives_own_lease_but_changes_after_real_progress(self):
        self.runtime.prepare()
        focus = self.runtime.candidates()[0]
        result = await self.runtime.tick(project_id=focus["key"], external_valid=lambda: focus in self.runtime.candidates())
        self.assertTrue(result["success"])
        self.assertNotIn(focus, self.runtime.candidates())

    async def test_inspect_respects_spoiler_filter_and_never_returns_raw_conclusions(self):
        await self.gathered()
        self.model()
        await self.tick_later()
        self.capture()
        self.bot._expression_records = lambda rows, context=None: []
        result = self.runtime.inspect({"session_id": "private-one"})
        self.assertTrue(result["success"])
        self.assertNotIn("森林旅人", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("有限资料", json.dumps(result, ensure_ascii=False))
        self.assertFalse(self.runtime.inspect({"session_id": "private-one", "message": {}})["success"])

    async def test_other_private_seed_remains_known_but_cannot_be_disclosed_to_owner(self):
        self.seed_growth(source_scope="private:"+OTHER)
        await self.gathered()
        self.model()
        await self.tick_later()
        records = self.runtime.records()
        self.assertEqual(len(records), 1)
        self.assertIn("private:"+OTHER, {ref["scope"] for ref in records[0]["sources"]})
        self.capture()
        inspected = self.runtime.inspect({"session_id": "private-one"})
        self.assertNotIn("森林旅人", json.dumps(inspected, ensure_ascii=False))
        raw = {"response": json.dumps({"behavior": "neutral", "decisions": [{"id": records[0]["id"], "mode": "say"}]})}
        plan = parse_expression(raw, records, audience_scope="private:"+OWNER, speaker_id=OWNER, is_owner=True)
        self.assertEqual(plan["memories"], [])

    async def test_evidence_changed_during_model_does_not_commit_old_conclusion(self):
        row = await self.gathered()
        ref = row["evidence_refs"][0]
        def changed():
            self.bot._tool_store.con.execute("UPDATE tool_observations SET text=? WHERE id=?", ("本资料已更正。", ref["id"]))
            self.bot._tool_store.con.commit()
        self.model(after_first=changed)
        await self.tick_later()
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 1)
        self.assertEqual(self.runtime.records(), [])
        self.runtime.prepare()
        self.assertEqual(self.runtime.store.get(OWNER, row["id"])["status"], "paused")

    async def test_background_project_gets_research_budget_before_other_open_questions(self):
        selected = self.three_open_questions()
        claimed_questions = {row["seed"]["question_key"] for row in selected}
        await self.bot._advance_background()
        rows = self.runtime.store.items(OWNER)
        progressed = [row for row in rows if row["stage"] == "review"]
        self.assertEqual(len(progressed), 1)
        attempts = self.bot._tool_store._read("SELECT question_key FROM tool_research WHERE question_key LIKE 'q:%' AND attempts>0")
        self.assertEqual([row["question_key"] for row in attempts], [progressed[0]["seed"]["question_key"]])
        self.assertIn(attempts[0]["question_key"], claimed_questions)
        self.assertEqual(self.runtime.store.state(OWNER)["daily_actions"], 1)
        self.assertEqual([name for name, _ in self.bot._mcp_clients["pixiv"].calls], ["search_library", "get_library_work"])

    async def test_background_owner_media_blocks_project_and_ordinary_question_budgets(self):
        self.three_open_questions()
        event = message(text="", mid="background-owner-media")
        event["raw_message"] = [{"type": "image"}]
        await self.bot.capture_unified(event)
        self.assertTrue(self.runtime.engaged())
        await self.bot._advance_background()
        self.assertEqual(self.runtime.store.state(OWNER)["daily_actions"], 0)
        self.assertEqual(self.bot._mcp_clients["pixiv"].calls, [])
        self.assertEqual(self.bot._tool_store._read("SELECT question_key FROM tool_research WHERE question_key LIKE 'q:%' AND attempts>0"), [])

    async def test_background_idle_project_allows_unclaimed_question_without_duplicate_project_read(self):
        selected = self.three_open_questions()
        claimed_questions = {row["seed"]["question_key"] for row in selected}
        await self.bot._advance_background()
        now = self.runtime.store.state(OWNER)["last_action"]+self.bot.config.mcp.research_interval_seconds+2
        with patch("time.time", return_value=now):
            await self.bot._advance_background()
        attempts = self.bot._tool_store._read("SELECT question_key FROM tool_research WHERE question_key LIKE 'q:%' AND attempts>0")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(sum(row["question_key"] not in claimed_questions for row in attempts), 1)
        self.assertEqual(self.runtime.store.state(OWNER)["daily_actions"], 1)
        self.assertEqual([name for name, _ in self.bot._mcp_clients["pixiv"].calls], ["search_library", "get_library_work"]*2)

    async def test_background_refreshes_reading_ownership_after_tick_selects_new_project(self):
        await self.gathered()
        self.model()
        await self.tick_later()
        self.three_open_questions(select=False)
        b = self.bot
        b.config.reading.enabled = b.config.reading.auto_continue = True
        b.config.reading.excerpt_chars = 100
        b.config.mcp.research_enabled = False
        b._mcp_clients["pixiv"] = NovelMCP()
        self.capture()
        opened = await b._ensure_companion().read("open", "pixiv", "novel", "123", "", {"session_id": "private-one"})
        self.assertTrue(opened["success"])
        self.assertEqual(self.runtime.active_reading_ids(), set())
        b._mcp_clients["pixiv"].calls.clear()
        b.ctx.llm.generate = AsyncMock(return_value={"response": "{}"})
        # The previous finished project's owner budget is still cooling down.
        now = self.runtime.store.state(OWNER)["last_action"]+1
        with patch("time.time", return_value=now):
            await b._advance_background()
        self.assertIn(opened["data"]["id"], self.runtime.active_reading_ids())
        self.assertEqual(b._companion.store.reading(opened["data"]["id"])["cursor"], 0)
        self.assertEqual(b._mcp_clients["pixiv"].calls, [])
