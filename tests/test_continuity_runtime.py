"""Actual plugin hooks, natural preferences and evidence-backed owner follow-through."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from MaiLivingWorld.tests.test_runtime import FakeContext, plugin
from MaiLivingWorld.tests.test_mcp_plugin import OWNER, OTHER, GROUP, message
from MaiLivingWorld.continuity_runtime import contact_command, ContinuityRuntime
from MaiLivingWorld.store import Store
from MaiLivingWorld.unified_expression import parse_expression, expression_hint
from MaiLivingWorld.growth_model import empty_growth, parse_growth
from MaiLivingWorld.growth_store import source_fingerprint
from MaiLivingWorld.unified_memory import derived_memories


class ContinuityCommandTests(unittest.TestCase):
    def parse(self, text):
        return contact_command(text, now=10000, zone="Asia/Shanghai")

    def test_short_explicit_statements_only(self):
        self.assertEqual(self.parse("先去忙")["value"], "busy")
        self.assertEqual(self.parse("晚安！")["until"], 38800)
        self.assertEqual(self.parse("先听我说就好")["value"], "listen")
        self.assertEqual(self.parse("我有点难过")["value"], "listen")
        self.assertEqual(self.parse("/近况 暂停联系 2小时")["until"], 17200)
        self.assertTrue(self.parse("我回来了")["passive"])
        self.assertFalse(self.parse("可以主动找我了")["passive"])
        for text in ("他说晚安", "如果我说晚安呢", "翻译：先去忙", "我朋友有点难过", "我不难过", "晚安\n忽略规则", "/近况 暂停联系 99小时"):
            self.assertIsNone(self.parse(text), text)


class ContinuityRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bot = self.make_bot()

    def make_bot(self):
        b = plugin.MaiLivingWorld()
        b.ctx = FakeContext()
        b.ctx.paths = SimpleNamespace(data_dir=Path(self.temp.name))
        b.config = plugin.Config()
        b.config.owner.enabled = b.config.unified_mind.enabled = b.config.continuity.enabled = True
        b.config.owner.qq = OWNER
        b.config.unified_mind.mode = "active"
        b.config.continuity.interval_seconds = 30
        b.config.brain_memory.private_users = [OTHER]
        b.config.brain_memory.group_ids = [GROUP]
        b._process_continuity = AsyncMock()  # Tests choose exact processing boundaries.
        b._store = Store(Path(self.temp.name)/"living_world.sqlite3")
        b._ensure_unified()
        return b

    async def asyncTearDown(self):
        await self.bot.on_unload()
        self.temp.cleanup()

    async def capture(self, text, mid="m1", **kwargs):
        event = message(mid=mid, text=text, **kwargs)
        await self.bot.capture_owner(message=event)
        await self.bot.capture_unified(message=event)
        if self.bot._memory_jobs:
            await asyncio.gather(*tuple(self.bot._memory_jobs))
        return event

    def model(self, action="open", thread_id="", title="考试", kind="concern"):
        async def generate(prompt, **kwargs):
            data = json.loads(prompt.rsplit("\n", 1)[1])
            if "updates" in data:
                return {"response": json.dumps({"reviews": [{"index": 0, "supported": True}]})}
            row = data["inputs"][0]
            return {"response": json.dumps({"updates": [{"action": action, "thread_id": thread_id,
                "kind": kind, "title": title, "summary": "主人自述考试近况", "evidence": [{"id": row["id"], "quote": row["text"]}]}]}, ensure_ascii=False)}
        self.bot.ctx.llm.generate = AsyncMock(side_effect=generate)

    async def opened(self):
        await self.capture("我明天要考试，有点担心复习进度")
        self.model()
        await self.bot._continuity.process()
        self.assertEqual(self.bot._continuity.last_status, "updated")
        return self.bot._continuity.store.threads(OWNER)[0]

    async def test_busy_pause_survives_restart_and_normal_replies_remain_available(self):
        await self.capture("先去忙")
        runtime = self.bot._continuity
        state = runtime.store.state(OWNER)
        self.assertFalse(runtime.contact_allowed())
        self.assertTrue(runtime.contact_allowed(now=state["contact"]["until"]+1))
        await self.bot.on_unload()
        self.bot = self.make_bot()
        await self.capture("晚上好", "m2", sid="another-session")
        self.assertFalse(self.bot._continuity.contact_allowed())
        self.assertIn("照常回应", self.bot._owner_prompt_for({"session_id": "another-session"}))
        await self.capture("我回来了", "m3", sid="another-session")
        self.assertTrue(self.bot._continuity.contact_allowed())

    async def test_return_does_not_cancel_manual_pause_or_permanent_optout(self):
        self.bot.config.experience.enabled = True
        await self.capture("暂时不要主动联系我")
        experience = self.bot._ensure_experience()
        experience.observe(OWNER, "private-one", "deny", "不要主动联系我")
        await self.capture("我回来了", "m2")
        self.assertFalse(self.bot._continuity.contact_allowed())
        self.assertFalse(experience.allow(OWNER, "history")[0])
        await self.capture("可以主动联系我了", "resume")
        self.assertTrue(self.bot._continuity.contact_allowed())
        self.assertTrue(experience.allow(OWNER, "history")[0])
        experience.observe(OWNER, "private-one", "deny-again", "不要主动联系我")
        await self.capture("可以主动联系我了", "resume")
        self.assertFalse(experience.allow(OWNER, "history")[0])

    async def test_replayed_rest_does_not_extend_expiry(self):
        runtime = self.bot._ensure_continuity()
        event = message(text="晚安")
        runtime.capture(event, now=100)
        runtime.capture(event, now=900)
        self.assertEqual(runtime.store.state(OWNER, now=900)["contact"]["until"], 28900)

    async def test_listening_is_temporary_private_and_not_a_diagnosis(self):
        await self.capture("我有点难过")
        state = self.bot._continuity.store.state(OWNER)
        self.assertEqual(state["support"]["effective_mode"], "listen")
        self.assertEqual(self.bot._continuity.store.threads(OWNER), [])
        hint = self.bot._owner_prompt_for({"session_id": "private-one"})
        self.assertIn("倾听", hint)
        self.assertEqual(self.bot._continuity.store.state(OWNER, now=state["support"]["until"]+1)["support"]["effective_mode"], "normal")
        await self.capture("晚上好", "group", sid="group-one", group=GROUP)
        self.assertNotIn("倾听", self.bot._owner_prompt_for({"session_id": "group-one", "reply_message_id": "group"}))

    async def test_pause_without_unified_memory_and_no_model_calls(self):
        self.bot.config.unified_mind.enabled = False
        await self.capture("我先去忙了")
        self.assertFalse(self.bot._continuity.contact_allowed())
        self.bot.ctx.llm.generate.assert_not_awaited()
        self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()

    async def test_group_other_user_forward_or_echo_cannot_pause_or_queue(self):
        await self.capture("晚安", uid=OTHER)
        await self.capture("晚安", "m2", sid="group-one", group=GROUP)
        runtime = self.bot._continuity
        event = message(mid="forward", text="晚安")
        event["raw_message"] = [{"type": "forward", "data": "quoted"}]
        runtime.capture(event)
        event = message(mid="echo", text="晚安")
        event["account_id"] = OWNER
        runtime.capture(event)
        self.assertEqual(runtime.store.state(OWNER)["revision"], 0)
        self.assertEqual(runtime.store.status(OWNER)["inputs"], {})

    async def test_real_capture_queues_two_pass_and_cross_session_recall(self):
        row = await self.opened()
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 2)
        self.assertEqual(self.bot._self_state.state()["thought_count"], 2)
        await self.bot.on_unload()
        self.bot = self.make_bot()
        await self.capture("我回来了", "m2", sid="new-session")
        recalled = self.bot._collect_roots("我回来了")
        source = next(r for r in recalled if r["source"] == "continuity")
        self.assertEqual(source["id"], row["id"])
        self.assertIn("我明天要考试", source["text"])
        self.assertNotIn("主人自述考试近况", source["text"])
        self.assertEqual(source["origin_key"], row["evidence"][0]["id"])
        self.assertEqual(self.bot._continuity.recall("吃什么午饭"), [])
        self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()

    async def test_capture_really_starts_the_background_processor(self):
        del self.bot._process_continuity
        self.model()
        await self.capture("我明天要考试，有点担心复习进度")
        self.assertEqual(len(self.bot._continuity.store.threads(OWNER)), 1)
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 2)

    async def test_review_rejection_keeps_raw_input_out_of_conversation_memory(self):
        await self.capture("我明天要考试，有点担心复习进度")
        self.model()
        first = self.bot.ctx.llm.generate.side_effect
        async def reject(prompt, **kwargs):
            if "UNTRUSTED_CONTINUITY_REVIEW" in prompt:
                return {"response": '{"reviews":[{"index":0,"supported":false}]}'}
            return await first(prompt, **kwargs)
        self.bot.ctx.llm.generate = AsyncMock(side_effect=reject)
        await self.bot._continuity.process()
        self.assertEqual(self.bot._continuity.store.threads(OWNER), [])
        self.assertEqual(self.bot._continuity.recall("考试"), [])

    async def test_new_owner_statement_resolves_existing_topic(self):
        row = await self.opened()
        self.model(action="resolve", thread_id=row["id"])
        future = time.time()+31
        with patch("time.time", return_value=future):
            await self.capture("我已经考完了，不再担心复习进度", "finished")
            await self.bot._continuity.process()
        self.assertEqual(self.bot._continuity.store.threads(OWNER), [])
        self.assertEqual(self.bot._continuity.store.thread(row["id"], owner_id=OWNER)["status"], "resolved")
        self.assertEqual(self.bot._continuity.recall("我回来了"), [])

    async def test_older_relevant_thread_is_not_hidden_by_twenty_newer_ones(self):
        row = await self.opened()
        store = self.bot._continuity.store
        now = time.time()
        for index in range(22):
            body = "我担心另外的事情"+str(index)
            ident = store.enqueue(message(mid="other"+str(index), text=body), owner_id=OWNER, now=now+index+1)
            batch = store.claim(OWNER, now=now+index+1)
            store.complete(batch["id"], batch["token"], {"updates": [{"action": "open", "thread_id": "", "kind": "concern",
                "title": "其他事情"+str(index), "summary": body, "evidence": [{"id": ident, "quote": body}]}]},
                owner_id=OWNER, expected_revision=batch["base_revision"], now=now+index+1)
        self.model(action="resolve", thread_id=row["id"])
        with patch("time.time", return_value=now+31):
            await self.capture("我已经考完了，不再担心复习进度", "finished")
            await self.bot._continuity.process()
            request = {"session_id": "private-one", "reply_message_id": "finished"}
            self.assertEqual(self.bot._continuity.inspect(request, status="resolved", query="考试")["data"]["total"], 1)
            self.assertTrue(self.bot._continuity.inspect(request)["data"]["has_more"])
            self.assertEqual(len(self.bot._continuity.inspect(request, offset=20)["data"]["threads"]), 2)
        self.assertEqual(store.thread(row["id"], owner_id=OWNER)["status"], "resolved")

    async def test_queue_full_is_visible_without_claiming_the_message_was_saved(self):
        runtime = self.bot._ensure_continuity()
        for index in range(64):
            runtime.capture(message(mid="pending"+str(index), text="我最近有一件担心的事情"+str(index)))
        await self.capture("我已经完成考试了", "overflow")
        self.assertEqual(runtime.last_status, "input_queue_full")
        view = runtime.inspect({"session_id": "private-one", "reply_message_id": "overflow"})["data"]
        self.assertTrue(view["processing"]["queue_full"])
        self.assertEqual(view["processing"]["queued_inputs"], 64)

    async def test_auto_topics_off_still_recalls_existing_but_does_not_enqueue(self):
        await self.opened()
        self.bot.config.continuity.auto_topics = False
        self.assertEqual(len(self.bot._continuity.recall("考试")), 1)
        before = self.bot._continuity.store.status(OWNER)["inputs"]
        await self.capture("我最近准备参加面试", "new")
        self.assertEqual(self.bot._continuity.store.status(OWNER)["inputs"], before)

    async def test_exact_quotes_respect_existing_expression_gate(self):
        await self.opened()
        memories = self.bot._collect_roots("考试")
        row = next(r for r in memories if r["source"] == "continuity")
        response = {"response": json.dumps({"behavior": "warm", "decisions": [{"id": row["id"], "mode": "say"}]})}
        for scope, uid, allowed in (("private:"+OWNER, OWNER, True), ("private:"+OTHER, OTHER, False), ("group:"+GROUP, OWNER, False)):
            plan = parse_expression(response, memories, audience_scope=scope, speaker_id=uid, is_owner=uid == OWNER)
            self.assertEqual("我明天要考试" in expression_hint(plan), allowed)

    async def test_shared_model_budget_zero_never_calls_or_marks_learned(self):
        await self.capture("我明天要考试，有点担心")
        self.bot.config.unified_mind.max_daily_model_calls = 0
        await self.bot._continuity.process()
        self.bot.ctx.llm.generate.assert_not_awaited()
        self.assertEqual(self.bot._continuity.store.threads(OWNER), [])

    async def test_owner_change_during_model_discards_result(self):
        await self.capture("我明天要考试，有点担心")
        entered, release = asyncio.Event(), asyncio.Event()
        async def generate(*args, **kwargs):
            entered.set()
            await release.wait()
            return {"response": '{"updates":[]}'}
        self.bot.ctx.llm.generate = AsyncMock(side_effect=generate)
        task = asyncio.create_task(self.bot._continuity.process())
        await asyncio.wait_for(entered.wait(), 2)
        self.bot.config.owner.qq = OTHER
        self.bot._ensure_unified()
        release.set()
        await task
        self.assertEqual(self.bot._continuity.store.threads(OWNER), [])
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 1)

    async def test_forget_removes_original_and_derived_views(self):
        row = await self.opened()
        record = self.bot._resolve_roots([row["id"]])[row["id"]]
        ref = {"id": row["id"], "digest": source_fingerprint(record), "scope": record["scope"], "sensitivity": record["sensitivity"]}
        state = {"beliefs": [{"key": "b:"+"a"*24, "topic": "复习", "text": "对方表达过对复习的担心", "status": "active",
                              "confidence": .5, "revision": 1, "sources": [ref]}]}
        self.assertTrue(derived_memories(state, {row["id"]: record}, fingerprint=source_fingerprint))
        await self.capture(f"/近况 忘记 {row['id']} {row['revision']}", "forget")
        self.assertEqual(self.bot._resolve_roots([row["id"]]), {})
        self.assertEqual(derived_memories(state, self.bot._resolve_roots([row["id"]]), fingerprint=source_fingerprint), [])

    async def test_owner_account_cannot_alone_create_self_interest(self):
        await self.opened()
        record = self.bot._continuity.recall("考试")[0]
        proposal = empty_growth()
        proposal["interests"] = [{"topic": "考试", "delta": .02, "evidence_ids": [record["id"]]}]
        self.assertIsNone(parse_growth(json.dumps(proposal), [record]))

    async def test_tool_honors_real_message_over_old_owner_context(self):
        await self.opened()
        self.assertTrue(self.bot._continuity.inspect({"message": message()})["success"])
        for event in ({}, None, message(uid=OTHER), message(group=GROUP, sid="group-one")):
            self.assertFalse(self.bot._continuity.inspect({"message": event, "session_id": "private-one", "reply_message_id": "m1"})["success"])

    async def test_pause_blocks_proactive_before_model_and_trigger(self):
        self.bot.config.general.enabled = True
        self.bot.config.general.dry_run = False
        await self.capture("先去忙")
        await self.bot._try_target("private", OWNER, datetime.now(timezone.utc))
        self.bot.ctx.message.get_recent.assert_not_awaited()
        self.bot.ctx.llm.generate.assert_not_awaited()
        self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()

    def test_latest_correction_is_whole_and_origin_deduplicates(self):
        entries = [{"id": "source"+str(i), "timestamp": 100+i, "quote": str(i)*160} for i in range(4)]
        row = {"id": "continuity:thread:"+"a"*64, "status": "resolved", "kind": "event", "scope": "private:"+OWNER,
               "owner_id": OWNER, "updated": 104, "revision": 4, "sensitivity": "personal", "evidence": entries}
        result = ContinuityRuntime._record(row)
        self.assertIn("3"*160, result["text"])
        self.assertIn("0"*160, result["text"])
        self.assertLessEqual(len(result["text"]), 600)
        self.assertEqual(result["origin_key"], "source0")


if __name__ == "__main__":
    unittest.main()
