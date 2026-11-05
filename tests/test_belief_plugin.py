"""Opinion revision through the actual plugin, original ledger and two model stages."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from MaiLivingWorld.tests.test_runtime import FakeContext, plugin
from MaiLivingWorld.tests.test_growth_runtime import model_response, review
from MaiLivingWorld.growth_model import empty_growth


OWNER = "12345678"
OLD = "安静环境更适合集中注意力"
NEW = "熟悉的背景音乐有时也有助于集中注意力"


class BeliefPluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = time.time()
        self.bot = self.make_bot()

    def make_bot(self):
        bot = plugin.MaiLivingWorld()
        bot.ctx = FakeContext()
        bot.ctx.paths = SimpleNamespace(data_dir=Path(self.temp.name))
        bot.config = plugin.Config()
        bot.config.owner.enabled = bot.config.brain_memory.enabled = bot.config.unified_mind.enabled = True
        bot.config.owner.qq = OWNER
        bot.config.brain_memory.group_ids = ["22222222"]
        bot.config.unified_mind.mode = bot.config.growth.mode = "active"
        bot.config.unified_mind.min_interval_seconds = 0
        bot.config.unified_mind.max_daily_model_calls = 100
        bot.config.growth.enabled = bot.config.growth.review_beliefs = True
        bot.config.growth.interval_seconds = 60
        bot._ensure_growth()
        return bot

    async def asyncTearDown(self):
        await self.bot.on_unload()
        self.temp.cleanup()

    def event(self, key, text, age=0):
        db = self.bot._ensure_brain()
        stamp = time.time()-age
        with db.con:
            db.con.execute("""INSERT INTO brain_events
                (id,scope,actor_id,kind,summary,importance,happened,created)
                VALUES (?,?,?,'experience',?,3,?,?)""",
                (key, "private:"+OWNER, OWNER, text, stamp, stamp))
        return "brain:event:"+key

    def proposal(self, ident, statement=OLD, relation="new"):
        old = self.bot._growth.state()["beliefs"]
        row = self.bot._resolve_roots([ident])[ident]
        data = empty_growth()
        data["beliefs"] = [{"topic": "专注环境", "statement": statement, "confidence": .8,
            "evidence_ids": [ident], "relation": relation,
            "previous_key": old[0]["key"] if relation != "new" else "",
            "previous_revision": old[0]["revision"] if relation != "new" else 0,
            "quotes": [{"source_id": ident, "quote": row["text"]}]}]
        return data

    async def tick(self, data):
        self.bot.ctx.llm.generate.reset_mock()
        self.bot.ctx.llm.generate.side_effect = [model_response(data), model_response(review(data))]
        await self.advance()

    async def advance(self):
        self.now += 61
        with patch("MaiLivingWorld.growth_runtime.time.time", return_value=self.now):
            await self.bot._grow()

    async def seed(self):
        first = self.event("quiet", "专注环境：那次在安静的房间里阅读更容易专心", age=1000)
        await self.tick(self.proposal(first))
        self.assertEqual(self.bot._growth_runtime.last_status, "learned")
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 2)
        return first

    async def test_old_original_returns_to_both_model_stages_then_survives_restart(self):
        first = await self.seed()
        for i in range(20):
            self.event("unrelated"+str(i), "另一个完全无关的话题记录"+str(i), age=100-i)
        second = self.event("music", "专注环境：今天听熟悉的背景音乐时也能专心学习")
        self.assertNotIn(first, {r["id"] for r in self.bot._collect_roots()})
        self.assertIn(first, {r["id"] for r in self.bot._growth_candidates()})
        await self.tick(self.proposal(second, NEW, "challenge"))
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 2)
        contested = self.bot._growth.state()["beliefs"][0]
        self.assertEqual(contested["text"], OLD)
        self.assertEqual(contested["assessment"]["status"], "contested")
        for call in self.bot.ctx.llm.generate.await_args_list:
            prompt = str(call.args)+str(call.kwargs)
            for value in (first, second, OLD, contested["key"]):
                self.assertIn(value, prompt)
        await self.bot.on_unload()
        self.bot = self.make_bot()
        self.assertEqual(self.bot._growth.state()["beliefs"][0], contested)
        third = self.event("later", "专注环境：另一天用轻缓的背景音乐阅读理解也很顺畅")
        await self.tick(self.proposal(third, NEW, "challenge"))
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 2)
        adopted = self.bot._growth.state()["beliefs"][0]
        self.assertEqual(adopted["text"], NEW)
        self.assertEqual(adopted["assessment"]["last_previous_text"], OLD)
        self.assertEqual({r["id"] for r in adopted["sources"]}, {first, second, third})
        self.assertEqual(self.bot._self_state.state()["thought_count"], 6)
        memories = self.bot._collect_unified("专注环境")
        grown = [r for r in memories if r.get("source") == "growth"]
        self.assertEqual(len(grown), 1)
        self.assertIn("调整前曾倾向", grown[0]["text"])

    async def test_changed_original_during_independent_review_rejects_commit(self):
        first = await self.seed()
        second = self.event("music", "专注环境：今天听熟悉的背景音乐时也能专心学习")
        before = self.bot._growth.state()["beliefs"]
        data = self.proposal(second, NEW, "challenge")
        calls = 0
        async def generate(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                with self.bot._brain.con:
                    self.bot._brain.con.execute("DELETE FROM brain_events WHERE id='quiet'")
                return model_response(review(data))
            return model_response(data)
        self.bot.ctx.llm.generate.side_effect = generate
        await self.advance()
        self.assertEqual(calls, 2)
        self.assertEqual(self.bot._growth.state()["beliefs"], before)
        self.assertEqual(self.bot._growth_snapshot()["beliefs"], [])
        self.assertNotIn(first, self.bot._resolve_roots([first]))

    async def test_hot_disable_during_model_call_does_not_commit_old_mode(self):
        await self.seed()
        second = self.event("music", "专注环境：今天听熟悉的背景音乐时也能专心学习")
        before = self.bot._growth.state()["beliefs"]
        data = self.proposal(second, NEW, "challenge")
        async def generate(*args, **kwargs):
            self.bot.config.growth.review_beliefs = False
            return model_response(data)
        self.bot.ctx.llm.generate.side_effect = generate
        await self.advance()
        self.assertEqual(self.bot._growth_runtime.last_status, "context_changed")
        self.assertEqual(self.bot._growth.state()["beliefs"], before)

    async def test_observe_and_exhausted_budget_never_adopt_a_candidate(self):
        ident = self.event("quiet", "专注环境：安静的阅读环境使理解更轻松")
        self.bot.config.growth.mode = "observe"
        await self.tick(self.proposal(ident))
        self.assertEqual(self.bot._growth_runtime.last_status, "observed")
        self.assertEqual(self.bot._growth.state()["beliefs"], [])
        self.bot.config.growth.mode = "active"
        self.bot.config.unified_mind.reserved_reply_calls = 0
        self.bot.config.unified_mind.max_daily_model_calls = 3  # two used by observe, only one remains
        await self.tick(self.proposal(ident))
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 1)
        self.assertEqual(self.bot._growth_runtime.last_status, "budget_or_cooldown")
        self.assertEqual(self.bot._growth.state()["beliefs"], [])


if __name__ == "__main__":
    unittest.main()
