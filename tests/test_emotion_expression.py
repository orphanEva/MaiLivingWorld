"""Event feelings are audience-checked, source-dependent and relevant-only."""
from __future__ import annotations

import copy
from pathlib import Path
import tempfile
import unittest

from MaiLivingWorld.emotion_episodes import HALF_LIFE, TTL
from MaiLivingWorld.growth_runtime import style_hint
from MaiLivingWorld.growth_store import GrowthStore, source_fingerprint
from MaiLivingWorld.unified_memory import derived_memories
from MaiLivingWorld.unified_expression import parse_expression, expression_hint
from MaiLivingWorld.unified_runtime import _memory_version
from MaiLivingWorld.tests.test_emotion_model import response
from MaiLivingWorld.tests.test_emotion_store import record, proposal


class EmotionExpressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = GrowthStore(Path(self.temp.name)/"growth.sqlite3")
        self.row = record()
        jid = self.db.enqueue([self.row], now=100)
        claim = self.db.claim(now=100)
        self.db.complete(jid, claim["token"], proposal([self.row]), [self.row], now=101, emotional_episodes=True)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def memories(self, *, now=101, query="雪原结局", roots=None, state=None):
        return derived_memories(state or self.db.state(now=now), roots if roots is not None else {self.row["id"]: self.row},
            fingerprint=source_fingerprint, query=query)

    def plan(self, rows, scope="private:12345678", speaker="12345678", mode="say"):
        decisions = [{"id": r["id"], "mode": mode, **({"text": "阅读故事有助于理解叙事"} if mode == "generalize" else {})} for r in rows]
        return parse_expression(response({"behavior": "neutral", "decisions": decisions}), rows,
            audience_scope=scope, speaker_id=speaker, is_owner=speaker == "12345678")

    def test_permitted_private_reaction_has_own_perspective_but_no_universal_fact(self):
        rows = self.memories()
        self.assertEqual(len(rows), 2)
        plan = self.plan(rows)
        self.assertTrue(all(r["perspective"] == "self_emotion" for r in plan["memories"]))
        hint = expression_hint(plan)
        self.assertIn("暂时感受", hint)
        self.assertIn("可保留同时欣赏又失望", hint)
        self.assertNotIn("0.4", hint)
        self.assertNotIn(self.row["id"], hint)
        generalized = self.plan(rows, mode="generalize")
        self.assertEqual(generalized["pending_generalizations"], [])
        self.assertEqual(generalized["memories"], [])

    def test_private_and_confidential_causes_never_leak_through_style_or_other_audience(self):
        rows = self.memories()
        self.assertEqual(self.plan(rows, "group:22222222")["memories"], [])
        self.assertEqual(self.plan(rows, "private:87654321", "87654321")["memories"], [])
        self.assertNotIn("雪原", style_hint(self.db.state(now=101), personality_tensions=True))
        self.assertNotIn("故事", style_hint(self.db.state(now=101)))

    def test_source_removal_or_revision_or_expiry_suppresses_reaction(self):
        self.assertEqual(self.memories(roots={}), [])
        changed = {**self.row, "text": "这项记录已更正"}
        self.assertEqual(self.memories(roots={self.row["id"]: changed}), [])
        self.assertEqual(self.memories(now=101+TTL), [])

    def test_discrete_decay_does_not_change_version_during_each_model_await(self):
        first = self.memories(now=101)
        self.assertEqual(_memory_version(first), _memory_version(self.memories(now=102)))
        later = self.memories(now=101+HALF_LIFE)
        self.assertNotEqual(_memory_version(first), _memory_version(later))
        self.assertEqual([r["sources"] for r in first], [r["sources"] for r in later])

    def test_unrelated_queries_do_not_load_emotion_but_internal_snapshot_may(self):
        self.assertEqual(self.memories(query="如何安装数据库"), [])
        self.assertEqual(len(self.memories(query="")), 2)

    def test_full_legacy_collections_cannot_starve_emotional_candidates(self):
        state = self.db.state(now=101)
        source = state["emotions"][0]["sources"]
        for category, prefix in (("interests", "i"), ("beliefs", "b"), ("knowledge", "k"), ("questions", "q")):
            state[category] = [{"key": prefix+":"+f"{n:024x}", "topic": "无关内容", "revision": 1,
                "status": "open" if category == "questions" else "active", "weight": .01, "confidence": .01,
                "text": "无关的一项背景材料", "sources": source} for n in range(32)]
        rows = self.memories(state=state)
        self.assertEqual(sum(r["kind"] == "emotion" for r in rows), 2)
