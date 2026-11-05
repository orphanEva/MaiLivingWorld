"""Actual runtime model calls respect the shared foreground reserve on SQLite."""
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from MaiLivingWorld.tests.test_runtime import FakeContext, plugin
from MaiLivingWorld.tests.test_mcp_plugin import OWNER, OTHER, GROUP, message
from MaiLivingWorld.tests.test_companion_runtime import NovelMCP, NOTE
from MaiLivingWorld.growth_model import empty_growth
from MaiLivingWorld.store import Store


RAW = "之前在私聊讨论过缓存技术，计划下周继续实验"
GENERAL = "缓存可以减少相同输入的重复计算"


def answer(value):
    return {"response": json.dumps(value, ensure_ascii=False)}


class RuntimeReplyBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        b = self.bot = plugin.MaiLivingWorld()
        b.ctx = FakeContext()
        b.ctx.paths = SimpleNamespace(data_dir=Path(self.temp.name))
        b.config = plugin.Config()
        b.config.owner.enabled = b.config.unified_mind.enabled = True
        b.config.owner.qq = OWNER
        b.config.unified_mind.mode = "active"
        b.config.unified_mind.min_interval_seconds = 0
        b.config.brain_memory.enabled = True
        b.config.brain_memory.private_users = [OTHER]
        b.config.brain_memory.group_ids = [GROUP]
        b._store = Store(Path(self.temp.name)/"living_world.sqlite3")
        b._ensure_unified()
        self.budget(3, 2)
        b.ctx.llm.generate = AsyncMock()
        self.record = {"id": "brain:fact:F1", "source": "brain_fact", "kind": "fact",
            "scope": "private:"+OWNER, "subject_id": OWNER, "text": RAW,
            "sensitivity": "personal", "evidence_status": "active", "timestamp": 1000.0}

    async def asyncTearDown(self):
        await self.bot.on_unload()
        self.temp.cleanup()

    def budget(self, total, reserved):
        self.bot.config.unified_mind.max_daily_model_calls = total
        self.bot.config.unified_mind.reserved_reply_calls = reserved

    async def foreground_reply(self, mid="reply"):
        b = self.bot
        await b.capture_unified(message=message(mid=mid, text="继续聊聊学习"))
        b.ctx.llm.generate.side_effect = None
        b.ctx.llm.generate.return_value = answer({"behavior": "patient", "decisions": []})
        before = b.ctx.llm.generate.await_count
        result = await b._unified.reply_hint({"session_id": "private-one", "reply_message_id": mid},
            b.config.unified_mind, recall=lambda _: [dict(self.record)], generate=b.ctx.llm.generate,
            task_name="utils", timezone="Asia/Shanghai")
        self.assertEqual(b.ctx.llm.generate.await_count, before+1)
        self.assertNotIn(RAW, result)
        return result

    async def test_growth_review_cannot_consume_reserved_reply_calls(self):
        b = self.bot
        b.config.growth.enabled = True
        b.config.growth.mode = "active"
        db = b._ensure_brain()
        now = time.time()
        with db.con:
            db.con.execute("INSERT INTO brain_events(id,scope,actor_id,kind,summary,importance,happened,created) "
                "VALUES (?,?,?,'experience',?,3,?,?)", ("budget-growth", "private:"+OWNER, OWNER, RAW, now, now))
        b.ctx.llm.generate.return_value = answer(empty_growth())
        await b._grow()
        self.assertEqual(b._growth_runtime.last_status, "budget_or_cooldown")
        self.assertEqual(b.ctx.llm.generate.await_count, 1)
        self.assertEqual(b._growth.state()["revision"], 0)
        await self.foreground_reply()
        await self.foreground_reply("reply-2")
        self.assertEqual(b._self_state.state()["thought_count"], 3)

    async def test_continuity_review_preserves_reply_reserve_without_committing_topic(self):
        b = self.bot
        b.config.continuity.enabled = True
        runtime = b._ensure_continuity()
        text = "我明天要考试，有点担心复习进度"
        self.assertTrue(runtime.capture(message(text=text))[1])
        async def generate(prompt, **kwargs):
            source = json.loads(prompt.rsplit("\n", 1)[1])["inputs"][0]
            return answer({"updates": [{"action": "open", "thread_id": "", "kind": "concern",
                "title": "考试", "summary": "担心复习进度", "evidence": [{"id": source["id"], "quote": source["text"]}]}]})
        b.ctx.llm.generate.side_effect = generate
        await runtime.process()
        self.assertEqual(runtime.last_status, "model_budget")
        self.assertEqual(runtime.store.threads(OWNER), [])
        self.assertEqual(b.ctx.llm.generate.await_count, 1)
        await self.foreground_reply()
        self.assertEqual(b._self_state.state()["thought_count"], 2)

    async def test_proactive_generalization_stops_at_background_ceiling_then_reply_works(self):
        b = self.bot
        b.ctx.llm.generate.return_value = answer({"behavior": "neutral", "decisions": [
            {"id": self.record["id"], "mode": "generalize", "text": GENERAL}]})
        result = await b._unified.proactive_hint({"scope": "group:"+GROUP, "speaker_id": "",
            "query": "缓存技术", "sid": "group-one"}, b.config.unified_mind,
            recall=lambda _: [dict(self.record)], generate=b.ctx.llm.generate,
            task_name="utils", timezone="Asia/Shanghai", valid=lambda: True)
        self.assertEqual(b.ctx.llm.generate.await_count, 1)
        self.assertNotIn(RAW, result)
        self.assertNotIn(GENERAL, result)
        await self.foreground_reply()
        self.assertEqual(b._self_state.state()["thought_count"], 2)

    async def open_reading(self):
        b = self.bot
        b.config.mcp.enabled = b.config.mcp_pixiv.enabled = b.config.reading.enabled = True
        b.config.reading.excerpt_chars = 100
        b._mcp_clients["pixiv"] = NovelMCP()
        await b.capture_unified(message=message(mid="reading-open"))
        result = await b._ensure_companion().read("open", "pixiv", "novel", "123", "", {"session_id": "private-one"})
        self.assertTrue(result["success"], result)
        return result["data"]["id"]

    async def test_background_read_and_review_leave_reserved_calls_for_explicit_review(self):
        b = self.bot
        self.budget(2, 2)
        rid = await self.open_reading()
        runtime = b._companion
        result = await runtime.read("next", "", "", "", rid, {}, internal=True)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["data"]["note_status"], "pending")
        self.assertEqual(runtime.store.reading(rid)["cursor"], 100)
        result = await runtime.read("review", "", "", "", rid, {}, internal=True)
        self.assertEqual(result["code"], "note_pending_review_or_budget")
        b.ctx.llm.generate.assert_not_awaited()
        self.assertEqual(b._self_state.state()["thought_count"], 0)
        await b.capture_unified(message=message(mid="reading-review"))
        b.ctx.llm.generate.side_effect = [answer(NOTE), answer({"supported": True})]
        result = await runtime.read("review", "", "", "", rid, {"session_id": "private-one"})
        self.assertTrue(result["success"], result)
        self.assertEqual(runtime.store.units(rid)[0]["note_status"], "reviewed")
        self.assertEqual(b.ctx.llm.generate.await_count, 2)
        self.assertEqual(b._self_state.state()["thought_count"], 2)

    async def test_explicit_read_uses_full_budget_even_when_all_calls_are_reserved(self):
        b = self.bot
        self.budget(2, 12)
        rid = await self.open_reading()
        await b.capture_unified(message=message(mid="reading-next"))
        b.ctx.llm.generate.side_effect = [answer(NOTE), answer({"supported": True})]
        result = await b._companion.read("next", "", "", "", rid, {"session_id": "private-one"})
        self.assertTrue(result["success"], result)
        self.assertEqual(result["data"]["note_status"], "reviewed")
        self.assertEqual(b.ctx.llm.generate.await_count, 2)

    async def test_attention_dialogue_is_foreground_with_small_total_budget(self):
        b = self.bot
        self.budget(1, 12)
        b.config.attention.enabled = True
        runtime = b._ensure_attention()
        text = "最近总被进度追着跑，我只是想说说这件事，先不用给我建议。"
        event = message(mid="dialogue", text=text)
        await b.capture_owner(message=event)
        await b.capture_unified(message=event)
        b.ctx.llm.generate.return_value = answer({"mode": "listen", "confidence": .95,
            "evidence": "我只是想说说这件事，先不用给我建议"})
        await runtime.prepare_dialogue({"session_id": "private-one", "reply_message_id": "dialogue"})
        self.assertEqual(b.ctx.llm.generate.await_count, 1)
        self.assertEqual(runtime.store.state(OWNER)["conversation"]["mode"], "listen")
        self.assertEqual(b._self_state.state()["thought_count"], 1)


if __name__ == "__main__":
    unittest.main()
