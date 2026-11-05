"""A real sent receipt reaches the private reply gate without a group-side hint."""
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from MaiLivingWorld.tests.test_runtime import FakeContext, plugin
from MaiLivingWorld.tests.test_mcp_plugin import OWNER, GROUP, message
from MaiLivingWorld.growth_store import source_fingerprint
from MaiLivingWorld.store import Store


class RecollectionExpressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        b = self.bot = plugin.MaiLivingWorld()
        b.ctx = FakeContext()
        b.ctx.paths = SimpleNamespace(data_dir=Path(self.temp.name))
        b.config = plugin.Config()
        b.config.owner.enabled = b.config.unified_mind.enabled = b.config.recollection.enabled = True
        b.config.owner.qq = OWNER
        b.config.unified_mind.mode = "active"
        b.config.unified_mind.min_interval_seconds = 0
        b.config.recollection.auto_correct = False
        b.config.brain_memory.enabled = True
        b.config.brain_memory.group_ids = [GROUP]
        b._store = Store(Path(self.temp.name)/"living_world.sqlite3")
        b._ensure_unified()
        b._ensure_recollection()
        self.ident = "brain:fact:exam"
        self.raw = "主人正在准备考试，担心复习进度。"
        brain = b._ensure_brain()
        stamp = time.time()-100
        with brain.con:
            brain.con.execute("INSERT INTO brain_facts(id,scope,subject_id,attribute,value,confidence,status,created,updated) "
                "VALUES ('exam',?,?,'近况',?,1,'active',?,?)", ("private:"+OWNER, OWNER, self.raw, stamp, stamp))
        b.ctx.llm.generate = AsyncMock(return_value={"response": json.dumps({"behavior": "patient", "decisions": [
            {"id": self.ident, "mode": "say"}]})})

    async def asyncTearDown(self):
        await self.bot.on_unload()
        self.temp.cleanup()

    async def reply(self, *, group=False):
        sid, mid = ("group-one", "group-input") if group else ("private-one", "owner-input")
        return await self.bot.brain_reply_hint(session_id=sid, reply_message_id=mid,
            messages=[{"role": "system", "content": "原始宿主系统提示"}])

    def prompt_records(self):
        prompt = self.bot.ctx.llm.generate.await_args.args[0]
        return json.loads(prompt.split("UNTRUSTED_INPUT_JSON\n", 1)[1])["memories"]

    async def test_sent_question_invalidates_private_cache_without_marking_group_memory(self):
        b = self.bot
        await b.capture_unified(message=message(mid="owner-input", text="聊聊考试的近况"))
        root_before = b._resolve_roots([self.ident])[self.ident]
        await self.reply()
        self.assertNotIn("recently_asked", next(row for row in self.prompt_records() if row["id"] == self.ident))
        self.assertEqual(b.ctx.llm.generate.await_count, 1)

        outgoing = message(mid="actual-host-receipt", uid="99999999", text="考试准备得怎么样？")
        outgoing["account_id"] = "99999999"
        await b.question_send_feedback(sent=True, message=outgoing)
        self.assertIn(self.ident, b._recollection.asked())
        rendered = json.dumps(await self.reply(), ensure_ascii=False)
        self.assertEqual(b.ctx.llm.generate.await_count, 2, "a real receipt must invalidate an earlier private expression cache")
        self.assertTrue(next(row for row in self.prompt_records() if row["id"] == self.ident)["recently_asked"])
        self.assertIn("这条近况近期已问过，本轮未主动谈及则不再次追问", rendered)
        self.assertEqual(source_fingerprint(root_before), source_fingerprint(b._resolve_roots([self.ident])[self.ident]))

        await b.capture_unified(message=message(mid="group-input", sid="group-one", group=GROUP,
                                                text="聊聊考试的近况"))
        rendered = json.dumps(await self.reply(group=True), ensure_ascii=False)
        self.assertEqual(b.ctx.llm.generate.await_count, 3)
        self.assertNotIn("recently_asked", next(row for row in self.prompt_records() if row["id"] == self.ident))
        self.assertNotIn("这条近况近期已问过", rendered)
        self.assertNotIn(self.raw, rendered)


if __name__ == "__main__":
    unittest.main()
