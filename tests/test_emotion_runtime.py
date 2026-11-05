"""The existing two-stage growth loop owns event reactions and their budgets."""
from __future__ import annotations

import asyncio
import copy
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from MaiLivingWorld.growth_runtime import GrowthRuntime
from MaiLivingWorld.growth_store import GrowthStore
from MaiLivingWorld.tests.test_emotion_model import response, review
from MaiLivingWorld.tests.test_emotion_store import record, proposal


class Identity:
    def __init__(self):
        self.calls, self.owner = 0, "12345678"

    def state(self):
        return {"owner_id": self.owner}

    def reserve_thought(self, **kwargs):
        if self.calls >= kwargs["daily_limit"]-kwargs["reserve_for_reply"]:
            return False
        self.calls += 1
        return True


class EmotionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = GrowthStore(Path(self.temp.name)/"growth.sqlite3")
        self.identity = Identity()
        self.runtime = GrowthRuntime(self.store, self.identity)
        self.config = SimpleNamespace(enabled=True, mode="active", interval_seconds=0,
                                      review_beliefs=False, emotional_episodes=True)
        self.budget = SimpleNamespace(max_daily_model_calls=100, reserved_reply_calls=12,
                                     min_interval_seconds=0, model_timeout_seconds=5)
        self.now = time.time()
        self.rows = [record(now=self.now)]
        self.model = AsyncMock()

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def run_tick(self, *, snapshot=None):
        with patch("time.time", return_value=self.now):
            await self.runtime.tick(self.config, self.budget,
                collect=lambda: copy.deepcopy(self.rows),
                resolve=lambda ids: {r["id"]: copy.deepcopy(r) for r in self.rows if r["id"] in ids},
                snapshot=snapshot or self.store.state, generate=self.model, allowed=lambda: True,
                task_name="test", timezone="Asia/Shanghai")

    def normal_model(self, *, selected=None):
        data = proposal(self.rows)
        self.model.side_effect = [response(data), response(review(data, selected=selected))]

    async def test_two_stage_review_creates_mixed_reactions_with_original_sources(self):
        self.normal_model()
        await self.run_tick()
        self.assertEqual(self.identity.calls, 2)
        self.assertEqual(self.model.await_count, 2)
        self.assertEqual(self.runtime.last_status, "learned")
        self.assertEqual(len(self.store.state(now=self.now)["emotions"]), 2)
        for call in self.model.await_args_list:
            self.assertIn(self.rows[0]["text"], call.args[0])

    async def test_independent_review_rejection_persists_no_reaction(self):
        self.normal_model(selected=[])
        await self.run_tick()
        self.assertEqual(self.store.state(now=self.now).get("emotions", []), [])
        self.assertEqual(self.store.status()["consumed"], 0)

    async def test_hot_disable_or_owner_change_during_first_await_defers_without_commit(self):
        async def generate(*args, **kwargs):
            self.config.emotional_episodes = False
            return response(proposal(self.rows))
        self.model.side_effect = generate
        await self.run_tick()
        self.assertEqual(self.model.await_count, 1)
        self.assertEqual(self.runtime.last_status, "context_changed")
        self.assertEqual(self.store.state(now=self.now).get("emotions", []), [])

    async def test_owner_change_during_second_review_drops_result(self):
        data = proposal(self.rows)
        calls = 0
        async def generate(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return response(data)
            self.identity.owner = "87654321"
            return response(review(data))
        self.model.side_effect = generate
        await self.run_tick()
        self.assertEqual(self.runtime.last_status, "context_changed")
        self.assertEqual(self.store.state(now=self.now).get("emotions", []), [])

    async def test_source_changed_during_review_cannot_become_new_emotional_evidence(self):
        data = proposal(self.rows)
        calls = 0
        async def generate(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return response(data)
            self.rows[0]["text"] = "修订后原来的情节其实不存在。"
            return response(review(data))
        self.model.side_effect = generate
        await self.run_tick()
        self.assertEqual(self.runtime.last_status, "learning_failed")
        self.assertEqual(self.store.state(now=self.now).get("emotions", []), [])

    async def test_second_stage_respects_shared_reply_reserve(self):
        self.budget.max_daily_model_calls = 13
        self.normal_model()
        await self.run_tick()
        self.assertEqual(self.model.await_count, 1)
        self.assertEqual(self.identity.calls, 1)
        self.assertEqual(self.runtime.last_status, "budget_or_cooldown")
        self.assertEqual(self.store.state(now=self.now).get("emotions", []), [])

    async def test_old_private_reaction_is_not_laundered_into_new_public_growth_prompt(self):
        self.normal_model()
        await self.run_tick()
        old_text = self.store.state(now=self.now)["emotions"][0]["text"]
        self.rows = [record("new", "一篇新的公共材料介绍海洋生态。", now=self.now+61, scope="public")]
        self.now += 61
        self.normal_model(selected=[])
        self.model.reset_mock()
        await self.run_tick()
        self.assertNotIn(old_text, self.model.await_args_list[0].args[0])

    async def test_cancelled_model_leaves_no_partial_emotion(self):
        self.model.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.run_tick()
        self.assertEqual(self.runtime.last_status, "interrupted")
        self.assertEqual(self.store.state(now=self.now).get("emotions", []), [])
