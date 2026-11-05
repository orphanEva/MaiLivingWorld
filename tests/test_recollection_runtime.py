"""Real plugin hooks and BrainBook sources, with offline host/model doubles."""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from MaiLivingWorld.tests.test_runtime import FakeContext, plugin
from MaiLivingWorld.tests.test_mcp_plugin import OWNER, OTHER, GROUP, message
from MaiLivingWorld.growth_store import source_fingerprint
from MaiLivingWorld.growth_model import empty_growth
from MaiLivingWorld.store import Store


CAT_CORRECTION = "你记错了，我不是讨厌猫，而是对猫毛过敏。"


class RecollectionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bot = self.make_bot()
        self.fact_id = self.fact()

    def make_bot(self):
        bot = plugin.MaiLivingWorld()
        bot.ctx = FakeContext()
        bot.ctx.paths = SimpleNamespace(data_dir=Path(self.temp.name))
        bot.config = plugin.Config()
        bot.config.owner.enabled = bot.config.unified_mind.enabled = bot.config.recollection.enabled = True
        bot.config.brain_memory.enabled = True
        bot.config.owner.qq = OWNER
        bot.config.unified_mind.mode = "active"
        bot.config.unified_mind.min_interval_seconds = 0
        bot.config.recollection.interval_seconds = 30
        bot.config.brain_memory.private_users = [OTHER]
        bot.config.brain_memory.group_ids = [GROUP]
        bot._process_recollection = AsyncMock()  # Each test chooses the await boundary.
        bot._store = Store(Path(self.temp.name)/"living_world.sqlite3")
        bot._ensure_unified()
        bot._ensure_recollection()
        return bot

    async def asyncTearDown(self):
        await self.bot.on_unload()
        self.temp.cleanup()

    def fact(self, ident="cat", *, value="主人讨厌猫。", attribute="关于猫", owner=OWNER, scope=None):
        db = self.bot._ensure_brain()
        now = time.time()-1000
        with db.con:
            db.con.execute("INSERT INTO brain_facts(id,scope,subject_id,attribute,value,confidence,status,seen_count,created,updated) "
                           "VALUES (?,?,?,?,?,1,'active',1,?,?)",
                           (ident, scope or "private:"+owner, owner, attribute, value, now, now))
        return "brain:fact:"+ident

    def alter_fact(self, *, value=None, status=None):
        db = self.bot._ensure_brain()
        with db.con:
            if value is not None:
                db.con.execute("UPDATE brain_facts SET value=?,updated=? WHERE id='cat'", (value, time.time()))
            if status is not None:
                db.con.execute("UPDATE brain_facts SET status=?,updated=? WHERE id='cat'", (status, time.time()))

    async def capture(self, text=CAT_CORRECTION, mid="m1", **changes):
        event = message(mid=mid, text=text, **changes)
        await self.bot.capture_owner(message=event)
        await self.bot.capture_unified(message=event)
        if self.bot._memory_jobs:
            await asyncio.gather(*tuple(self.bot._memory_jobs))
        return event

    def model(self, *, quote=None, old_quote=None, on_first=None, supported=True):
        async def generate(prompt, **kwargs):
            data = json.loads(prompt.rsplit("\n", 1)[1])
            if "updates" in data:
                return {"response": json.dumps({"reviews": [{"index": 0, "supported": supported}]})}
            raw = next(row for row in data["targets"] if row["id"] == self.fact_id)
            value = {"target_id": raw["id"], "target_digest": raw["target_digest"],
                     "old_quote": raw["text"] if old_quote is None else old_quote,
                     "quote": data["input"]["text"] if quote is None else quote, "relation": "clarify"}
            if on_first:
                result = on_first()
                if asyncio.iscoroutine(result):
                    await result
            return {"response": json.dumps({"updates": [value]}, ensure_ascii=False)}
        self.bot.ctx.llm.generate = AsyncMock(side_effect=generate)

    async def corrected(self, text=CAT_CORRECTION, mid="m1", **model_options):
        await self.capture(text, mid)
        self.model(**model_options)
        runtime = self.bot._recollection
        # Later turns use the real persisted cooldown, not an implementation stub.
        now = max(time.time(), runtime.store.state(OWNER)["last_cycle"]+31)
        with patch("time.time", return_value=now):
            await runtime.process()
        self.assertEqual(runtime.last_status, "updated")
        return runtime.store.active(OWNER)[0]

    async def test_two_pass_cat_correction_survives_session_change_and_restart(self):
        raw = self.bot._resolve_roots_raw([self.fact_id])[self.fact_id]
        entry = await self.corrected()
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 2)
        record = self.bot._resolve_roots([self.fact_id])[self.fact_id]
        self.assertIn(CAT_CORRECTION, record["text"])
        self.assertEqual(record["evidence_status"], "owner_reported")
        self.assertEqual(self.bot._resolve_roots_raw([self.fact_id])[self.fact_id], raw)
        self.assertNotEqual(source_fingerprint(record), source_fingerprint(raw))
        await self.bot.on_unload()
        self.bot = self.make_bot()
        await self.capture("我又想到猫毛的事情", "new-session-message", sid="new-session")
        records = self.bot._collect_roots("猫毛过敏")
        restored = next(row for row in records if row["id"] == self.fact_id)
        self.assertEqual(restored["correction_id"], entry["id"])
        self.assertIn("不是讨厌猫", restored["text"])
        self.bot.ctx.llm.generate.assert_not_awaited()

    async def test_real_capture_hook_schedules_and_finishes_two_pass_correction(self):
        del self.bot._process_recollection
        self.model()
        await self.capture()
        self.assertEqual(self.bot._recollection.last_status, "updated")
        self.assertEqual(len(self.bot._recollection.store.active(OWNER)), 1)
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 2)

    async def test_second_correction_uses_original_digest_and_explicit_retract_restores_original(self):
        first = await self.corrected()
        second_text = "纠正一下，我现在不是对猫毛过敏，而是只对花粉过敏。"
        second = await self.corrected(second_text, "m2")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["target_digest"], second["target_digest"])
        self.assertIn("花粉", self.bot._resolve_roots([self.fact_id])[self.fact_id]["text"])
        await self.capture(f"/更正 撤回 {second['id']} {second['revision']}", "retract")
        self.assertEqual(self.bot._recollection.store.active(OWNER), [])
        self.assertNotIn("correction_id", self.bot._resolve_roots([self.fact_id])[self.fact_id])
        self.assertIn("主人讨厌猫", self.bot._resolve_roots([self.fact_id])[self.fact_id]["text"])

    async def test_correction_of_one_fact_preserves_other_original_facts(self):
        self.alter_fact(value="面试在周三。居住地是杭州。")
        # Ensure the source precedes this newly captured correction.
        await self.corrected("纠正一下，我的面试从周三改到周五了。", old_quote="面试在周三。")
        record = self.bot._resolve_roots([self.fact_id])[self.fact_id]
        self.assertIn("居住地是杭州", record["text"])
        self.assertIn("改到周五", record["text"])
        self.assertNotIn("面试在周三。", record["text"])

    async def test_independent_corrections_survive_restart_and_retract_separately(self):
        self.alter_fact(value="面试在周三。居住地是杭州。喜欢喝绿茶。")
        first = await self.corrected("纠正一下，我的面试改到周五了。", old_quote="面试在周三。")
        second = await self.corrected("纠正一下，我的居住地现在是苏州。", "m2", old_quote="居住地是杭州。")
        self.assertEqual(first["target_digest"], second["target_digest"])
        record = self.bot._resolve_roots([self.fact_id])[self.fact_id]
        self.assertIn("改到周五", record["text"])
        self.assertIn("现在是苏州", record["text"])
        self.assertIn("喜欢喝绿茶", record["text"])
        self.assertNotIn("面试在周三", record["text"])
        self.assertNotIn("居住地是杭州", record["text"])
        before = source_fingerprint(record)
        await self.bot.on_unload()
        self.bot = self.make_bot()
        self.assertEqual(source_fingerprint(self.bot._resolve_roots([self.fact_id])[self.fact_id]), before)
        await self.capture(f"/更正 撤回 {first['id']} {first['revision']}", "retract-first", sid="new-session")
        remaining = self.bot._resolve_roots([self.fact_id])[self.fact_id]
        self.assertIn("面试在周三", remaining["text"])
        self.assertNotIn("改到周五", remaining["text"])
        self.assertIn("现在是苏州", remaining["text"])
        self.assertNotIn("居住地是杭州", remaining["text"])
        self.assertIn("喜欢喝绿茶", remaining["text"])
        self.assertNotEqual(source_fingerprint(remaining), before)
        self.assertEqual(self.bot._recollection.store.active(OWNER), [second])
        await self.capture(f"/更正 撤回 {second['id']} {second['revision']}", "retract-second", sid="new-session")
        restored = self.bot._resolve_roots([self.fact_id])[self.fact_id]
        raw = self.bot._resolve_roots_raw([self.fact_id])[self.fact_id]
        self.assertEqual(restored, raw)

    async def test_raw_source_update_or_retirement_immediately_disables_old_overlay(self):
        await self.corrected()
        self.alter_fact(value="主人已经解释过猫毛的事情。")
        updated = self.bot._resolve_roots([self.fact_id])[self.fact_id]
        self.assertNotIn("correction_id", updated)
        self.assertIn("已经解释", updated["text"])
        self.alter_fact(status="rejected")
        self.assertNotIn(self.fact_id, self.bot._resolve_roots([self.fact_id]))

    async def test_real_growth_derived_memory_stops_using_old_source_after_correction(self):
        raw = self.bot._resolve_roots_raw([self.fact_id])[self.fact_id]
        self.bot.config.growth.enabled = True
        self.bot.config.growth.mode = "active"
        self.bot._ensure_growth()
        store, now = self.bot._growth, time.time()-10
        store.enqueue([raw], now=now)
        job = store.claim(now=now+1)
        value = empty_growth()
        value["beliefs"] = [{"topic": "猫", "statement": "主人曾表达讨厌猫", "confidence": .8, "evidence_ids": [raw["id"]]}]
        store.complete(job["id"], job["token"], value, [raw], expected_revision=0, now=now+2)
        self.assertTrue(self.bot._derived_memories("猫"))
        await self.corrected()
        self.assertEqual(self.bot._derived_memories("猫"), [])
        self.assertEqual(self.bot._growth_snapshot()["beliefs"], [])

    async def test_group_other_sender_forward_echo_and_disabled_capture_do_not_write(self):
        await self.capture(mid="other", uid=OTHER)
        await self.capture(mid="group", group=GROUP, sid="group-one")
        runtime = self.bot._recollection
        forward = message(mid="forward", text=CAT_CORRECTION)
        forward["raw_message"] = [{"type": "forward", "data": CAT_CORRECTION}]
        echo = message(mid="echo", text=CAT_CORRECTION)
        echo["account_id"] = OWNER
        for event in (forward, echo):
            with self.assertRaises(ValueError):
                runtime.capture(event)
        self.bot.config.recollection.enabled = False
        await self.capture(mid="disabled")
        self.assertEqual(runtime.store.status(OWNER)["inputs"], {})
        self.bot.ctx.llm.generate.assert_not_awaited()

    async def test_owner_and_configuration_changes_after_await_cancel_correction(self):
        await self.capture()
        def replace_owner():
            self.bot.config.owner.qq = OTHER
            self.bot._ensure_unified()
        self.model(on_first=replace_owner)
        await self.bot._recollection.process()
        self.assertEqual(self.bot._recollection.store.active(OWNER), [])
        self.assertEqual(self.bot._recollection.store.active(OTHER), [])
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 1)

    async def test_disable_after_await_does_not_commit_or_send(self):
        await self.capture()
        self.model(on_first=lambda: setattr(self.bot.config.recollection, "enabled", False))
        runtime = self.bot._recollection
        await runtime.process()
        self.assertEqual(runtime.store.active(OWNER), [])
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 1)
        self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()

    async def test_source_update_or_retraction_after_await_prevents_commit(self):
        await self.capture()
        self.model(on_first=lambda: self.alter_fact(status="rejected"))
        await self.bot._recollection.process()
        self.assertEqual(self.bot._recollection.store.active(OWNER), [])
        self.assertEqual(self.bot._recollection.last_status, "context_changed")
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 1)

    async def test_rejected_review_is_not_committed_and_duplicate_input_does_not_regrow(self):
        await self.capture()
        self.model(supported=False)
        await self.bot._recollection.process()
        self.assertEqual(self.bot._recollection.store.active(OWNER), [])
        self.assertEqual(self.bot._recollection.last_status, "no_supported_correction")
        calls = self.bot.ctx.llm.generate.await_count
        await self.capture()
        await self.bot._recollection.process()
        self.assertEqual(self.bot.ctx.llm.generate.await_count, calls)

    def outgoing(self, mid="out1", *, sid="private-one", text="你现在还讨厌猫吗？", **changes):
        event = message(mid=mid, uid="99999999", sid=sid, text=text)
        event.update(changes)
        return event

    async def test_only_verified_sent_owner_question_gets_receipt(self):
        event = self.outgoing()
        for sent in (False, None, 1):
            await self.bot.question_send_feedback(sent=sent, message=event)
        self.assertEqual(self.bot._recollection.store.recent_deliveries(OWNER), [])
        await self.bot.question_send_feedback(sent=True, message=message(mid="inbound", text="你现在还讨厌猫吗？"))
        await self.bot.question_send_feedback(sent=True, message=self.outgoing(text="今天聊得很开心"))
        self.assertEqual(self.bot._recollection.store.recent_deliveries(OWNER), [])
        await self.bot.question_send_feedback(sent=True, message=event)
        self.assertEqual(len(self.bot._recollection.store.recent_deliveries(OWNER)), 1)
        self.assertIn(self.fact_id, self.bot._recollection.asked())

    async def test_unknown_or_group_recipient_is_not_treated_as_owner_delivery(self):
        self.bot.ctx.chat.get_stream_by_user_id.return_value = None
        await self.bot.question_send_feedback(sent=True, message=self.outgoing())
        self.bot.ctx.chat.get_stream_by_user_id.return_value = {"stream_id": "private-one"}
        await self.bot.question_send_feedback(sent=True, message=self.outgoing(sid="another-private"))
        group = self.outgoing()
        group["message_info"]["group_info"] = {"group_id": GROUP}
        await self.bot.question_send_feedback(sent=True, message=group)
        self.assertEqual(self.bot._recollection.store.recent_deliveries(OWNER), [])

    async def test_quoted_negated_or_merely_reported_questions_are_not_delivery_evidence(self):
        examples = ["你说不要再问还讨厌猫吗，我记住了。",
                    "不要再问你还讨厌猫吗？", "“你还讨厌猫吗？”是你刚才引用的话。",
                    "比如可以问你还讨厌猫吗？", "我不知道你对猫的态度怎么样，先好好休息。"]
        for index, text in enumerate(examples):
            with self.subTest(text=text):
                await self.bot.question_send_feedback(sent=True, message=self.outgoing("quoted"+str(index), text=text))
                self.assertEqual(self.bot._recollection.store.recent_deliveries(OWNER), [])
        await self.bot.question_send_feedback(sent=True, message=self.outgoing("actual", text="你现在还讨厌猫吗？"))
        self.assertIn(self.fact_id, self.bot._recollection.asked())

    async def test_statement_about_one_topic_does_not_mark_it_asked_with_other_topic_question(self):
        meal = self.fact("meal", attribute="晚餐", value="主人晚餐打算吃面。")
        await self.bot.question_send_feedback(sent=True, message=self.outgoing(text="你讨厌猫的事我记住了。晚餐怎么样？"))
        asked = self.bot._recollection.asked()
        self.assertIn(meal, asked)
        self.assertNotIn(self.fact_id, asked)

    async def test_delivery_owner_change_during_recipient_resolution_is_rejected(self):
        async def change(*args, **kwargs):
            self.bot.config.owner.qq = OTHER
            self.bot._ensure_unified()
            return {"stream_id": "private-one"}
        self.bot.ctx.chat.get_stream_by_user_id.side_effect = change
        await self.bot.question_send_feedback(sent=True, message=self.outgoing())
        self.assertEqual(self.bot._recollection.store.recent_deliveries(OWNER), [])
        self.assertEqual(self.bot._recollection.store.recent_deliveries(OTHER), [])

    async def test_delivery_dedup_survives_restart_and_changed_source_is_not_still_asked(self):
        outgoing = self.outgoing()
        await self.bot.question_send_feedback(sent=True, message=outgoing)
        rows = self.bot._recollection.store.recent_deliveries(OWNER)
        self.assertEqual(len(rows), 1)
        await self.bot.on_unload()
        self.bot = self.make_bot()
        await self.bot.question_send_feedback(sent=True, message=outgoing)
        self.assertEqual(self.bot._recollection.store.recent_deliveries(OWNER), rows)
        self.assertIn(self.fact_id, self.bot._recollection.asked())
        self.alter_fact(value="主人现在解释不是讨厌猫，而是猫毛过敏。")
        self.assertNotIn(self.fact_id, self.bot._recollection.asked())

    async def test_inspection_is_owner_private_and_disable_restores_raw_retrieval(self):
        await self.corrected()
        kwargs = {"session_id": "private-one", "reply_message_id": "m1"}
        self.assertTrue(self.bot._recollection.inspect(kwargs)["success"])
        self.assertFalse(self.bot._recollection.inspect({**kwargs, "message": message(uid=OTHER)})["success"])
        self.assertFalse(self.bot._recollection.inspect({**kwargs, "message": message(group=GROUP, sid="group-one")})["success"])
        self.bot.config.recollection.enabled = False
        self.assertNotIn("correction_id", self.bot._resolve_roots([self.fact_id])[self.fact_id])
        self.assertFalse(self.bot._recollection.inspect(kwargs)["success"])


if __name__ == "__main__":
    unittest.main()
