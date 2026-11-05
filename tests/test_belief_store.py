"""Gradual opinions keep their original evidence across revision and restart."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from MaiLivingWorld.growth_model import empty_growth
from MaiLivingWorld.growth_store import GrowthStore


OLD = "长时间讨论时先倾听通常更合适"
NEW = "明确求建议时可以先给简短建议再倾听"
TOPIC = "交流方式"


def record(ident, text=None, *, origin=None, scope="public"):
    return {"id": "brain:event:"+ident, "source": "brain_event", "kind": "experience",
            "text": text or ident+"交流方式的实际不同体验提供了新的观察",
            "scope": scope, "subject_id": "12345678" if scope != "public" else "",
            "sensitivity": "personal" if scope != "public" else "public",
            "evidence_status": "recorded_event", "timestamp": 100.,
            "origin_key": origin or "observation:"+ident}


def proposal(rows, *, old=None, relation="new", statement=OLD, confidence=.95, topic=TOPIC):
    result = empty_growth()
    result["beliefs"] = [{"topic": topic, "statement": statement, "confidence": confidence,
        "evidence_ids": [r["id"] for r in rows], "relation": relation,
        "previous_key": old["key"] if old else "", "previous_revision": old["revision"] if old else 0,
        "quotes": [{"source_id": r["id"], "quote": r["text"]} for r in rows]}]
    return result


class BeliefChecks:
    """The same transactional contracts run against SQLite and disposable MySQL."""

    def submit(self, data, rows, *, now=100):
        jid = self.db.enqueue(rows, now=now)
        job = self.db.claim(now=now)
        self.assertEqual(job["id"], jid)
        return self.db.complete(jid, job["token"], data, rows, now=now+1,
            expected_revision=self.db.state(now=now)["revision"], review_beliefs=True)

    def held(self):
        return self.db.state(now=1000)["beliefs"][0]

    def seed(self):
        a = record("A", "交流方式：上次长时间倾听帮助对方整理了思路", scope="private:12345678")
        self.submit(proposal([a]), [a])
        return a, self.held()

    def test_counterevidence_waits_then_changes_gradually_with_all_roots(self):
        a, old = self.seed()
        self.assertEqual(old["confidence"], .6)
        b = record("B", "交流方式：明确求建议后给简短步骤帮助对方开始行动")
        self.submit(proposal([b], old=old, relation="challenge", statement=NEW), [a, b], now=110)
        pending = self.held()
        self.assertEqual(pending["text"], OLD)
        self.assertEqual(pending["assessment"]["status"], "contested")
        self.assertEqual(pending["assessment"]["candidate_text"], NEW)
        self.assertEqual(pending["assessment"]["last_previous_text"], "")
        self.assertEqual(pending["confidence"], .5)
        self.db.close()
        self.db = self.open_store()
        self.assertEqual(self.held(), pending)
        c = record("C", "交流方式：另一场求助中对方明确要建议，先说明一个选择很有帮助")
        self.submit(proposal([c], old=pending, relation="challenge", statement=NEW), [a, b, c], now=120)
        revised = self.held()
        self.assertEqual(revised["text"], NEW)
        self.assertEqual(revised["confidence"], .6)
        self.assertEqual(revised["assessment"]["status"], "considered")
        self.assertEqual(revised["assessment"]["last_previous_text"], OLD)
        self.assertEqual({r["id"] for r in revised["sources"]}, {a["id"], b["id"], c["id"]})
        restored = self.db.rollback(self.db.state()["revision"], now=130)
        self.assertEqual(restored["beliefs"][0], pending)
        self.assertEqual(self.db.history()[0]["kind"], "rollback")

    def test_two_new_sources_in_one_batch_still_require_a_later_observation(self):
        a, old = self.seed()
        b, c, d = record("B"), record("C"), record("D")
        self.submit(proposal([b, c], old=old, relation="refine", statement=NEW), [a, b, c], now=110)
        pending = self.held()
        self.assertEqual(pending["text"], OLD)
        self.assertEqual(len(pending["assessment"]["candidate_origins"]), 2)
        self.submit(proposal([d], old=pending, relation="refine", statement=NEW), [a, b, c, d], now=120)
        self.assertEqual(self.held()["text"], NEW)

    def test_origin_and_content_aliases_never_strengthen_or_advance_candidate(self):
        a, old = self.seed()
        b = record("B")
        self.submit(proposal([b], old=old, relation="challenge", statement=NEW), [a, b], now=110)
        pending = self.held()
        aliases = [record("alias-origin", "同一事件拆成另一条记录也不是另一次经历", origin=b["origin_key"]),
                   record("alias-content", b["text"]+"！")]
        for index, alias in enumerate(aliases):
            result = self.submit(proposal([alias], old=pending, relation="challenge", statement=NEW),
                                 [a, b, alias], now=120+index*10)
            self.assertFalse(result["changed"])
            self.assertEqual(self.held(), pending)
        jid = self.db.enqueue([a, b], now=150)
        self.assertIsNone(self.db.claim(now=150))
        self.assertTrue(jid)
        self.assertEqual(self.held(), pending)

    def test_reinforcement_changes_confidence_by_at_most_one_tenth(self):
        a, old = self.seed()
        b = record("B")
        self.submit(proposal([b], old=old, relation="reinforce"), [a, b], now=110)
        self.assertEqual(self.held()["confidence"], .7)
        self.assertEqual(self.held()["text"], OLD)
        self.assertEqual(self.held()["assessment"]["status"], "considered")
        c = record("C", a["text"])
        self.assertFalse(self.submit(proposal([c], old=self.held(), relation="reinforce"),
                                     [a, b, c], now=120)["changed"])
        self.assertEqual(self.held()["confidence"], .7)

    def test_reinforcement_preserves_unresolved_refinement(self):
        a, old = self.seed()
        b, c = record("B"), record("C")
        self.submit(proposal([b], old=old, relation="refine", statement=NEW), [a, b], now=110)
        pending = self.held()
        self.submit(proposal([c], old=pending, relation="reinforce"), [a, b, c], now=120)
        self.assertEqual(self.held()["assessment"], pending["assessment"])
        self.assertEqual(self.held()["confidence"], pending["confidence"])

    def test_same_topic_cannot_bypass_previous_key_even_when_hidden(self):
        a, old = self.seed()
        b = record("B")
        jid = self.db.enqueue([b], now=110)
        job = self.db.claim(now=110)
        data = proposal([b], statement=NEW, topic="交流 方式！")
        with self.assertRaisesRegex(ValueError, "invalid_belief_review"):
            self.db.complete(jid, job["token"], data, [b], now=111, review_beliefs=True)
        self.assertEqual(self.held(), old)

    def test_old_roots_must_be_queued_and_current_not_added_after_selection(self):
        a, old = self.seed()
        b = record("B")
        jid = self.db.enqueue([b], now=110)
        job = self.db.claim(now=110)
        with self.assertRaisesRegex(ValueError, "belief_context_missing"):
            self.db.complete(jid, job["token"], proposal([b], old=old, relation="challenge", statement=NEW),
                             [a, b], now=111, review_beliefs=True)
        self.assertEqual(self.held(), old)

    def test_changed_original_source_cannot_support_a_revision(self):
        a, old = self.seed()
        b = record("B")
        altered = {**a, "text": "原始经历后来撤回或改变"}
        jid = self.db.enqueue([altered, b], now=110)
        job = self.db.claim(now=110)
        with self.assertRaisesRegex(ValueError, "belief_context_changed"):
            self.db.complete(jid, job["token"], proposal([b], old=old, relation="challenge", statement=NEW),
                             [altered, b], now=111, review_beliefs=True)
        self.assertEqual(self.held(), old)

    def test_stale_revision_rejected_across_connections_without_partial_change(self):
        a, old = self.seed()
        b, c = record("B"), record("C")
        other = self.open_store()
        try:
            jid = other.enqueue([a, c], now=110)
            job = other.claim(now=110)
            self.submit(proposal([b], old=old, relation="reinforce"), [a, b], now=111)
            current = self.held()
            with self.assertRaisesRegex(ValueError, "revision_conflict"):
                other.complete(jid, job["token"], proposal([c], old=old, relation="reinforce"),
                               [a, c], now=113, expected_revision=1, review_beliefs=True)
            with self.assertRaisesRegex(ValueError, "invalid_belief_review"):
                other.complete(jid, job["token"], proposal([c], old=old, relation="reinforce"),
                               [a, c], now=114, review_beliefs=True)
            self.assertEqual(self.held(), current)
        finally:
            other.close()

    def test_invalid_literal_quote_and_relation_are_atomic(self):
        a = record("A")
        jid = self.db.enqueue([a], now=100)
        job = self.db.claim(now=100)
        invalid = []
        for field, value in (("relation", []), ("previous_revision", True),
                             ("quotes", [{"source_id": a["id"], "quote": "根本没有出现的原话"}]),
                             ("statement", "过"*181)):
            data = proposal([a])
            data["beliefs"][0][field] = value
            invalid.append(data)
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.db.complete(jid, job["token"], data, [a], now=101, review_beliefs=True)
            self.assertEqual(self.db.state()["revision"], 0)
            self.assertEqual(self.db.history(), [])

    def test_legacy_belief_gets_assessment_only_after_reviewed_original_evidence(self):
        a, b = record("A"), record("B")
        data = proposal([a])
        for field in ("relation", "previous_key", "previous_revision", "quotes"):
            data["beliefs"][0].pop(field)
        jid = self.db.enqueue([a], now=100)
        job = self.db.claim(now=100)
        self.db.complete(jid, job["token"], data, [a], now=101)
        old = self.held()
        self.assertNotIn("assessment", old)
        self.submit(proposal([b], old=old, relation="challenge", statement=NEW), [a, b], now=110)
        self.assertEqual(self.held()["text"], OLD)
        self.assertEqual(self.held()["assessment"]["status"], "contested")
        self.assertAlmostEqual(old["confidence"]-self.held()["confidence"], .1)

    def test_long_legacy_view_is_preserved_in_history_without_partial_expression(self):
        a, b, c = record("A"), record("B"), record("C")
        long_text = "记录"*95+"，但对方明确求建议时不适用"
        data = proposal([a], statement=long_text)
        for field in ("relation", "previous_key", "previous_revision", "quotes"):
            data["beliefs"][0].pop(field)
        jid = self.db.enqueue([a], now=100)
        job = self.db.claim(now=100)
        self.db.complete(jid, job["token"], data, [a], now=101)
        self.submit(proposal([b], old=self.held(), relation="refine", statement=NEW), [a, b], now=110)
        self.submit(proposal([c], old=self.held(), relation="refine", statement=NEW), [a, b, c], now=120)
        self.assertEqual(self.held()["text"], NEW)
        self.assertEqual(self.held()["assessment"]["last_previous_text"], "")
        restored = self.db.rollback(self.db.state()["revision"], now=130)
        self.assertEqual(restored["beliefs"][0]["text"], long_text)


class BeliefStoreTests(BeliefChecks, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"belief-growth.sqlite3"
        self.db = self.open_store()

    def open_store(self):
        return GrowthStore(self.path)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()
