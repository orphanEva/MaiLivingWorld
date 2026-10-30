"""经验闭环：可信身份证据、真实发送、幂等反馈及策略边界。"""
from __future__ import annotations

import sqlite3
import tempfile
import time
import types
import unittest
from pathlib import Path

from MaiLivingWorld.experience_loop import ExperienceLoop, feedback_signal
from MaiLivingWorld.tests.test_runtime import FakeContext, plugin
from MaiLivingWorld.proactive_feedback import ProactiveFeedback
from MaiLivingWorld.tests.test_proactive_feedback import outgoing, tool
from MaiLivingWorld.store import Store


def incoming(uid="12345678", sid="private-one", text="谢谢你分享", mid="m1", group=None):
    return {
        "platform": "qq", "session_id": sid, "message_id": mid,
        "processed_plain_text": text,
        "account_id": "99999999",
        "message_info": {
            "user_info": {"user_id": uid},
            "group_info": {"group_id": group} if group else None,
        }
    }


class ExperienceStoreTests(unittest.TestCase):
    def test_only_explicit_signal(self):
        self.assertEqual(feedback_signal("好"), "reply_only")
        self.assertEqual(feedback_signal("我懂了"), "reply_only")
        self.assertEqual(feedback_signal("谢谢你分享！"), "positive")
        self.assertEqual(feedback_signal("这个话题没兴趣。"), "negative")
        self.assertEqual(feedback_signal("别再主动找我了！"), "opt_out")
        self.assertEqual(feedback_signal("他说别再主动找我了"), "reply_only")

    def test_end_to_end_and_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            store = ExperienceLoop(Path(d) / "loop.db")
            t = 10000.0
            self.assertTrue(store.begin(1, "12345678", "sid", "explore", "public:key", now=t))
            self.assertFalse(store.begin(1, "12345678", "sid", "history", "other", now=t))
            self.assertFalse(store.mark(1, "send_confirmed", now=t + 1))
            self.assertTrue(store.mark(1, "reply_planned", now=t + 2))
            self.assertTrue(store.mark(1, "send_confirmed", now=t + 3))
            self.assertFalse(store.mark(1, "planner_stop", now=t + 4))
            self.assertIsNone(store.observe("not-owner", "sid", "m1", "谢谢你分享", now=t + 4))
            self.assertIsNone(store.observe("12345678", "other", "m1", "谢谢你分享", now=t + 4))
            self.assertEqual(store.observe("12345678", "sid", "m1", "今天下雨了", now=t + 4,
                                            active=True), "reply_only")
            self.assertIsNone(store.observe("12345678", "sid", "m1", "今天下雨了", now=t + 5))
            self.assertEqual(store.observe("12345678", "sid", "m2", "这个话题没兴趣", now=t + 6,
                                            active=False), "negative")
            self.assertEqual(store.allow("12345678", "explore", now=t + 7, active=True), (True, "ok"))
            self.assertTrue(store.begin(2, "12345678", "sid", "history", "open_topic", now=t + 8))
            store.mark(2, "reply_planned", now=t + 9)
            store.mark(2, "send_confirmed", now=t + 10)
            self.assertEqual(store.observe("12345678", "sid", "m3", "这个话题没兴趣", now=t + 11,
                                            active=True, negative_pause_hours=6), "negative")
            self.assertEqual(store.allow("12345678", "history", now=t + 12, active=True),
                             (False, "explicit_negative_cooldown"))
            self.assertEqual(store.allow("12345678", "history", now=t + 12, active=False), (True, "ok"))
            self.assertEqual(store.allow("12345678", "explore", now=t + 12, active=True), (True, "ok"))
            self.assertEqual(store.allow("12345678", "history", now=t + 6 * 3600 + 12, active=True), (True, "ok"))
            self.assertEqual(store.status("12345678")["recent"][0]["feedback"], "negative")
            self.assertFalse(store.con.execute("SELECT 1 FROM experience_evidence WHERE evidence_digest LIKE '%今天%'").fetchone())
            store.close()
            reopened = ExperienceLoop(Path(d) / "loop.db")
            self.assertEqual(reopened.status("12345678")["recent"][0]["feedback"], "negative")
            reopened.close()

    def test_no_reply_is_not_failure_and_explicit_opt_out_persists(self):
        with tempfile.TemporaryDirectory() as d:
            db = ExperienceLoop(Path(d) / "loop.db")
            t = time.time()
            db.begin(8, "12345678", "sid", "explore", "topic", now=t)
            db.mark(8, "planner_stop", now=t + 1)
            self.assertIsNone(db.observe("12345678", "sid", "m1", "谢谢你分享", now=t + 2))
            self.assertEqual(db.status("12345678")["recent"][0]["feedback"], "unrated")
            self.assertEqual(db.observe("12345678", "sid", "m2", "不要再主动联系我", now=t + 3), "opt_out")
            self.assertEqual(db.allow("12345678", "explore", now=t + 4, active=False),
                             (False, "owner_opted_out"))
            db.close()
            db = ExperienceLoop(Path(d) / "loop.db")
            self.assertEqual(db.allow("12345678", "history", now=t + 5, active=True),
                             (False, "owner_opted_out"))
            self.assertTrue(db.resume("12345678"))
            self.assertEqual(db.allow("12345678", "history", now=t + 5, active=True), (True, "ok"))
            db.close()


class ExperienceHooksTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.bot = plugin.MaiLivingWorld()
        self.bot.config = plugin.Config()
        self.bot.config.plugin.enabled = True
        self.bot.config.general.enabled = True
        self.bot.config.general.dry_run = False
        self.bot.config.owner.enabled = True
        self.bot.config.owner.qq = "12345678"
        self.bot.config.experience.enabled = True
        self.bot.config.experience.mode = "active"
        self.bot.ctx = FakeContext()
        self.bot.ctx.paths = types.SimpleNamespace(data_dir=Path(self.dir.name))
        self.bot._feedback = ProactiveFeedback(Path(self.dir.name) / "feedback.db")
        self.bot._store = Store(Path(self.dir.name) / "stories.db")
        self.bot._experience = ExperienceLoop(Path(self.dir.name) / "experience.db")

    async def asyncTearDown(self):
        self.bot._experience.close()
        self.bot._feedback.close()
        self.bot._store.close()
        self.dir.cleanup()

    async def test_real_send_then_verified_feedback(self):
        now = time.time()
        eid = self.bot._feedback.begin("qq:private:12345678", "private-one", now)
        self.bot._experience.begin(eid, "12345678", "private-one", "explore", "topic", now=now)
        self.bot._pending_proactive["private-one"] = (eid, now, "qq:private:12345678")
        await self.bot.proactive_planner_feedback(session_id="private-one", output_items=tool("reply"))
        await self.bot.proactive_send_feedback(message=outgoing("private-one"), sent=True)
        self.assertEqual(self.bot._experience.status("12345678")["recent"][0]["outcome"], "send_confirmed")
        for msg in [
            incoming(uid="87654321"),
            incoming(sid="other"),
            incoming(group="22222222"),
            incoming(mid="", text="这个话题没兴趣"),
        ]:
            await self.bot.capture_experience_feedback(message=msg)
        self.assertEqual(self.bot._experience.status("12345678")["recent"][0]["feedback"], "unrated")
        await self.bot.capture_experience_feedback(message=incoming(text="这个话题没兴趣"))
        self.assertEqual(self.bot._experience.status("12345678")["recent"][0]["feedback"], "negative")
        self.assertFalse(self.bot._experience.allow("12345678", "explore", active=True)[0])
        self.assertTrue(self.bot._experience.allow("12345678", "history", active=True)[0])

    async def test_owner_opt_out_blocks_target(self):
        await self.bot.capture_experience_feedback(
            message=incoming(text="别再主动找我了", mid="optout-01"))
        self.assertTrue(self.bot._experience.status("12345678")["paused"])
        from datetime import datetime
        from zoneinfo import ZoneInfo
        await self.bot._try_target("private", "12345678", datetime.now(ZoneInfo("Asia/Shanghai")))
        self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()
        self.assertFalse((await self.bot.experience_resume(confirm="bad"))["success"])
        self.assertTrue((await self.bot.experience_resume(confirm="RESUME_OWNER_PROACTIVE"))["success"])
        self.assertFalse(self.bot._experience.status("12345678")["paused"])


if __name__ == "__main__":
    unittest.main()
