"""Delivered QQ recall notices, persistent withdrawal and concurrent writers."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from MaiLivingWorld.chat_media import recalled_media_target
from MaiLivingWorld.mcp_runtime import digest
from MaiLivingWorld.tests.test_mcp_plugin import GROUP, OTHER, OWNER, message
from MaiLivingWorld.tests.test_runtime import FakeContext, plugin
from MaiLivingWorld.tests.test_visual_plugin import media_event
from MaiLivingWorld.tool_store import ToolStore


def notice(mid, *, author=OWNER, group=None, actor=None):
    actor = actor or author
    event = message(mid="napcat-notice-example", uid=actor, sid=group or actor, group=group, text="[事件-撤回]")
    kind = "group_recall" if group else "friend_recall"
    payload = {"post_type": "notice", "notice_type": kind, "self_id": event["account_id"],
               "message_id": int(mid), "user_id": author}
    if group:
        payload.update(group_id=group, operator_id=actor)
    event["is_notify"] = True
    event["message_info"]["additional_config"] = {"self_id": event["account_id"], "napcat_notice_type": kind,
                                                   "napcat_notice_payload": payload}
    return event


class VisualRecallTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bot = self.new_bot()

    def new_bot(self):
        bot = plugin.MaiLivingWorld()
        bot.ctx = FakeContext()
        bot.ctx.paths = SimpleNamespace(data_dir=Path(self.tmp.name))
        bot.set_plugin_config({"owner": {"setup_mode": "日常陪伴", "qq": OWNER},
            "brain_memory": {"group_ids": [GROUP]}, "reading": {"chat_media_enabled": True}})
        return bot

    async def asyncTearDown(self):
        await self.bot.on_unload()
        self.tmp.cleanup()

    async def ingest(self, event):
        await self.bot.capture_unified(message=event)
        if self.bot._memory_jobs:
            await asyncio.gather(*self.bot._memory_jobs, return_exceptions=True)

    def test_untrusted_text_and_mismatched_notice_fields(self):
        target = notice("-412", group=GROUP, author=OTHER, actor=OWNER)
        self.assertEqual(recalled_media_target(target), (GROUP, "99999999", "group:"+GROUP, OTHER, "-412"))
        self.assertIsNone(recalled_media_target({**target, "is_notify": False}))
        self.assertIsNone(recalled_media_target({**target, "account_id": "12311231"}))
        extras = target["message_info"]["additional_config"]
        for key, value in [("group_id", "12311231"), ("operator_id", OTHER), ("self_id", OWNER),
                           ("notice_type", "friend_recall"), ("post_type", "message"), ("message_id", "123x")]:
            with patch.dict(extras["napcat_notice_payload"], {key: value}):
                self.assertIsNone(recalled_media_target(target))

    async def test_only_original_group_observation_is_withdrawn_across_restart(self):
        event = media_event("411", group=GROUP, uid=OTHER)
        model = AsyncMock(side_effect=["蓝杯在书架上。", "树下的木椅。"])
        with patch("MaiLivingWorld.visual_runtime.describe_visual", model):
            await self.ingest(event)
            await self.ingest(media_event("412", group=GROUP, uid=OTHER))
            await self.ingest(notice("411", group=GROUP, author=OWNER))
            self.assertEqual(len(self.bot._ensure_tool_store().observations()), 2)
            await self.bot.on_unload()
            self.bot = self.new_bot()
            self.bot.config.reading.chat_media_enabled = False
            await self.ingest(notice("411", group=GROUP, author=OTHER, actor=OWNER))
            self.bot.config.reading.chat_media_enabled = True
            await self.ingest(event)
            db = self.bot._ensure_tool_store()
            self.assertEqual(len(db.observations()), 1)
            self.assertIn("木椅", db.observations()[0]["text"])
            self.assertNotIn("蓝杯", str(db._read("SELECT text FROM tool_observations")))
            self.assertEqual(model.await_count, 2)
            for _ in range(2):
                await self.ingest(notice("499", group=GROUP, author=OTHER))
            self.assertEqual(len(db._read("SELECT id FROM tool_calls WHERE status='withdrawn'")), 1)

    async def test_private_withdrawal_while_reading_blocks_late_worker_and_replay(self):
        started = asyncio.Event()
        async def slow(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        original = media_event("-421")
        with patch("MaiLivingWorld.visual_runtime.describe_visual", side_effect=slow) as model:
            await self.bot.capture_unified(message=original)
            await asyncio.wait_for(started.wait(), 3)
            await self.ingest(notice("-421"))
            key_parts = [OWNER, original["account_id"], "private:"+OWNER, OWNER, "-421"]
            identity = digest([*key_parts, 0, "image"])
            other = ToolStore(Path(self.tmp.name) / "tool_memory.sqlite3")
            try:
                self.assertIsNone(other.put_observation("chat_media", identity, "迟到的识别", "private:"+OWNER,
                    subject_id=OWNER, media_message_key=digest(key_parts)))
            finally:
                other.close()
            await self.bot.on_unload()
            self.bot = self.new_bot()
            await self.ingest(original)
            self.assertFalse(self.bot._ensure_tool_store().observations())
            self.assertEqual(model.await_count, 1)
