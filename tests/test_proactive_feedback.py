"""主动聊天唤醒回执、沉默退避与不同会话归因回归测试。"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from MaiLivingWorld.tests.test_runtime import plugin, FakeContext
from MaiLivingWorld.proactive_feedback import ProactiveFeedback, decision_from_items


def tool(name: str) -> list[dict]:
    return [{"item_type":"FunctionCallItem","tool_call":{"func_name":name,"call_id":"test-call","args":{}}}]


class ProactiveFeedbackPolicyTests(unittest.TestCase):
    def test_only_real_function_items(self):
        self.assertEqual(decision_from_items(tool("stop")), "planner_stop")
        self.assertEqual(decision_from_items(tool("reply")), "reply_planned")
        self.assertEqual(decision_from_items([{"item_type":"ReasoningItem","text":"stop"}]), "")
        self.assertEqual(decision_from_items(None), "")

    def test_stop_backoff_and_confirmed_send_are_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            db = ProactiveFeedback(Path(directory) / "feedback.db")
            now = time.time()
            target = "qq:private:123"
            one = db.begin(target, "sid", now)
            self.assertTrue(db.finish(one, "planner_stop", now=now, base_backoff_minutes=30, max_backoff_minutes=120))
            self.assertFalse(db.finish(one, "send_confirmed", now=now))
            self.assertFalse(db.check(target, now+1200, now-100, 3600, 1)[0])
            self.assertTrue(db.check(target, now+1860, now-100, 3600, 1)[0])
            two = db.begin(target, "sid", now+1860)
            db.finish(two, "planner_stop", now=now+1860, base_backoff_minutes=30, max_backoff_minutes=120)
            self.assertFalse(db.check(target, now+4800, now-100, 3600, 1)[0])
            three = db.begin(target, "sid", now+5600)
            db.finish(three, "reply_planned", now=now+5600)
            db.finish(three, "send_confirmed", now=now+5601)
            self.assertFalse(db.finish(three, "send_confirmed", now=now+5601))
            state = db.status(target, day_start=now-100)
            self.assertEqual(state["today"].get("send_confirmed"), 1)
            self.assertEqual(state["stop_streak"], 0)
            self.assertFalse(db.check(target, now+5700, now-100, 0, 1)[0])
            db.close()
            reopened = ProactiveFeedback(Path(directory) / "feedback.db")
            self.assertEqual(reopened.status(target, day_start=now-100)["today"]["send_confirmed"], 1)
            reopened.close()

    def test_expiry_never_counts_as_send(self):
        with tempfile.TemporaryDirectory() as directory:
            db = ProactiveFeedback(Path(directory) / "feedback.db")
            event = db.begin("qq:private:123", "sid", 100)
            self.assertEqual(db.expire_unfinished(now=500, timeout_seconds=240), 1)
            self.assertEqual(db.outcome(event), "unknown")
            self.assertEqual(db.status("qq:private:123", day_start=0)["today"].get("send_confirmed", 0), 0)
            db.close()


class ProactiveFeedbackHookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.bot = plugin.MaiLivingWorld()
        self.bot.config = plugin.Config()
        self.bot.ctx = FakeContext()
        self.bot._feedback = ProactiveFeedback(Path(self.dir.name) / "feedback.db")

    async def asyncTearDown(self):
        self.bot._feedback.close()
        self.dir.cleanup()

    async def test_planner_stop_never_becomes_send(self):
        event = self.bot._feedback.begin("qq:private:123", "private-one", time.time())
        self.bot._pending_proactive["private-one"] = (event, time.time(), "qq:private:123")
        await self.bot.proactive_planner_feedback(session_id="private-one", output_items=tool("stop"))
        await self.bot.proactive_send_feedback(message={"platform":"qq","session_id":"private-one"}, sent=True)
        self.assertEqual(self.bot._feedback.outcome(event), "planner_stop")
        self.assertFalse(self.bot._pending_proactive)

    async def test_reply_requires_host_success_and_exact_session(self):
        event = self.bot._feedback.begin("qq:private:123", "private-one", time.time())
        self.bot._pending_proactive["private-one"] = (event, time.time(), "qq:private:123")
        await self.bot.proactive_planner_feedback(session_id="private-one", output_items=tool("reply"))
        await self.bot.proactive_send_feedback(message={"platform":"qq","session_id":"wrong"},sent=True)
        self.assertEqual(self.bot._feedback.outcome(event),"reply_planned")
        await self.bot.proactive_send_feedback(message={"platform":"qq","session_id":"private-one"},sent=True)
        await self.bot.proactive_send_feedback(message={"platform":"qq","session_id":"private-one"},sent=True)
        self.assertEqual(self.bot._feedback.outcome(event),"send_confirmed")

    async def test_incoming_private_message_interrupts_attribution(self):
        event = self.bot._feedback.begin("qq:private:123","private-one",time.time())
        self.bot._pending_proactive["private-one"] = (event, time.time(), "qq:private:123")
        await self.bot.proactive_planner_feedback(session_id="private-one", output_items=tool("reply"))
        await self.bot.proactive_interrupted(message={
            "platform":"qq","session_id":"private-one","account_id":"999",
            "message_info":{"user_info":{"user_id":"123"}}
        })
        await self.bot.proactive_send_feedback(message={"platform":"qq","session_id":"private-one"},sent=True)
        self.assertEqual(self.bot._feedback.outcome(event),"interrupted")


if __name__ == "__main__":
    unittest.main()
