"""Offline growth integration: real temporary ledgers, SDK/model stubs, no QQ access."""
from __future__ import annotations

import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from MaiLivingWorld.tests.test_runtime import FakeContext, plugin
from MaiLivingWorld.growth_model import empty_growth
from MaiLivingWorld.growth_runtime import style_hint
from MaiLivingWorld.store import Store
from MaiLivingWorld.explore import Story


OWNER = "12345678"
OTHER = "87654321"
GROUP = "22222222"
PRIVATE = "private:" + OWNER
PRIVATE_TEXT = "曾经在私聊讨论工作压力，后来耐心倾听让交流更顺畅"
LEARNED_TEXT = "耐心倾听有助于厘清问题而非急于下结论"


def model_response(value):
    return {"content": json.dumps(value, ensure_ascii=False)}


def proposal(ident, *, interest=True, question=True):
    data = empty_growth()
    if interest:
        data["interests"] = [{"topic": "交流方式", "delta": 0.08, "evidence_ids": [ident]}]
    data["knowledge"] = [{"topic": "交流方式", "statement": LEARNED_TEXT,
                           "confidence": 0.7, "evidence_ids": [ident]}]
    if question:
        data["questions"] = [{"topic": "倾听方法", "question": "怎样判断对方更需要倾听还是建议",
                               "status": "open", "evidence_ids": [ident]}]
    return data


def review(data):
    return {"approved": {**{name: list(range(len(data[name]))) for name in
                            ("interests", "beliefs", "knowledge", "questions", "traits")},
                          "mood": bool(data["mood"])}}


class GrowthRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bot = self.make_bot()

    def make_bot(self):
        bot = plugin.MaiLivingWorld()
        bot.ctx = FakeContext()
        bot.ctx.paths = SimpleNamespace(data_dir=Path(self.temp.name))
        bot.config = plugin.Config()
        bot.config.owner.enabled = True
        bot.config.owner.qq = OWNER
        bot.config.brain_memory.enabled = True
        bot.config.brain_memory.group_ids = [GROUP]
        bot.config.unified_mind.enabled = True
        bot.config.unified_mind.mode = "active"
        bot.config.unified_mind.min_interval_seconds = 0
        bot.config.unified_mind.max_daily_model_calls = 100
        bot.config.growth.enabled = True
        bot.config.growth.mode = "active"
        bot.config.growth.interval_seconds = 60
        return bot

    async def asyncTearDown(self):
        await self.bot.on_unload()
        self.temp.cleanup()

    def event(self, identity="growth-event-one", text=PRIVATE_TEXT, *, scope=PRIVATE):
        db = self.bot._ensure_brain()
        stamp = time.time()
        with db.con:
            db.con.execute("""INSERT INTO brain_events
                (id,scope,actor_id,kind,summary,importance,happened,created)
                VALUES (?,?,?,'experience',?,3,?,?)""", (identity,scope,OWNER,text,stamp,stamp))
        return "brain:event:" + identity

    def model_for(self, data):
        self.bot.ctx.llm.generate.side_effect = [model_response(data), model_response(review(data))]

    async def learn(self, ident=None):
        ident = ident or self.event()
        data = proposal(ident)
        self.model_for(data)
        await self.bot._grow()
        return data

    async def test_two_stage_growth_and_profile_survive_restart(self):
        await self.learn()
        state = await self.bot.growth_profile()
        self.assertGreater(state["revision"], 0)
        self.assertEqual(state["interests"][0]["topic"], "交流方式")
        self.assertAlmostEqual(state["interests"][0]["weight"], 0.08)
        self.assertEqual(state["knowledge"][0]["text"], LEARNED_TEXT)
        self.assertEqual(state["questions"][0]["status"], "open")
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 2)
        self.assertEqual(self.bot._growth_runtime.last_status, "learned")
        prompts = [call.args[0] for call in self.bot.ctx.llm.generate.await_args_list]
        self.assertIn("UNTRUSTED_GROWTH_INPUT", prompts[0])
        self.assertIn("UNTRUSTED_GROWTH_REVIEW", prompts[1])
        before = copy.deepcopy(state)
        identity = self.bot._self_state.state()["agent_id"]
        await self.bot.on_unload()
        self.bot = self.make_bot()
        after = await self.bot.growth_profile()
        for key in ("revision", "interests", "beliefs", "knowledge", "questions"):
            self.assertEqual(after[key], before[key])
        self.assertEqual(self.bot._self_state.state()["agent_id"], identity)
        self.assertEqual(self.bot._self_state.state()["thought_count"], 2)
        self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()

    async def test_old_private_conclusions_require_their_roots_in_new_learning_input(self):
        await self.learn()
        self.bot._store = Store(Path(self.temp.name)/"living_world.sqlite3")
        self.bot._store.save_stories([Story("new-public", "公开资讯", "天文学研究方法",
            "https://example.com/research", "比较观测方法有助于理解天文现象", time.time())])
        public = self.bot._resolve_roots(["story:new-public"])["story:new-public"]
        self.bot.ctx.llm.generate.reset_mock()
        self.model_for(empty_growth())
        with patch.object(self.bot, "_growth_candidates", return_value=[public]), patch(
                "MaiLivingWorld.growth_runtime.time.time", return_value=time.time()+61):
            await self.bot._grow()
        prompt = self.bot.ctx.llm.generate.await_args_list[0].args[0]
        self.assertIn("天文学研究方法", prompt)
        self.assertNotIn(PRIVATE_TEXT, prompt)
        self.assertNotIn(LEARNED_TEXT, prompt)
        self.assertEqual(self.bot._growth.state()["knowledge"][0]["text"], LEARNED_TEXT)

    async def test_full_interest_finishes_each_review_without_starving_unrelated_knowledge(self):
        from MaiLivingWorld.growth_store import MAX_INTEREST_SOURCES
        from MaiLivingWorld.tests.test_growth_store import fill_interest_sources
        self.bot._ensure_growth()
        ids = [self.event("seed-"+str(i), "第"+str(i)+"次交流的观察") for i in range(MAX_INTEREST_SOURCES)]
        sources = []
        for offset in range(0, len(ids), 128):
            sources.extend(self.bot._resolve_roots(ids[offset:offset+128]).values())
        old = fill_interest_sources(self.bot._growth, sources, "交流方式")
        before = self.bot._growth.state()["revision"]
        for index, with_knowledge in enumerate((False, True)):
            repeat = self.event("repeat-"+str(index), "一次新的交流观察")
            sibling = self.event("sibling-"+str(index), "新的倾听材料")
            rows = list(self.bot._resolve_roots([repeat, sibling]).values())
            self.bot._growth.enqueue(rows)
            data = empty_growth()
            data["interests"] = [{"topic": "交流方式", "delta": .01, "evidence_ids": [repeat]}]
            if with_knowledge:
                data["knowledge"] = [{"topic": "交谈的节奏", "statement": LEARNED_TEXT,
                                      "confidence": .7, "evidence_ids": [sibling]}]
            self.model_for(data)
            await self.bot._grow()
            store = self.bot._growth
            runtime = self.bot._growth_runtime
            self.assertEqual(runtime.last_status, "learned" if with_knowledge else "interest_source_capacity")
            self.assertEqual(runtime.last_counts["skipped_interest_overflow"], 1)
            self.assertEqual(runtime.last_counts["changed"], with_knowledge)
            self.assertEqual(store.state()["revision"], before+int(with_knowledge))
            self.assertEqual(len(store.history()), before+int(with_knowledge))
            self.assertEqual(store.state()["interests"], [old])
            self.assertEqual(store.status()["consumed"], MAX_INTEREST_SOURCES)
            self.assertEqual(store.status()["jobs"], {"pending": 0, "running": 0, "done": before+1+index, "failed": 0})
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 4)
        self.assertEqual(self.bot._growth.state()["knowledge"][0]["text"], LEARNED_TEXT)
        self.assertEqual([ref["id"] for ref in self.bot._growth.history()[0]["sources"]], [sibling])

    async def test_interest_changes_after_twenty_four_sources_and_keeps_learning_after_restart(self):
        from MaiLivingWorld.tests.test_growth_store import fill_interest_sources
        self.bot._ensure_growth()
        ids = [self.event("earlier-"+str(i), "此前第"+str(i)+"次交流的独立观察") for i in range(24)]
        original = fill_interest_sources(self.bot._growth, list(self.bot._resolve_roots(ids).values()), "交流方式")
        previous = original
        for index, delta in enumerate((-.02, .03)):
            source = self.event("later-"+str(index), "后来关于交流方式的一次新观察")
            rows = list(self.bot._resolve_roots([source]).values())
            self.bot._growth.enqueue(rows)
            data = empty_growth()
            data["interests"] = [{"topic": "交流方式", "delta": delta, "evidence_ids": [source]}]
            if index:
                data["knowledge"] = [{"topic": "交谈节奏", "statement": LEARNED_TEXT,
                                      "confidence": .7, "evidence_ids": [source]}]
            self.model_for(data)
            await self.bot._grow()
            self.assertEqual(self.bot._growth_runtime.last_status, "learned")
            self.assertNotIn("skipped_interest_overflow", self.bot._growth_runtime.last_counts)
            current = self.bot._growth.state()["interests"][0]
            self.assertAlmostEqual(current["weight"], previous["weight"]+delta)
            self.assertEqual(len(current["sources"]), 25+index)
            self.assertTrue(all(ref in current["sources"] for ref in original["sources"]))
            prompt = self.bot.ctx.llm.generate.await_args_list[0].args[0]
            self.assertIn(rows[0]["text"], prompt)
            self.assertNotIn("此前第", prompt)
            await self.bot.on_unload()
            self.bot = self.make_bot()
            self.bot._ensure_growth()
            self.assertEqual(self.bot._growth.state()["interests"], [current])
            previous = current
        self.assertEqual(self.bot._growth.status()["consumed"], 26)
        self.assertEqual(self.bot._growth.state()["knowledge"][0]["text"], LEARNED_TEXT)

    async def test_observe_does_not_change_profile_or_consume_active_evidence(self):
        ident = self.event()
        self.bot.config.growth.mode = "observe"
        data = proposal(ident)
        self.model_for(data)
        await self.bot._grow()
        state = self.bot._growth.state()
        self.assertEqual(state["revision"], 0)
        self.assertEqual(state["interests"], [])
        self.assertEqual(state["knowledge"], [])
        self.assertEqual(self.bot._growth.status()["consumed"], 0)
        self.assertEqual(self.bot._growth_runtime.last_status, "observed")
        self.bot.config.growth.mode = "active"
        await self.bot.on_config_update("growth", {}, "test")
        self.model_for(data)
        later = time.time() + 61
        with patch("MaiLivingWorld.growth_runtime.time.time", return_value=later):
            await self.bot._grow()
        self.assertGreater(self.bot._growth.state()["revision"], 0)
        self.assertEqual(self.bot._growth.state()["knowledge"][0]["text"], LEARNED_TEXT)

    async def test_budget_zero_defers_without_failure_attempt_or_consumption(self):
        self.event()
        self.bot.config.unified_mind.max_daily_model_calls = 0
        self.bot._ensure_growth()
        for offset in (0, 61, 122, 183):
            with patch("MaiLivingWorld.growth_runtime.time.time", return_value=time.time()+offset):
                await self.bot._grow()
        status = self.bot._growth.status()
        self.assertEqual(status["revision"], 0)
        self.assertEqual(status["consumed"], 0)
        self.assertEqual(status["jobs"].get("failed", 0), 0)
        self.assertEqual(self.bot._self_state.state()["thought_count"], 0)
        self.bot.ctx.llm.generate.assert_not_awaited()
        # Restore budget: a deferred job still has its retry allowance.
        self.bot.config.unified_mind.max_daily_model_calls = 100
        self.model_for(proposal("brain:event:growth-event-one"))
        with patch("MaiLivingWorld.growth_runtime.time.time", return_value=time.time()+400):
            await self.bot._grow()
        self.assertEqual(self.bot._growth_runtime.last_status, "learned")

    async def test_model_failures_have_bounded_retries_and_do_not_log_source(self):
        self.event()
        self.bot.ctx.llm.generate.side_effect = RuntimeError("provider echoed " + PRIVATE_TEXT)
        start = time.time()
        for offset in (0, 61, 182, 500):
            with patch("MaiLivingWorld.growth_runtime.time.time", return_value=start+offset):
                await self.bot._grow()
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 3)
        self.assertEqual(self.bot._growth.status()["jobs"].get("failed"), 1)
        self.assertEqual(self.bot._growth.state()["revision"], 0)
        self.assertEqual(self.bot._growth.status()["consumed"], 0)
        self.assertNotIn(PRIVATE_TEXT, json.dumps(self.bot._growth.status(), ensure_ascii=False))

    async def test_expired_lease_is_recovered_after_restart(self):
        ident = self.event()
        self.bot._ensure_growth()
        start = time.time()
        self.bot._growth.enqueue(self.bot._growth_candidates(), mode="active", now=start)
        leased = self.bot._growth.claim(now=start, lease_seconds=120, mode="active")
        self.assertIsNotNone(leased)
        await self.bot.on_unload()
        self.bot = self.make_bot()
        self.model_for(proposal(ident))
        with patch("MaiLivingWorld.growth_runtime.time.time", return_value=start+121):
            await self.bot._grow()
        self.assertEqual(self.bot._growth_runtime.last_status, "learned")
        self.assertEqual(self.bot._growth.status()["jobs"].get("done"), 1)
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 2)

    async def test_owner_change_during_first_request_prevents_review_and_commit(self):
        ident = self.event()
        data = proposal(ident)
        async def change_owner(*args, **kwargs):
            self.bot.config.owner.qq = OTHER
            await self.bot.on_config_update("owner", {}, "test")
            self.bot._ensure_unified()
            return model_response(data)
        self.bot.ctx.llm.generate.side_effect = change_owner
        await self.bot._grow()
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 1)
        self.assertEqual(self.bot._growth_runtime.last_status, "context_changed")
        self.assertEqual(self.bot._growth.state()["revision"], 0)
        self.assertEqual(self.bot._growth.status()["consumed"], 0)

    async def test_disable_mode_change_and_reset_during_review_do_not_commit(self):
        ident = self.event()
        data = proposal(ident)
        start = time.time()
        for index, change in enumerate(("disable", "mode", "reset")):
            with self.subTest(change=change):
                self.bot.config.growth.enabled = True
                self.bot.config.growth.mode = "active"
                self.bot._ensure_growth()
                count = 0
                async def change_during_review(*args, **kwargs):
                    nonlocal count
                    count += 1
                    if count == 1:
                        return model_response(data)
                    if change == "disable":
                        self.bot.config.growth.enabled = False
                    elif change == "mode":
                        self.bot.config.growth.mode = "observe"
                    else:
                        self.bot._growth_runtime.reset()
                    return model_response(review(data))
                self.bot.ctx.llm.generate.side_effect = change_during_review
                with patch("MaiLivingWorld.growth_runtime.time.time", return_value=start+index*200):
                    await self.bot._grow()
                self.assertEqual(count, 2)
                self.assertEqual(self.bot._growth.state()["revision"], 0)
                self.assertEqual(self.bot._growth_runtime.last_status, "context_changed")
        self.assertEqual(self.bot._growth.status()["consumed"], 0)

    async def test_source_deletion_or_edit_during_review_prevents_commit(self):
        ident = self.event()
        data = proposal(ident)
        start = time.time()
        for index, mutation in enumerate(("edit", "delete")):
            with self.subTest(mutation=mutation):
                if mutation == "delete":
                    with self.bot._brain.con:
                        self.bot._brain.con.execute("UPDATE brain_events SET summary=? WHERE id=?",
                                                     (PRIVATE_TEXT, "growth-event-one"))
                count = 0
                async def mutate(*args, **kwargs):
                    nonlocal count
                    count += 1
                    if count == 1:
                        return model_response(data)
                    with self.bot._brain.con:
                        if mutation == "edit":
                            self.bot._brain.con.execute("UPDATE brain_events SET summary='来源已被修正' WHERE id=?",
                                                         ("growth-event-one",))
                        else:
                            self.bot._brain.con.execute("DELETE FROM brain_events WHERE id=?", ("growth-event-one",))
                    return model_response(review(data))
                self.bot.ctx.llm.generate.side_effect = mutate
                with patch("MaiLivingWorld.growth_runtime.time.time", return_value=start+index*200):
                    await self.bot._grow()
                self.assertEqual(count, 2)
                self.assertEqual(self.bot._growth.state()["revision"], 0)
                self.assertEqual(self.bot._growth.status()["consumed"], 0)
                self.assertEqual(self.bot._growth_runtime.last_status, "learning_failed")

    async def test_learning_candidates_exclude_derived_conclusions(self):
        await self.learn()
        derived = self.bot._derived_memories("交流")
        self.assertTrue(derived)
        candidates = self.bot._growth_candidates()
        self.assertTrue(candidates)
        self.assertTrue(all(not record["source"].startswith("growth") for record in candidates))
        prompts = [call.args[0] for call in self.bot.ctx.llm.generate.await_args_list]
        self.assertNotIn('"source": "growth', prompts[0])

    async def test_private_derived_knowledge_not_disclosed_to_group(self):
        await self.learn()
        derived = self.bot._derived_memories("交流")
        self.assertTrue(derived)
        self.assertTrue(all(item["sensitivity"] != "public" for item in derived))
        message = {"platform": "qq", "session_id": "group-one", "message_id": "new-question",
                   "account_id": "99999999", "processed_plain_text": "怎样改善交流方式",
                   "message_info": {"user_info": {"user_id": OWNER},
                                    "group_info": {"group_id": GROUP}}}
        await self.bot.capture_unified(message=message)
        self.bot.ctx.llm.generate.side_effect = None
        self.bot.ctx.llm.generate.return_value = model_response({"behavior": "patient", "decisions":
            [{"id": item["id"], "mode": "say"} for item in self.bot._collect_unified("交流")]})
        result = await self.bot.brain_reply_hint(session_id="group-one", reply_message_id="new-question",
            messages=[{"role": "system", "content": "宿主提示"}])
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(PRIVATE_TEXT, rendered)
        self.assertNotIn(LEARNED_TEXT, rendered)
        self.assertIn("耐心", rendered)

    async def test_public_interest_affects_story_choice_without_extra_external_action(self):
        self.bot._store = Store(Path(self.temp.name)/"living_world.sqlite3")
        stamp = time.time()
        self.bot._store.save_stories([
            Story("research", "公开资讯", "天文学研究方法", "https://example.com/astronomy", "比较观测方法有助于理解天文现象", stamp),
            Story("other", "公开资讯", "一般新闻", "https://example.com/news", "普通资讯摘要", stamp+1),
        ])
        data = empty_growth()
        data["interests"] = [{"topic": "天文学", "delta": 0.1, "evidence_ids": ["story:research"]}]
        self.model_for(data)
        await self.bot._grow()
        public = [item for item in self.bot._derived_memories() if item["kind"] == "interest"]
        self.assertTrue(public)
        self.assertEqual(public[0]["sensitivity"], "public")
        self.bot.config.explorer.enabled = True
        self.bot.config.explorer.interests = []
        self.bot.config.brain.use_llm_selection = False
        chosen = await self.bot._choose_story("qq:private:" + OWNER)
        self.assertEqual(chosen["key"], "research")
        self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()

    async def test_rollback_api_requires_confirmation_revision_and_keeps_identity_dedup(self):
        await self.learn()
        before = self.bot._growth.state()
        revision = before["revision"]
        identity = self.bot._self_state.state()
        consumed = self.bot._growth.status()["consumed"]
        self.assertFalse((await self.bot.growth_rollback(revision))["ok"])
        self.assertFalse((await self.bot.growth_rollback(revision+10, "ROLLBACK_GROWTH"))["ok"])
        self.assertEqual(self.bot._growth.state()["revision"], revision)
        result = await self.bot.growth_rollback(revision, "ROLLBACK_GROWTH")
        self.assertTrue(result["ok"])
        self.assertGreater(result["revision"], revision)
        self.assertEqual(self.bot._growth.state()["knowledge"], [])
        self.assertEqual(self.bot._growth.status()["consumed"], consumed)
        self.assertEqual(self.bot._self_state.state()["owner_id"], identity["owner_id"])
        self.assertEqual(self.bot._self_state.state()["agent_id"], identity["agent_id"])

    async def test_rollback_during_review_invalidates_pending_growth(self):
        await self.learn()
        first_revision = self.bot._growth.state()["revision"]
        ident = self.event("second-growth-event", "后来一次交流进一步支持耐心倾听")
        data = proposal(ident, interest=False, question=False)
        count = 0
        async def rollback_during_review(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                return model_response(data)
            result = await self.bot.growth_rollback(first_revision, "ROLLBACK_GROWTH")
            self.assertTrue(result["ok"])
            return model_response(review(data))
        self.bot.ctx.llm.generate.side_effect = rollback_during_review
        with patch("MaiLivingWorld.growth_runtime.time.time", return_value=time.time()+61):
            await self.bot._grow()
        self.assertEqual(self.bot._growth_runtime.last_status, "context_changed")
        self.assertEqual(self.bot._growth.state()["knowledge"], [])
        self.assertEqual(self.bot._growth.state()["revision"], first_revision+1)

    def test_style_hint_uses_only_fixed_phrases_never_private_source_text(self):
        state = {"traits": {"patience": 0.6, "curiosity": 0.7},
                 "mood": {"valence": -0.2, "energy": 0.3},
                 "knowledge": [{"text": PRIVATE_TEXT}], "reason": PRIVATE_TEXT,
                 "sources": [{"text": PRIVATE_TEXT}], "owner_id": OWNER}
        hint = style_hint(state)
        self.assertIn("耐心回应", hint)
        self.assertIn("保持好奇", hint)
        self.assertIn("简短回应", hint)
        self.assertNotIn(PRIVATE_TEXT, hint)
        self.assertNotIn(OWNER, hint)


if __name__ == "__main__":
    unittest.main()
