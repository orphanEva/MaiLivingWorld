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
from MaiLivingWorld.tests.test_growth_store import fill_interest_sources
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

    def grow(self, sources, proposal, **options):
        store = self.bot._growth
        ident = store.enqueue(sources)
        lease = store.claim()
        self.assertEqual(lease["id"], ident)
        return store.complete(ident, lease["token"], proposal, sources,
                              expected_revision=store.state()["revision"], **options)

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

    async def test_adaptive_plan_resumes_with_new_evidence_and_stops_on_retraction(self):
        self.bot.config.projects.adaptive_plans = True
        row = await self.gathered()
        first = self.runtime.store.get(OWNER, row["id"])["evidence_refs"][0]

        async def generate(prompt, **options):
            if "UNTRUSTED_PROJECT_REVIEW" in prompt:
                return {"response": '{"supported":true}'}
            raw = prompt.split("UNTRUSTED_PROJECT_EVIDENCE\n", 1)[1].split("\n", 1)[0]
            record = json.loads(raw)["records"][0]
            return {"response": json.dumps({"outcome": "insufficient", "conclusion": "现有资料只说明旅途主题。",
                "quotes": [{"source_id": record["id"], "quote": record["text"][:80]}],
                "next_step": {"question": "森林旅人的对话如何描写旅途？", "evidence_needed": "需要实际对话片段"}}, ensure_ascii=False)}

        self.bot.ctx.llm.generate.side_effect = generate
        self.assertTrue((await self.tick_later())["success"])
        planned = self.runtime.store.get(OWNER, row["id"])
        self.assertEqual((planned["status"], planned["stage"]), ("active", "gather"))
        self.assertEqual(len(planned["plan"]["rounds"]), 1)
        progress = self.runtime.records("森林旅人")
        self.assertEqual(len(progress), 1)
        self.assertIn("需要实际对话片段", progress[0]["text"])
        self.assertEqual(progress[0]["sensitivity"], "personal")
        await self.bot.on_unload()
        self.bot, self.runtime = self.make_bot()
        self.bot.config.projects.adaptive_plans = True
        self.assertEqual(self.runtime.store.get(OWNER, row["id"])["plan"], planned["plan"])
        followup = self.bot._ensure_tool_store().put_observation("pixiv", "novel:followup",
            "森林旅人中的对话说明旅途中人物不断交流。", "private:"+OWNER, subject_id=OWNER)
        self.bot._research_tools = AsyncMock(return_value={"success": True, "records": [followup]})
        self.assertTrue((await self.tick_later())["success"])
        reviewed = self.runtime.store.get(OWNER, row["id"])
        self.assertEqual({ref["id"] for ref in reviewed["evidence_refs"]}, {first["id"], followup["id"]})
        self.assertEqual(reviewed["plan"]["latest_refs"][0]["id"], followup["id"])
        with patch.object(self.bot, "_resolve_roots", return_value={}):
            self.assertIsNone(await self.tick_later())
        self.assertEqual(self.runtime.store.get(OWNER, row["id"])["status"], "paused")

    async def test_legacy_twenty_sources_review_then_fill_remaining_capacity(self):
        from MaiLivingWorld.projects_runtime import _ref
        from MaiLivingWorld.mcp_runtime import day_start
        self.bot.config.projects.adaptive_plans = True
        row = self.runtime.prepare()
        originals = [self.bot._tool_store.put_observation("pixiv", "legacy-project:"+str(index),
            "森林旅人的旧资料描述了旅途线索第"+str(index)+"项。", "private:"+OWNER, subject_id=OWNER)
            for index in range(20)]
        claimed = self.runtime.store.claim(OWNER, [row["id"]], day_start=day_start(self.bot.config.general.timezone),
                                          interval_seconds=300)
        self.assertTrue(self.runtime.store.finish(OWNER, row["id"], claimed["lease_token"], claimed["revision"],
                                                 action="gathered", evidence_refs=[_ref(record) for record in originals]))
        seen = []
        async def generate(prompt, **options):
            if "UNTRUSTED_PROJECT_REVIEW" in prompt:
                return {"response": '{"supported":true}'}
            records = json.loads(prompt.split("UNTRUSTED_PROJECT_EVIDENCE\n", 1)[1].split("\n", 1)[0])["records"]
            seen.append(records)
            record = records[0]
            return {"response": json.dumps({"outcome": "insufficient", "conclusion": "这些资料仍只提供有限的旅途线索。",
                "quotes": [{"source_id": record["id"], "quote": record["text"]}],
                "next_step": {"question": "还需哪些旅途正文线索？", "evidence_needed": "进一步的正文材料"}}, ensure_ascii=False)}
        self.bot.ctx.llm.generate.side_effect = generate
        self.assertTrue((await self.tick_later())["success"])
        self.assertEqual(len(seen[0]), 8)
        self.assertEqual(len(self.runtime.store.get(OWNER, row["id"])["evidence_refs"]), 20)
        new_records = [self.bot._tool_store.put_observation("pixiv", "followup-project:"+str(index),
            "森林旅人的新材料补充了旅途对话第"+str(index)+"项。", "private:"+OWNER, subject_id=OWNER)
            for index in range(8)]
        self.bot._research_tools = AsyncMock(return_value={"success": True, "records": new_records})
        self.assertTrue((await self.tick_later())["success"])
        full = self.runtime.store.get(OWNER, row["id"])
        self.assertEqual(len(full["evidence_refs"]), 24)
        self.assertEqual(len(full["plan"]["latest_refs"]), 4)
        self.assertTrue((await self.tick_later())["success"])
        final = self.runtime.store.get(OWNER, row["id"])
        self.assertEqual((final["status"], final["reason"]), ("unresolved", "plan_needs_help"))
        self.assertTrue({record["id"] for record in new_records[:4]} <= {record["id"] for record in seen[-1]})
        self.assertEqual(len(final["plan"]["rounds"]), 2)

    async def test_exception_after_owner_interrupt_keeps_project_failure_count(self):
        row = self.runtime.prepare()
        async def interrupted_gather(*args, **kwargs):
            self.runtime.capture(message(mid="owner-interrupt", text="先陪我聊聊"))
            raise RuntimeError("simulated concurrent transport failure")
        self.runtime._gather = interrupted_gather
        result = await self.runtime.tick(project_id=row["id"])
        current = self.runtime.store.get(OWNER, row["id"])
        self.assertFalse(result["success"])
        self.assertEqual(current["reason"], "interrupted")
        self.assertEqual(current["failures"], 0)

    async def test_public_question_reaches_attention_and_projects_without_pixiv(self):
        b = self.bot
        b.config.mcp_pixiv.enabled = False
        b.config.mcp.public_sources_enabled = b.config.attention.enabled = True
        source = b._tool_store.put_observation("news", "telescope:original", "报道节选：望远镜记录了观测资料。",
            "public:news", sensitivity="public", url="https://www.ithome.com/0/875/123.htm")
        proposal = empty_growth()
        proposal["interests"] = [{"topic": "望远镜", "delta": .08, "evidence_ids": [source["id"]]}]
        proposal["questions"] = [{"topic": "望远镜", "question": "望远镜提供了哪些观测资料？",
                                  "status": "open", "evidence_ids": [source["id"]]}]
        b._growth.enqueue([source])
        job = b._growth.claim()
        b._growth.complete(job["id"], job["token"], proposal, [source], expected_revision=b._growth.state()["revision"])

        seeds = self.runtime.seed_candidates()
        self.assertEqual([seed["topic"] for seed in seeds], ["望远镜"])
        self.assertEqual(seeds[0]["source_signature"], b._research_source_signature())
        attention = b._ensure_attention()
        candidates = attention.candidates()
        self.assertEqual([(row["kind"], row["key"]) for row in candidates], [("research", seeds[0]["question_key"])])
        self.assertIsNotNone(attention._activity_record(candidates[0]))

        row = self.runtime.prepare()
        self.assertEqual(self.runtime.active_question_keys(), {seeds[0]["question_key"]})
        self.assertEqual([item["kind"] for item in attention.candidates()], ["project"])
        evidence = b._tool_store.put_observation("news", "telescope:followup", "报道节选：望远镜获得了补充观测数据。",
            "public:news", sensitivity="public", url="https://www.ithome.com/0/875/124.htm")
        b._research_tools = AsyncMock(return_value={"success": True, "records": [evidence]})
        self.assertTrue((await self.runtime.tick())["success"])
        self.assertEqual(self.runtime.store.get(OWNER, row["id"])["stage"], "review")
        b._research_tools.assert_awaited_once()
        self.assertEqual(b._research_tools.call_args.kwargs["question_key"], seeds[0]["question_key"])
        b._tool_store.retract_observation("news", "telescope:original")
        self.assertIsNone(attention._activity_record(candidates[0]))
        self.assertEqual(self.runtime.candidates(), [])

    async def test_gather_continues_when_the_same_interest_gains_evidence(self):
        row = self.runtime.prepare()
        original_seed = row["seed"]
        more = self.bot._tool_store.put_observation("cap", "album:more-interest",
            "森林旅人资料进一步描述了旅途中的风景。", "private:"+OWNER, subject_id=OWNER)
        self.grow([more], {"interests": [{"topic": "森林旅人", "delta": .02,
                                        "evidence_ids": [more["id"]]}]})
        self.runtime.prepare()
        current = self.runtime.store.get(OWNER, row["id"])
        self.assertEqual((current["status"], current["revision"], current["seed"]),
                         ("active", row["revision"], original_seed))
        await self.bot.on_unload()
        self.bot, self.runtime = self.make_bot()
        self.assertEqual(self.runtime.candidates()[0]["key"], row["id"])
        self.assertTrue((await self.runtime.tick())["success"])
        self.assertEqual(self.runtime.store.get(OWNER, row["id"])["stage"], "review")

    async def test_gather_accepts_question_evidence_but_stops_when_the_question_changes(self):
        row = self.runtime.prepare()
        more = self.bot._tool_store.put_observation("cap", "album:more-question",
            "森林旅人提供了一份补充旅途线索。", "private:"+OWNER, subject_id=OWNER)
        self.grow([more], {"questions": [{"topic": "森林旅人", "question": row["seed"]["question"],
            "status": "open", "evidence_ids": [more["id"]]}]})
        self.runtime.prepare()
        self.assertEqual(len(self.runtime.candidates()), 1)
        changed = self.bot._tool_store.put_observation("cap", "album:changed-question",
            "森林旅人的新资料转而讨论作者。", "private:"+OWNER, subject_id=OWNER)
        self.grow([changed], {"questions": [{"topic": "森林旅人", "question": "森林旅人的作者有何创作经历？",
            "status": "open", "evidence_ids": [changed["id"]]}]})
        self.runtime.prepare()
        self.assertEqual(self.runtime.candidates(), [])
        self.assertEqual(self.runtime.store.get(OWNER, row["id"])["reason"], "source_changed")

    async def test_new_interest_basis_cannot_continue_a_project_via_question_sources(self):
        fresh = self.bot._tool_store.put_observation("cap", "album:renewal-basis",
            "森林旅人还有一份可供重新了解的独立材料。", "private:"+OWNER, subject_id=OWNER)
        self.grow([fresh], {"questions": [{"topic": "森林旅人", "question": "森林旅人如何描写旅途？",
            "status": "open", "evidence_ids": [fresh["id"]]}]})
        row = self.runtime.prepare()
        interest = self.bot._growth.state()["interests"][0]
        self.bot._growth.retire(interest["key"], expected_revision=self.bot._growth.state()["revision"])
        extra = self.bot._tool_store.put_observation("cap", "album:renewal-extra",
            "同批还有一份别的作品资料。", "private:"+OWNER, subject_id=OWNER)
        self.grow([fresh, extra], {"interests": [{"topic": "森林旅人", "delta": .03, "evidence_ids": [fresh["id"]]}]})
        self.assertTrue(all(ref in self.bot._growth_snapshot()["questions"][0]["sources"]
                            for ref in row["seed"]["seed_refs"]))
        self.runtime.prepare()
        self.assertEqual(self.runtime.candidates(), [])
        self.assertEqual(self.runtime.store.get(OWNER, row["id"])["reason"], "source_changed")

    async def test_large_interest_project_preserves_all_private_sources_and_small_review(self):
        sources = [self.bot._tool_store.put_observation("cap", "album:long-"+str(index),
            "森林旅人第"+str(index)+"份材料描述了旅途景象。",
            "private:"+(OTHER if index == 158 else OWNER),
            subject_id=OTHER if index == 158 else OWNER) for index in range(159)]
        fill_interest_sources(self.bot._growth, sources, "森林旅人")
        state = self.bot._growth.state()
        self.bot._growth.retire(state["questions"][0]["key"], expected_revision=state["revision"])
        separate = self.bot._tool_store.put_observation("cap", "album:separate-question",
            "另一份材料提出森林旅人的旅途描写问题。", "private:"+OWNER, subject_id=OWNER)
        self.grow([separate], {"questions": [{"topic": "森林旅人", "question": "森林旅人如何描写旅途？",
            "status": "open", "evidence_ids": [separate["id"]]}]})
        row = self.runtime.prepare()
        self.assertEqual(len(row["seed"]["interest_refs"]), 160)
        self.assertEqual(len(row["seed"]["seed_refs"]), 161)
        await self.bot.on_unload()
        self.bot, self.runtime = self.make_bot()
        with patch.object(self.bot, "_resolve_roots", wraps=self.bot._resolve_roots) as resolve:
            self.assertTrue((await self.runtime.tick())["success"])
        self.assertTrue(all(len(call.args[0]) <= 128 for call in resolve.call_args_list))
        self.model()
        self.assertTrue((await self.tick_later())["success"])
        for call in self.bot.ctx.llm.generate.await_args_list:
            self.assertNotIn(sources[-1]["text"], call.args[0])
            self.assertLess(len(call.args[0]), 12000)
        result = self.runtime.records()[0]
        self.assertEqual(len(result["sources"]), 162)
        self.assertEqual(result["sensitivity"], "personal")
        self.assertEqual(result["subject_id"], "")
        self.bot._tool_store.retract_observation("cap", "album:long-158")
        self.assertEqual(self.runtime.records(), [])

    async def test_owner_paused_and_stopped_projects_keep_their_status_after_interest_growth(self):
        row = self.runtime.prepare()
        self.runtime.store.command(OWNER, row["id"], row["revision"], "pause", "a"*64, "b"*64)
        more = self.bot._tool_store.put_observation("cap", "album:owner-paused",
            "森林旅人的资料增补了旅途内容。", "private:"+OWNER, subject_id=OWNER)
        self.grow([more], {"interests": [{"topic": "森林旅人", "delta": .02, "evidence_ids": [more["id"]]}]})
        self.runtime.prepare()
        paused = self.runtime.store.get(OWNER, row["id"])
        self.assertEqual((paused["status"], paused["reason"]), ("paused", "owner_pause"))
        self.runtime.store.command(OWNER, row["id"], paused["revision"], "stop", "c"*64, "d"*64)
        self.runtime.prepare()
        stopped = self.runtime.store.get(OWNER, row["id"])
        self.assertEqual((stopped["status"], stopped["reason"]), ("stopped", "owner_stop"))
        self.assertEqual(self.runtime.candidates(), [])

    async def test_negative_interest_pauses_and_later_recovery_waits_for_owner_resume(self):
        row = self.runtime.prepare()
        for index, delta in enumerate((-.1, .1)):
            source = self.bot._tool_store.put_observation("cap", "album:changed-interest-"+str(index),
                "森林旅人的另一份材料展示了新的旅途视角。", "private:"+OWNER, subject_id=OWNER)
            self.grow([source], {"interests": [{"topic": "森林旅人", "delta": delta,
                                              "evidence_ids": [source["id"]]}]})
            self.runtime.prepare()
            current = self.runtime.store.get(OWNER, row["id"])
            self.assertEqual((current["status"], current["reason"]), ("paused", "source_changed"))
        self.assertTrue(self.runtime.capture(message(
            text=f"/项目 继续 {row['id']} {current['revision']}", mid="resume-after-recovery")))
        self.assertEqual(self.runtime.store.get(OWNER, row["id"])["status"], "active")

    async def test_legacy_separate_sources_need_an_exact_seed_to_preserve_the_old_interest_basis(self):
        state = self.bot._growth.state()
        self.bot._growth.retire(state["questions"][0]["key"], expected_revision=state["revision"])
        source = self.bot._tool_store.put_observation("cap", "album:legacy-separate",
            "森林旅人的独立问题资料。", "private:"+OWNER, subject_id=OWNER)
        self.grow([source], {"questions": [{"topic": "森林旅人", "question": "森林旅人如何描写旅途？",
            "status": "open", "evidence_ids": [source["id"]]}]})
        seed = self.runtime.seed_candidates()[0]
        seed.pop("interest_refs")
        row = self.runtime.store.create(OWNER, seed)
        self.assertTrue(self.runtime._current(row, active=True))
        more = self.bot._tool_store.put_observation("cap", "album:legacy-more",
            "森林旅人的新兴趣材料。", "private:"+OWNER, subject_id=OWNER)
        self.grow([more], {"interests": [{"topic": "森林旅人", "delta": .02, "evidence_ids": [more["id"]]}]})
        self.runtime.prepare()
        self.assertEqual(self.runtime.store.get(OWNER, row["id"])["status"], "paused")

    async def test_legacy_separate_basis_does_not_assume_continuity_for_a_refined_review(self):
        state = self.bot._growth.state()
        self.bot._growth.retire(state["questions"][0]["key"], expected_revision=state["revision"])
        source = self.bot._tool_store.put_observation("cap", "album:legacy-review-question",
            "森林旅人的独立问题资料。", "private:"+OWNER, subject_id=OWNER)
        self.grow([source], {"questions": [{"topic": "森林旅人", "question": "森林旅人如何描写旅途？",
            "status": "open", "evidence_ids": [source["id"]]}]})
        seed = self.runtime.seed_candidates()[0]
        seed.pop("interest_refs")
        row = self.runtime.store.create(OWNER, seed)
        self.assertTrue((await self.runtime.tick())["success"])
        self.assertEqual(self.runtime.store.get(OWNER, row["id"])["stage"], "review")
        more = self.bot._tool_store.put_observation("cap", "album:legacy-refined-review",
            "森林旅人的新资料把问题细化到旅途的色彩描写。", "private:"+OWNER, subject_id=OWNER)
        self.grow([more], {"questions": [{"topic": "森林旅人", "question": "森林旅人如何用色彩描写旅途？",
            "status": "open", "evidence_ids": [more["id"]]}]})
        self.runtime.prepare()
        self.assertEqual(self.runtime.store.get(OWNER, row["id"])["status"], "paused")

    async def test_gathered_project_reviews_original_question_after_growth_answers_it(self):
        row = await self.gathered()
        for status in ("open", "resolved"):
            source = self.bot._tool_store.put_observation("cap", "album:progress:"+status,
                "作品元信息补充了森林旅人的叙述视角。"+status, "private:"+OWNER, subject_id=OWNER)
            data = empty_growth()
            data["questions"] = [{"topic": row["seed"]["topic"], "question": "现有资料只支持有限的叙述视角判断。",
                                  "status": status, "evidence_ids": [source["id"]]}]
            self.bot._growth.enqueue([source])
            job = self.bot._growth.claim()
            self.bot._growth.complete(job["id"], job["token"], data, [source],
                                      expected_revision=self.bot._growth.state()["revision"])
            self.runtime.prepare()
            self.assertEqual(self.runtime.store.get(OWNER, row["id"])["status"], "active")
            self.assertEqual(len(self.runtime.candidates()), 1)
        await self.bot.on_unload()
        self.bot, self.runtime = self.make_bot()
        self.model()
        self.assertTrue((await self.tick_later())["success"])
        self.assertEqual(self.runtime.store.get(OWNER, row["id"])["status"], "unresolved")
        prompt = self.bot.ctx.llm.generate.await_args_list[0].args[0]
        self.assertIn(row["seed"]["question"], prompt)

    async def test_gathered_project_still_stops_after_question_is_retired(self):
        row = await self.gathered()
        state = self.bot._growth.state()
        self.bot._growth.retire(row["seed"]["question_key"], expected_revision=state["revision"])
        self.runtime.prepare()
        self.assertEqual(self.runtime.candidates(), [])
        self.assertEqual(self.runtime.store.get(OWNER, row["id"])["status"], "paused")

    async def test_legacy_pixiv_project_continues_with_same_sources_after_restart(self):
        seed = self.runtime.seed_candidates()[0]
        seed.pop("interest_refs")
        seed["source_signature"] = self.bot._mcp_source_signature("pixiv")
        row = self.runtime.store.create(OWNER, seed)
        self.runtime.prepare()
        self.assertEqual(self.runtime.store.get(OWNER, row["id"])["status"], "active")
        source = self.bot._tool_store.put_observation("cap", "album:legacy-shared-more",
            "森林旅人的新增旅途资料。", "private:"+OWNER, subject_id=OWNER)
        self.grow([source], {"interests": [{"topic": "森林旅人", "delta": .02, "evidence_ids": [source["id"]]}]})
        self.runtime.prepare()
        self.assertEqual(self.runtime.store.get(OWNER, row["id"])["status"], "active")
        self.assertTrue((await self.runtime.tick())["success"])
        await self.bot.on_unload()
        self.bot, self.runtime = self.make_bot()
        self.assertEqual(len(self.runtime.candidates()), 1)
        persisted = self.runtime.store.get(OWNER, row["id"])
        self.assertEqual(persisted["stage"], "review")
        self.assertEqual(persisted["seed"]["source_signature"], seed["source_signature"])
        self.bot.config.mcp_pixiv.url = "http://127.0.0.1:9001/mcp"
        self.assertFalse(self.runtime._current(persisted, active=True))

    async def test_persisted_project_survives_runtime_generation_and_budget_changes(self):
        row = await self.gathered()
        signature = self.runtime.signature()
        self.bot._mcp_generation += 1
        self.bot.config.unified_mind.max_daily_model_calls += 1
        self.assertNotEqual(signature, self.runtime.signature())
        self.assertTrue(self.runtime._current(row, active=True))
        await self.bot.on_unload()
        self.bot, self.runtime = self.make_bot()
        self.runtime.prepare()
        self.assertEqual(self.runtime.store.get(OWNER, row["id"])["status"], "active")
        self.assertEqual(len(self.runtime.candidates()), 1)

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
