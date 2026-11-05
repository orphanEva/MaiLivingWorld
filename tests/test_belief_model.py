"""Original quotes and prior opinions have distinct roles in the two model passes."""
from __future__ import annotations

import copy
import hashlib
import json
import unittest

from MaiLivingWorld.belief_revision import normalized, select_belief_context
from MaiLivingWorld.growth_model import build_growth_prompt, build_growth_review_prompt, parse_growth, review_growth
from MaiLivingWorld.growth_store import source_fingerprint
from MaiLivingWorld.tests.test_belief_store import OLD, NEW, TOPIC, proposal, record


def prior(rows, **extra):
    return {"key": "b:"+hashlib.sha256(normalized(TOPIC).encode()).hexdigest()[:24],
            "topic": TOPIC, "text": OLD, "confidence": .6, "status": "active", "revision": 1,
            "sources": [{"id": r["id"], "digest": source_fingerprint(r)} for r in rows], **extra}


def parse(data, rows, state):
    return parse_growth(json.dumps(data, ensure_ascii=False), rows, review_beliefs=True, state=state)


def approved(indices):
    return json.dumps({"approved": {"interests": [], "beliefs": indices, "knowledge": [], "questions": [],
                         "traits": [], "mood": False}})


class BeliefModelTests(unittest.TestCase):
    def test_both_passes_see_all_roots_and_prior_state_without_self_citation(self):
        a, b = record("A", scope="private:12345678"), record("B")
        old = prior([a])
        state = {"beliefs": [old]}
        data = proposal([b], old=old, relation="challenge", statement=NEW)
        valid = parse(data, [a, b], state)
        self.assertIsNotNone(valid)
        first = build_growth_prompt([a, b], state, review_beliefs=True)
        second = build_growth_review_prompt(valid, [a, b], state=state, review_beliefs=True)
        for prompt in (first, second):
            self.assertIn(a["text"], prompt)
            self.assertIn(b["text"], prompt)
            self.assertIn(old["key"], prompt)
            self.assertIn(OLD, prompt)
            self.assertIn("private:12345678", prompt)
        reviewed = review_growth(approved([0]), valid, review_beliefs=True)
        self.assertEqual(reviewed, valid)
        self.assertEqual(review_growth(approved([]), valid, review_beliefs=True)["beliefs"], [])
        self.assertIsNone(review_growth(approved([True]), valid, review_beliefs=True))
        forged = copy.deepcopy(data)
        forged["beliefs"][0]["evidence_ids"] = [old["key"]]
        forged["beliefs"][0]["quotes"][0] = {"source_id": old["key"], "quote": OLD}
        self.assertIsNone(parse(forged, [a, b], state))

    def test_quoted_text_must_be_unique_and_cover_every_evidence_source(self):
        a, b = record("A"), record("B")
        original = proposal([a, b])
        changes = [[], [{"source_id": a["id"], "quote": a["text"]}],
                   [{"source_id": a["id"], "quote": "没有出现的原话"}, {"source_id": b["id"], "quote": b["text"]}],
                   [{"source_id": a["id"], "quote": a["text"]}]*2]
        for quotes in changes:
            data = copy.deepcopy(original)
            data["beliefs"][0]["quotes"] = quotes
            with self.subTest(quotes=quotes):
                self.assertIsNone(parse(data, [a, b], {}))
        repeated = {**a, "text": a["text"]+"；"+a["text"]}
        self.assertIsNone(parse(proposal([a]), [repeated], {}))

    def test_new_existing_and_revision_contracts_reject_bypasses_and_bad_types(self):
        a, b = record("A"), record("B")
        old = prior([a])
        original = proposal([b], old=old, relation="challenge", statement=NEW)
        for field, value in (("relation", []), ("relation", {}), ("relation", "replace"),
                             ("previous_key", "b:"+"0"*24), ("previous_revision", True),
                             ("previous_revision", 0), ("previous_revision", 2)):
            data = copy.deepcopy(original)
            data["beliefs"][0][field] = value
            with self.subTest(field=field, value=value):
                self.assertIsNone(parse(data, [a, b], {"beliefs": [old]}))
        self.assertIsNone(parse(proposal([b]), [a, b], {"beliefs": [old]}))
        self.assertIsNone(parse(proposal([b], old=old, relation="reinforce", statement=NEW),
                                [a, b], {"beliefs": [old]}))
        self.assertIsNone(parse(original, [a, b], {"beliefs": []}))

    def test_pending_context_is_limited_metadata_not_a_source(self):
        a, b = record("A"), record("B")
        old = prior([a, b], assessment={"status": "contested", "candidate_text": NEW,
            "candidate_relation": "challenge", "candidate_sources": [{"id": b["id"], "digest": "not-in-state-view"}],
            "candidate_origins": ["private-origin-not-in-state-view"], "candidate_contents": ["secret-content-digest"]})
        prompt = build_growth_prompt([a, b], {"beliefs": [old]}, review_beliefs=True)
        data = json.loads(prompt.split("UNTRUSTED_GROWTH_INPUT\n", 1)[1])
        view = data["current_self"]["beliefs"][0]
        self.assertEqual(view["key"], old["key"])
        self.assertEqual(view["assessment"], {"status": "contested", "candidate_text": NEW,
            "candidate_relation": "challenge", "independent_evidence_count": 1})
        self.assertNotIn("secret-content-digest", prompt)
        self.assertEqual(len(data["source_records"]), 2)

    def test_legacy_long_prior_keeps_its_trailing_qualification(self):
        a, b = record("A"), record("B")
        text = "记录"*95+"，但对方明确求建议时不适用"
        old = prior([a], text=text)
        state = {"beliefs": [old]}
        first = build_growth_prompt([a, b], state, review_beliefs=True)
        candidate = proposal([b], old=old, relation="refine", statement=NEW)
        second = build_growth_review_prompt(candidate, [a, b], state=state, review_beliefs=True)
        for prompt, delimiter in ((first, "UNTRUSTED_GROWTH_INPUT\n"), (second, "UNTRUSTED_GROWTH_REVIEW\n")):
            data = json.loads(prompt.split(delimiter, 1)[1])
            self.assertEqual(data["current_self"]["beliefs"][0]["text"], text)

    def test_default_model_contract_remains_compatible(self):
        a = record("A")
        data = proposal([a])
        for field in ("relation", "previous_key", "previous_revision", "quotes"):
            data["beliefs"][0].pop(field)
        self.assertIsNotNone(parse_growth(json.dumps(data), [a]))
        self.assertIsNone(parse_growth(json.dumps(data), [a], review_beliefs=True))

    def test_context_selection_reserves_whole_old_roots_and_fresh_observation(self):
        roots = [record("old-"+str(i), scope="private:12345678") for i in range(3)]
        fresh = [record("new-"+str(i)) for i in range(12)]
        lookup = {r["id"]: r for r in roots+fresh}
        old = prior(roots)
        selected = select_belief_context(fresh, {"beliefs": [old]}, lambda ids: {i: lookup[i] for i in ids}, limit=12)
        self.assertEqual(len(selected), 12)
        self.assertEqual(selected[:3], roots)
        self.assertEqual(selected[3:], fresh[:9])
        self.assertEqual(len({r["id"] for r in selected}), len(selected))

    def test_context_selection_rejects_missing_changed_or_too_many_old_roots(self):
        a, b = record("A"), record("B")
        state = {"beliefs": [prior([a])]}
        self.assertEqual(select_belief_context([b], state, lambda ids: {}), [b])
        self.assertEqual(select_belief_context([b], state, lambda ids: {a["id"]: {**a, "text": "已撤回"}}), [b])
        many = [record(str(i)) for i in range(12)]
        self.assertEqual(select_belief_context([b], {"beliefs": [prior(many)]}, lambda ids: {r["id"]: r for r in many}), [b])
        self.assertEqual(select_belief_context([a], state, lambda ids: {a["id"]: a}), [a])
