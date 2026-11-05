"""Actual sent hook -> owner correction -> persisted repair -> relevant reply."""
import asyncio
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from MaiLivingWorld.tests.test_runtime import FakeContext, plugin
from MaiLivingWorld.tests.test_mcp_plugin import OWNER, OTHER, GROUP, message


SENT = "你周五要参加面试，今晚可以准备一下。"
OLD = "周五要参加面试"
CORRECTION = "你刚才说“周五要参加面试”，其实我面试是在周六。"


def response(value):
    return {"response": json.dumps(value, ensure_ascii=False)}


class RepairPluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = time.time()
        self.bot = self.make_bot()

    def make_bot(self):
        b = plugin.MaiLivingWorld()
        b.ctx = FakeContext()
        b.ctx.paths = SimpleNamespace(data_dir=Path(self.temp.name))
        b.config = plugin.Config()
        b.config.owner.enabled = b.config.unified_mind.enabled = b.config.repair.enabled = True
        b.config.owner.qq = OWNER
        b.config.unified_mind.mode = "active"
        b.config.unified_mind.min_interval_seconds = 0
        b.config.unified_mind.max_daily_model_calls = 100
        b.config.brain_memory.private_users = [OTHER]
        b.config.brain_memory.group_ids = [GROUP]
        b._ensure_repair()
        return b

    async def asyncTearDown(self):
        await self.bot.on_unload()
        self.temp.cleanup()

    async def capture(self, mid, text, extra=None, **kw):
        event = message(mid=mid, text=text, **kw)
        event.update(extra or {})
        await self.bot.capture_owner(message=event)
        await self.bot.capture_unified(message=event)
        return {"session_id": event["session_id"], "reply_message_id": mid}

    async def sent(self, text=SENT, mid="out", *, sent=True, **kw):
        with patch("time.time", return_value=self.now):
            return await self.bot.repair_send_feedback(sent=sent,
                message=message(mid=mid, uid="99999999", text=text, **kw))

    async def drain(self):
        if self.bot._memory_jobs:
            await asyncio.gather(*list(self.bot._memory_jobs))

    async def correct(self, text=CORRECTION, mid="correction", *, supported=True):
        self.bot.ctx.llm.generate.side_effect = [response({"old_quote": OLD, "correction_quote": text}),
                                                response({"supported": supported})]
        with patch("time.time", return_value=self.now):
            request = await self.capture(mid, text)
            await self.drain()
        return request

    async def reply(self, mid, text, **kw):
        with patch("time.time", return_value=self.now):
            request = await self.capture(mid, text, **kw)
            result = await self.bot.brain_reply_hint(**request,
                messages=[{"role": "system", "content": "宿主原始系统提示"}])
        return json.dumps(result, ensure_ascii=False), request

    def active(self):
        return [r for r in self.bot._repair.store.items(OWNER) if r["status"] == "active"]

    async def test_real_delivery_review_restart_and_relevant_private_reply(self):
        await self.sent()
        await self.correct()
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 2)
        row, = self.active()
        self.assertEqual(row["old_quote"], OLD)
        self.assertEqual(row["correction_quote"], CORRECTION)
        self.assertEqual(self.bot._collect_roots("面试"), [])
        self.assertFalse(any(r.get("source") == "repair" for r in self.bot._collect_unified("面试")))
        result, _ = await self.reply("related", "我的面试安排是什么时候")
        self.assertIn("周六", result)
        self.assertIn("自述", result)
        unrelated, _ = await self.reply("unrelated", "今晚吃什么好")
        self.assertNotIn(CORRECTION, unrelated)
        self.now += 86401
        self.assertEqual(self.bot._repair.store.recent(OWNER, now=self.now), [])
        await self.bot.on_unload()
        self.bot = self.make_bot()
        result, request = await self.reply("restart", "聊聊我的面试安排", sid="new-private")
        self.assertIn("周六", result)
        self.bot.ctx.llm.generate.assert_not_awaited()
        inspected = await self.bot.repair("list", **request)
        self.assertTrue(inspected["success"], inspected)
        self.assertIn(row["id"], str(inspected))

    async def test_failed_send_or_other_recipient_is_not_a_said_statement(self):
        await self.sent(sent=False)
        await self.sent(mid="wrong-target", sid="other-private")
        await self.sent(mid="group", sid="group-one", group=GROUP)
        self.assertEqual(self.bot._repair.store.recent(OWNER), [])
        await self.correct()
        self.bot.ctx.llm.generate.assert_not_awaited()
        self.assertEqual(self.active(), [])

    async def test_ambiguous_vague_quoted_media_and_other_speaker_do_not_learn(self):
        await self.sent()
        for mid, text, kw in (("vague", "你刚才说错了", {}),
                              ("reported", "朋友说："+CORRECTION, {}),
                              ("media", CORRECTION, {"extra": {"is_picture": True}}),
                              ("other", CORRECTION, {"uid": OTHER, "sid": "other-private"}),
                              ("group", CORRECTION, {"sid": "group-one", "group": GROUP})):
            await self.capture(mid, text, **kw)
            await self.drain()
        self.assertEqual(self.active(), [])
        self.bot.ctx.llm.generate.assert_not_awaited()
        await self.sent("我记得你周五要参加面试。", "out-two")
        await self.correct()
        self.bot.ctx.llm.generate.assert_not_awaited()

    async def test_group_and_other_owner_never_receive_private_repairs(self):
        await self.sent()
        await self.correct()
        for mid, kw in (("group", {"group": GROUP, "sid": "group-one"}),
                        ("other", {"uid": OTHER, "sid": "other-private"})):
            result, request = await self.reply(mid, "聊聊面试安排", **kw)
            self.assertNotIn("周六", result)
            self.assertFalse((await self.bot.repair("list", **request))["success"])
        self.bot.config.owner.qq = OTHER
        self.bot._ensure_unified()
        result, _ = await self.reply("new-owner", "聊聊面试安排", uid=OTHER, sid="new-owner")
        self.assertNotIn("周六", result)

    async def test_forget_clears_text_and_original_correction_replay_cannot_revive_it(self):
        await self.sent()
        await self.correct()
        row, = self.active()
        await self.capture("forget", f"/修复 忘记 {row['id']} {row['revision']}")
        await self.drain()
        self.assertEqual(self.active(), [])
        self.assertEqual(self.bot._repair.store.recent(OWNER), [])
        count = self.bot.ctx.llm.generate.await_count
        await self.sent()
        await self.correct()
        self.assertEqual(self.bot.ctx.llm.generate.await_count, count)
        self.assertEqual(self.active(), [])
        for table in ("repair_items", "repair_inputs", "repair_receipts"):
            raw = repr([tuple(r) for r in self.bot._repair.store.con.execute("SELECT * FROM "+table).fetchall()])
            self.assertNotIn(CORRECTION, raw)
        result, _ = await self.reply("after-forget", "面试是什么时候")
        self.assertNotIn("周六", result)

    async def test_two_stage_rejection_does_not_turn_partial_quote_into_admitted_fact(self):
        await self.sent("我不认为你周五要参加面试，先确认日期吧。")
        await self.correct(supported=False)
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 2)
        self.assertEqual(self.active(), [])
        result, _ = await self.reply("later", "面试是什么时候")
        self.assertNotIn("周六", result)

    async def test_hot_disable_during_model_does_not_commit_a_repair(self):
        await self.sent()
        async def model(*args, **kwargs):
            self.bot.config.repair.enabled = False
            return response({"old_quote": OLD, "correction_quote": CORRECTION})
        self.bot.ctx.llm.generate.side_effect = model
        await self.capture("correct", CORRECTION)
        await self.drain()
        self.assertEqual(self.active(), [])

    async def test_pending_hint_preserves_the_full_negated_statement(self):
        sent = "我不认为你周五要参加面试，只有日期确认了才安排。"
        await self.sent(sent)
        self.bot.config.repair.auto_review = False
        result, _ = await self.reply("pending", CORRECTION)
        self.assertIn(sent, result)
        self.assertIn("修复尚未核验", result)
        self.assertIn("不要将截取片段", result)
        self.assertEqual(self.active(), [])
        self.bot.ctx.llm.generate.assert_not_awaited()
        result, _ = await self.reply("after-pending", "今晚吃什么")
        self.assertNotIn(sent, result)

    async def test_forgetting_during_second_review_blocks_late_commit(self):
        await self.sent()
        await self.correct()
        row, = self.active()
        self.now += 301
        newer = "你刚才说“周五要参加面试”，其实我面试已经改到了周日。"
        reviewing, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def model(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return response({"old_quote": OLD, "correction_quote": newer})
            reviewing.set()
            await release.wait()
            return response({"supported": True})

        self.bot.ctx.llm.generate.side_effect = model
        with patch("time.time", return_value=self.now):
            await self.capture("new-correction", newer)
            await asyncio.wait_for(reviewing.wait(), 3)
            await self.capture("forget-inflight", f"/修复 忘记 {row['id']} {row['revision']}")
            release.set()
            await self.drain()
        self.assertEqual(calls, 2)
        self.assertEqual(self.active(), [])
        self.assertEqual(self.bot._repair.store.get(OWNER, row["id"])["status"], "forgotten")
        self.assertEqual(self.bot._repair.store.recent(OWNER, now=self.now), [])
        result, _ = await self.reply("after-late-review", "面试是什么时候")
        self.assertNotIn("周日", result)

    async def test_expired_or_overlong_original_never_becomes_a_partial_statement(self):
        await self.sent("完整限定"*350, "long")
        self.assertEqual(self.bot._repair.store.recent(OWNER), [])
        await self.sent()
        self.now += 86401
        await self.correct()
        self.bot.ctx.llm.generate.assert_not_awaited()
        self.assertEqual(self.active(), [])

    async def test_shared_budget_cannot_be_bypassed_to_review_a_repair(self):
        await self.sent()
        self.bot.config.unified_mind.max_daily_model_calls = 0
        await self.correct()
        self.bot.ctx.llm.generate.assert_not_awaited()
        self.assertEqual(self.active(), [])


if __name__ == "__main__":
    unittest.main()
