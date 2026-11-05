"""Owner-visible archive lookup keeps delivery, source and audience contracts."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from MaiLivingWorld.advice_runtime import AdviceRuntime, _ref
from MaiLivingWorld.repair_runtime import RepairRuntime
from MaiLivingWorld.tests.test_runtime import FakeContext, plugin
from MaiLivingWorld.tests.test_mcp_plugin import message, OWNER, OTHER, GROUP
from test_memory_archive import ArchiveChecks


class ArchiveRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bot = b = plugin.MaiLivingWorld()
        b.ctx = FakeContext()
        b.ctx.paths = SimpleNamespace(data_dir=Path(self.temp.name))
        b.config = plugin.Config()
        b.config.owner.qq = OWNER
        b.config.owner.enabled = b.config.unified_mind.enabled = True
        b.config.unified_mind.mode = "active"
        b.config.advice.enabled = b.config.repair.enabled = True
        object.__setattr__(b.config.advice, "archive_enabled", True)
        object.__setattr__(b.config.repair, "archive_enabled", True)
        b._ensure_unified()
        self.advice = b._advice = AdviceRuntime(b)
        self.repair = b._repair = RepairRuntime(b)
        self.source = b._ensure_tool_store().put_observation("cap", "album:archive", "可以依据实际安排准备材料并核对日期。", "private:"+OWNER, subject_id=OWNER)

    async def asyncTearDown(self):
        await self.bot.on_unload()
        self.temp.cleanup()

    def context(self, text="聊聊安排", mid="query", *, uid=OWNER, sid="private-one", group=None):
        event = message(text=text, mid=mid, uid=uid, sid=sid, group=group)
        self.bot._unified.capture(event, private_users=[OWNER, OTHER], group_ids=[GROUP])
        self.repair.capture(event)
        return {"message": event}

    def seed_advice(self):
        first = None
        for index in range(65):
            row = self.advice.store.prepare(OWNER, "private-one", "q-"+str(index), "事项%03d" % index,
                "安排%03d先核对资料再决定" % index, [_ref(self.source)], now=100+index*2, archive_enabled=True)
            self.advice.store.record_delivery(OWNER, row["id"], 1, "private-one", row["delivery_text"], "s-"+str(index), now=101+index*2)
            delivered = self.advice.store.get(OWNER, row["id"])
            if index == 0:
                command = message(mid="helpful", text="/建议 有用 "+row["id"]+" "+str(delivered["revision"]))
                self.advice.store.command(command, OWNER, now=101.5)
                first = self.advice.store.get(OWNER, row["id"])
        return first

    def seed_repair(self):
        first = None
        for index in range(65):
            item, _, _ = ArchiveChecks.repair(self, self.repair.store, index, enabled=True)
            first = first or item
        return first

    async def test_old_advice_is_related_recall_only_and_owner_can_search_when_disabled(self):
        first = self.seed_advice()
        records = self.advice.records(first["topic"])
        self.assertEqual(len(records), 1)
        self.assertIn(first["advice"], records[0]["text"])
        self.assertEqual(self.advice.records("完全无关"), [])
        self.assertEqual(self.advice.records(), [])
        self.bot.config.advice.archive_enabled = False
        self.assertEqual(self.advice.records(first["topic"]), [])
        context = self.context()
        search = await self.bot.advice("search", topic=first["topic"], **context)
        self.assertTrue(search["success"])
        hit = next(row for row in search["data"] if row["id"] == first["id"])
        self.assertEqual((hit["memory_tier"], hit["advice"]), ("archive", first["advice"]))
        page = await self.bot.advice("list", **context)
        self.assertEqual((len(page["data"]), page["next_offset"]), (20, 20))
        self.assertEqual((await self.bot.advice("list", offset=60, **context))["next_offset"], None)
        event = message(mid="forget", text="/建议 忘记 "+first["id"]+" "+str(first["revision"]))
        self.assertEqual(self.advice.capture(event), (True, False))
        self.assertEqual(self.advice.store.get(OWNER, first["id"])["advice"], "")
        self.bot.ctx.llm.generate.assert_not_awaited()

    async def test_archive_search_retains_source_and_audience_restrictions(self):
        first = self.seed_advice()
        for context in (self.context(uid=OTHER, sid="other"), self.context(group=GROUP, sid="group")):
            self.assertFalse((await self.bot.advice("search", topic=first["topic"], **context))["success"])
        context = self.context(mid="owner")
        self.bot._tool_store.retract_observation("cap", "album:archive")
        result = await self.bot.advice("search", topic=first["topic"], **context)
        self.assertTrue(result["success"])
        self.assertNotIn(first["advice"], str(result))
        self.assertEqual(self.advice.records(first["topic"]), [])
        self.assertFalse((await self.bot.advice("search", offset=-1, **context))["success"])

    async def test_repair_archive_hint_is_related_and_search_forget_survive_hot_disable(self):
        first = self.seed_repair()
        context = self.context(first["old_quote"])
        hint = self.repair.hint(context)
        self.assertIn(first["correction_quote"], hint)
        self.assertNotIn(first["correction_quote"], self.repair.hint(self.context("今晚吃什么", "other-topic")))
        self.bot.config.repair.archive_enabled = False
        context = self.context(first["old_quote"], "old-topic")
        self.assertNotIn(first["correction_quote"], self.repair.hint(context))
        result = await self.bot.repair("search", query=first["id"], **context)
        self.assertEqual(result["data"][0]["old_quote"], first["old_quote"])
        self.assertEqual(result["data"][0]["memory_tier"], "archive")
        self.assertEqual((await self.bot.repair("list", **context))["next_offset"], 20)
        self.assertFalse((await self.bot.repair("search", offset=-1, **context))["success"])
        context = self.context("/修复 忘记 "+first["id"]+" 1", "forget")
        result = await self.bot.repair("search", query=first["id"], **context)
        self.assertEqual((result["data"][0]["memory_tier"], result["data"][0]["old_quote"]), ("tombstone", ""))
        self.bot.ctx.llm.generate.assert_not_awaited()

    async def test_repair_archive_never_crosses_owner_private_boundary(self):
        first = self.seed_repair()
        for context in (self.context(first["old_quote"], uid=OTHER, sid="other"),
                        self.context(first["old_quote"], group=GROUP, sid="group")):
            self.assertEqual(self.repair.hint(context), "")
            self.assertFalse((await self.bot.repair("search", query=first["id"], **context))["success"])
