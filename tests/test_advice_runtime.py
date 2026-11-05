"""Actual identity/source/delivery stores, offline Host transport and model doubles."""
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from MaiLivingWorld.advice_runtime import AdviceRuntime
from MaiLivingWorld.tests.test_runtime import FakeContext, plugin
from MaiLivingWorld.tests.test_mcp_plugin import message, OWNER, OTHER, GROUP
from MaiLivingWorld.unified_expression import parse_expression
from test_advice_model import response
from test_advice_store import ADVICE


class Settings(SimpleNamespace):
    def model_dump(self):
        return vars(self).copy()


class AdviceRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        b = self.bot = plugin.MaiLivingWorld()
        b.ctx = FakeContext()
        b.ctx.paths = SimpleNamespace(data_dir=Path(self.temp.name))
        b.config = plugin.Config()
        if not hasattr(b.config, "advice"):
            object.__setattr__(b.config, "advice", Settings(enabled=False, auto_feedback=True, interval_seconds=600, max_daily_batches=4))
        b.config.owner.qq = OWNER
        b.config.owner.enabled = b.config.unified_mind.enabled = b.config.advice.enabled = True
        b.config.unified_mind.mode = "active"
        b._ensure_unified()
        self.runtime = AdviceRuntime(b)
        b._advice = self.runtime
        self.source = b._ensure_tool_store().put_observation("cap", "album:sample", "学习安排可以分成小任务，一次开始一件事。", "private:"+OWNER, subject_id=OWNER)
        self.context()

    def context(self, mid="question", text="我学习的时候有点没头绪", uid=OWNER, sid="private-one", group=None):
        event = message(mid=mid, text=text, uid=uid, sid=sid, group=group)
        self.bot._unified.capture(event, private_users=[OWNER, OTHER], group_ids=[GROUP])
        return {"message": event}

    async def prepare(self, **changes):
        options = {"action": "prepare", "topic": "学习安排", "advice": ADVICE, "source_ids": [self.source["id"]], "kwargs": self.context()}
        options.update(changes)
        result = await self.runtime.tool(**options)
        self.assertTrue(result["success"], result)
        return result

    async def delivered(self):
        result = await self.prepare()
        outgoing = message(mid="sent", uid="99999999", text=result["delivery_text"])
        self.assertTrue(await self.runtime.sent({"sent": True, "message": outgoing}))
        return self.runtime.store.get(OWNER, result["data"]["id"])

    async def asyncTearDown(self):
        if hasattr(self.bot, "_advice"):
            self.bot._advice = None
        self.runtime.close()
        await self.bot.on_unload()
        self.temp.cleanup()

    async def test_actual_send_receipt_required_not_tool_or_failed_send(self):
        result = await self.prepare()
        self.assertEqual(self.runtime.store.items(OWNER)[0]["status"], "draft")
        outgoing = message(mid="sent", uid="99999999", text=result["delivery_text"])
        self.assertFalse(await self.runtime.sent({"sent": False, "message": outgoing}))
        self.assertFalse(await self.runtime.sent({"sent": True, "message": {**outgoing, "raw_message": [{"type": "reply"}]}}))
        self.assertTrue(await self.runtime.sent({"sent": True, "message": outgoing}))
        self.assertFalse(await self.runtime.sent({"sent": True, "message": outgoing}))
        self.assertEqual(self.runtime.store.items(OWNER)[0]["outcome"], "unknown")
        self.assertEqual(self.runtime.records(), [])

    async def test_natural_report_has_literal_evidence_and_is_owner_private_only(self):
        row = await self.delivered()
        event = message(mid="report", text="你说的“先列一张小清单”我试了，没有帮助")
        self.assertEqual(self.runtime.capture({**event, "message_info": {"user_info": {"user_id": OTHER}}}), (False, False))
        self.assertEqual(self.runtime.capture(message(mid="ambiguous", text="那个有用")), (False, False))
        self.assertEqual(self.runtime.capture(event), (False, True))
        self.bot.ctx.llm.generate.side_effect = [response({"outcome": "unhelpful", "quote": "我试了，没有帮助"}), response({"supported": True})]
        await self.runtime.process()
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 2)
        stored = self.runtime.store.get(OWNER, row["id"])
        self.assertEqual(stored["outcome"], "unhelpful")
        records = self.runtime.records("学习")
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual((record["scope"], record["sensitivity"], record["evidence_status"]), ("private:"+OWNER, "personal", "owner_reported"))
        self.assertIn("我试了，没有帮助", record["text"])
        with self.assertRaises(ValueError):
            self.bot._resolve_roots([record["id"]])
        plan = response({"behavior": "neutral", "decisions": [{"id": record["id"], "mode": "say"}]})
        self.assertEqual(parse_expression(plan, records, audience_scope="group:"+GROUP, speaker_id=OWNER, is_owner=True)["memories"], [])
        self.assertTrue(parse_expression(plan, records, audience_scope="private:"+OWNER, speaker_id=OWNER, is_owner=True)["memories"])
        self.assertFalse(self.runtime.capture(event)[1])

    async def test_source_retraction_or_new_message_during_receipt_blocks_activation(self):
        result = await self.prepare()
        async def new_context(*args, **kwargs):
            self.context(mid="new")
            return {"stream_id": "private-one"}
        self.bot.ctx.chat.get_stream_by_user_id.side_effect = new_context
        self.assertFalse(await self.runtime.sent({"sent": True, "message": message(mid="out", uid="99999999", text=result["delivery_text"])}))
        self.bot.ctx.chat.get_stream_by_user_id.side_effect = None
        self.context()
        self.bot._tool_store.retract_observation("cap", "album:sample")
        self.assertFalse(await self.runtime.sent({"sent": True, "message": message(mid="out2", uid="99999999", text=result["delivery_text"])}))

    async def test_forget_during_model_await_discards_late_result(self):
        row = await self.delivered()
        self.runtime.capture(message(mid="feedback", text="你说的“先列一张小清单”我试了，没有帮助"))
        async def generate(*args, **kwargs):
            self.runtime.capture(message(mid="forget", text="/建议 忘记 "+row["id"]+" "+str(row["revision"])))
            return response({"outcome": "unhelpful", "quote": "我试了，没有帮助"})
        self.bot.ctx.llm.generate.side_effect = generate
        await self.runtime.process()
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 1)
        self.assertEqual(self.runtime.store.get(OWNER, row["id"])["advice"], "")
        self.assertEqual(self.runtime.records(), [])

    async def test_preparation_rejects_other_private_and_shared_reading_sources(self):
        private = self.bot._tool_store.put_observation("cap", "album:other", "另一个人的学习安排", "private:"+OTHER, subject_id=OTHER)
        result = await self.runtime.tool("prepare", "学习", ADVICE, [private["id"]], self.context())
        self.assertFalse(result["success"])
        self.bot._expression_records = lambda records, context=None: []
        result = await self.runtime.tool("prepare", "学习", ADVICE, [self.source["id"]], self.context())
        self.assertFalse(result["success"])
        self.assertEqual(self.runtime.store.items(OWNER), [])

    async def test_group_other_user_and_old_context_cannot_prepare(self):
        for context in (self.context(uid=OTHER, sid="other"), self.context(group=GROUP, sid="group-one")):
            result = await self.runtime.tool("prepare", "学习", ADVICE, [self.source["id"]], context)
            self.assertFalse(result["success"])
        old = self.context(mid="old")
        self.context(mid="latest")
        self.assertFalse((await self.runtime.tool("prepare", "学习", ADVICE, [self.source["id"]], old))["success"])

    async def test_public_advice_report_stays_personal_and_source_change_removes_it(self):
        self.source = self.bot._tool_store.put_observation("cap", "album:public", "学习可以安排一个小目标", "public", subject_id="", sensitivity="public")
        row = await self.delivered()
        self.assertTrue(self.runtime.capture(message(mid="report", text="/建议 有用 "+row["id"]+" "+str(row["revision"])))[0])
        record = self.runtime.records()[0]
        self.assertEqual(record["sensitivity"], "personal")
        self.bot._tool_store.put_observation("cap", "album:public", "资料已更新", "public", subject_id="")
        self.assertEqual(self.runtime.records(), [])

    async def test_low_shared_budget_defers_without_any_model_and_keeps_report(self):
        await self.delivered()
        self.runtime.capture(message(mid="report", text="你说的“先列一张小清单”我试了，没有帮助"))
        self.bot.config.unified_mind.max_daily_model_calls = self.bot.config.unified_mind.reserved_reply_calls
        await self.runtime.process()
        self.bot.ctx.llm.generate.assert_not_awaited()
        self.assertEqual(self.runtime.last_status, "budget")
        self.assertEqual(self.runtime.store.state(OWNER)["cycle_count"], 1)

    async def test_sources_support_real_brain_context_and_exclude_other_private(self):
        self.bot.config.brain_memory.enabled = True
        db = self.bot._ensure_brain()
        now = time.time()
        with db.con:
            db.con.execute("INSERT INTO brain_facts(id,scope,subject_id,attribute,value,confidence,status,created,updated) VALUES (?,?,?,'学习安排',?,1,'active',?,?)",
                           ("FAdvice", "private:"+OWNER, OWNER, "任务太多时我常常不知道从何开始", now, now))
            db.con.execute("INSERT INTO brain_facts(id,scope,subject_id,attribute,value,confidence,status,created,updated) VALUES (?,?,?,'学习安排',?,1,'active',?,?)",
                           ("FPrivate", "private:"+OTHER, OTHER, "另一个人的私人学习限制", now, now))
        sources = await self.runtime.tool("sources", topic="学习安排", kwargs=self.context())
        self.assertTrue(sources["success"])
        ids = [row["id"] for row in sources["data"]]
        self.assertIn("brain:fact:FAdvice", ids)
        self.assertNotIn("brain:fact:FPrivate", ids)
        result = await self.prepare(source_ids=["brain:fact:FAdvice"])
        self.assertTrue(await self.runtime.sent({"sent": True, "message": message(mid="out", uid="99999999", text=result["delivery_text"])}))
        row = self.runtime.store.items(OWNER)[0]
        self.runtime.capture(message(mid="feedback", text="/建议 没用 "+row["id"]+" "+str(row["revision"])))
        before = self.runtime.records()
        self.runtime.close()
        self.runtime = AdviceRuntime(self.bot)
        self.bot._advice = self.runtime
        self.assertEqual(self.runtime.records(), before)
