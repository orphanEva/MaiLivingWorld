"""Fix regressions: long-idle history, deferred public memory, review state and cognition."""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from MaiLivingWorld.tests.test_runtime import FakeContext, plugin
from MaiLivingWorld.tests.test_proactive_feedback import outgoing, tool
from MaiLivingWorld.explore import Story
from MaiLivingWorld.mind import MindStore
from MaiLivingWorld.proactive_feedback import ProactiveFeedback
from MaiLivingWorld.store import Store


class ReviewRegressionTests(unittest.TestCase):
    def test_dismiss_conflict_preserves_pending(self):
        with tempfile.TemporaryDirectory() as folder:
            db = MindStore(Path(folder) / "mind.sqlite3")
            owner = "12345678"
            evidence = [{"id": "E1", "role": "owner", "digest": "digest1"}]
            note = {"kind": "preference", "basis": "user_explicit",
                    "text": "最近在研究RNN的训练细节", "confidence": 0.85,
                    "evidence_ids": ["E1"]}
            db.save_reflection(owner, "h1", [note], [], evidence, time.time(), 20)
            before = db.notes(owner)[0]
            self.assertEqual(before["status"], "pending")
            db.save_reflection(owner, "h2", [],
                               [{"target_key": before["note_key"],
                                 "reason": "新的说法可能和旧偏好有冲突", "evidence_ids": ["E1"]}],
                               evidence, time.time() + 1, 20)
            self.assertEqual(db.notes(owner)[0]["status"], "disputed")
            rid = db.reviews(owner)[0]["id"]
            self.assertTrue(db.decide(owner, rid, "reject"))
            self.assertEqual(db.notes(owner)[0]["status"], "pending")
            db.close()


class ProactiveRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_messages_fallback_and_memory_only_after_send(self):
        with tempfile.TemporaryDirectory() as folder:
            bot = plugin.MaiLivingWorld()
            bot.ctx = FakeContext()
            bot.config = plugin.Config()
            bot.config.general.enabled = True
            bot.config.general.dry_run = False
            bot.config.brain.use_llm_selection = False
            bot.config.explorer.enabled = True
            bot.config.memory.enabled = True
            bot.config.audience.history_gate = False
            bot._store = Store(Path(folder) / "stories.sqlite3")
            bot._feedback = ProactiveFeedback(Path(folder) / "feedback.sqlite3")
            story = Story("history-key", "RSS", "一条科技消息",
                          "https://example.org/story", "一些公开的科技信息", time.time())
            bot._store.save_stories([story])
            bot.ctx.message.get_recent.return_value = []
            # First query checks idle period; second fetches older persistent messages.
            bot.ctx.message.get_by_time_in_chat = AsyncMock(
                side_effect=[[], [{"message_id": "old-1"}, {"message_id": "old-2"}]])
            bot._remember = AsyncMock()
            now = datetime.now(ZoneInfo("Asia/Shanghai"))
            await bot._try_target("private", "123", now)
            self.assertEqual(bot.ctx.maisaka.proactive.trigger.await_count, 1)
            self.assertEqual(bot.ctx.message.get_by_time_in_chat.await_count, 2)
            bot._remember.assert_not_awaited()
            sid = "private-one"
            event_id = bot._pending_proactive[sid][0]
            await bot.proactive_planner_feedback(session_id=sid, output_items=tool("reply"))
            bot._remember.assert_not_awaited()
            await bot.proactive_send_feedback(message=outgoing(sid), sent=True)
            self.assertEqual(bot._feedback.outcome(event_id), "send_confirmed")
            bot._remember.assert_awaited_once()
            bot._feedback.close()
            bot._store.close()

    async def test_planner_stop_does_not_write_memory(self):
        with tempfile.TemporaryDirectory() as folder:
            bot = plugin.MaiLivingWorld()
            bot.ctx = FakeContext()
            bot.config = plugin.Config()
            bot._feedback = ProactiveFeedback(Path(folder) / "feedback.sqlite3")
            eid = bot._feedback.begin("qq:private:123", "private-one", time.time())
            bot._pending_proactive["private-one"] = (eid, time.time(), "qq:private:123")
            bot._pending_memory[eid] = {"key": "a"}
            bot._remember = AsyncMock()
            await bot.proactive_planner_feedback(session_id="private-one", output_items=tool("stop"))
            bot._remember.assert_not_awaited()
            self.assertNotIn(eid, bot._pending_memory)
            bot._feedback.close()

    async def test_public_cognition_independent_of_proactive_switch(self):
        with tempfile.TemporaryDirectory() as folder:
            bot = plugin.MaiLivingWorld()
            bot.ctx = FakeContext()
            bot.ctx.paths = types.SimpleNamespace(data_dir=Path(folder))
            bot.config = plugin.Config()
            bot.config.general.enabled = False
            bot.config.explorer.enabled = True
            bot.config.cognition.enabled = True
            bot.config.cognition.auto_think = True
            bot._store = Store(Path(folder) / "stories.sqlite3")
            bot._store.save_stories([
                Story("pub-key", "RSS", "研究报告发布",
                      "https://example.org/public", "报告中提出了一些新研究问题", time.time())
            ])
            bot.ctx.llm.generate.return_value = {"content": json.dumps({
                "new": [{"source_key": "pub-key", "topic": "研究报告",
                         "question": "这些研究结论有哪些独立验证？",
                         "observation": "公开标题声称发布了一份研究报告",
                         "uncertainty": "尚未阅读研究报告正文和验证过程",
                         "next_action": "wait_for_update"}], "updates": []
            }, ensure_ascii=False)}
            await bot._think_public()
            self.assertEqual(bot._cognition.summary()["goals"].get("open"), 1)
            bot._cognition.close()
            bot._store.close()


if __name__ == "__main__":
    unittest.main()
