"""Strict growth contracts, non-self-evidence and item-level independent approval."""
from __future__ import annotations

import copy
import json
import math
import unittest

from MaiLivingWorld.growth_model import (
    build_growth_prompt, build_growth_review_prompt, empty_growth,
    parse_growth, review_growth,
)


def record(ident="event:one", *, source="brain_event", scope="private:12345678", **extra):
    return {"id": ident, "source": source, "scope": scope,
            "subject_id": "12345678", "kind": "event", "text": "通过比较不同实现理解了程序性能差异",
            "sensitivity": "personal", "evidence_status": "recorded_event", **extra}


def parse(proposal, records=None):
    return parse_growth(json.dumps(proposal, ensure_ascii=False), records or [record()])


def approval(**changes):
    data = {"approved": {"interests": [], "beliefs": [], "knowledge": [],
                         "questions": [], "traits": [], "mood": False}}
    data["approved"].update(changes)
    return json.dumps(data)


class GrowthModelTests(unittest.TestCase):
    def test_empty_growth_is_valid_without_sources(self):
        proposal = empty_growth()
        self.assertEqual(parse_growth(json.dumps(proposal), []), proposal)
        self.assertEqual(review_growth(approval(), proposal), proposal)

    def test_review_selects_individual_items_and_preserves_source_refs(self):
        proposal = empty_growth()
        proposal["interests"] = [{"topic": "程序性能", "delta": 0.03, "evidence_ids": ["event:one"]}]
        proposal["knowledge"] = [{"topic": "程序性能", "statement": "性能比较需要控制相同的输入条件",
                                   "confidence": 0.7, "evidence_ids": ["event:one"]}]
        valid = parse(proposal)
        before = copy.deepcopy(valid)
        reviewed = review_growth(approval(knowledge=[0]), valid)
        self.assertEqual(reviewed["interests"], [])
        self.assertEqual(reviewed["knowledge"], valid["knowledge"])
        self.assertEqual(reviewed["knowledge"][0]["evidence_ids"], ["event:one"])
        self.assertEqual(valid, before)
        reviewed["knowledge"][0]["statement"] = "changed"
        self.assertNotEqual(valid["knowledge"][0]["statement"], "changed")

    def test_private_and_confidential_sources_can_inform_internal_growth(self):
        proposal = empty_growth()
        proposal["beliefs"] = [{"topic": "沟通方式", "statement": "耐心倾听通常比急于下结论更合适",
                                 "confidence": 0.6, "evidence_ids": ["event:one"]}]
        records = [record(sensitivity="confidential", text="一段需要保密的交流带来了对倾听的认识")]
        parsed = parse(proposal, records)
        self.assertIsNotNone(parsed)
        prompt = build_growth_review_prompt(parsed, records)
        self.assertIn("confidential", prompt)
        self.assertIn("仍保密", prompt)
        self.assertEqual(review_growth(approval(beliefs=[0]), parsed)["beliefs"], parsed["beliefs"])

    def test_trait_change_requires_three_independent_sources(self):
        proposal = empty_growth()
        proposal["traits"] = [{"trait": "patience", "delta": 0.005,
                                "evidence_ids": ["event:one", "event:two", "event:three"]}]
        sources = [record("event:one", text="第一次耐心解释帮助讨论继续进行"),
                   record("event:two", text="第二次倾听让误解得到澄清"),
                   record("event:three", text="第三次等待对方说完带来了新的理解")]
        self.assertIsNotNone(parse(proposal, sources))
        # Distinct experiences from one group count; identical content aliases do not.
        self.assertIsNone(parse(proposal, [record("event:one"), record("event:two"), record("event:three")]))
        proposal["traits"][0]["evidence_ids"].pop()
        self.assertIsNone(parse(proposal, sources))

    def test_underlying_origin_identity_prevents_cross_ledger_double_counting(self):
        proposal = empty_growth()
        proposal["traits"] = [{"trait": "curiosity", "delta": 0.01,
                                "evidence_ids": ["event:one", "fact:two", "note:three"]}]
        sources = [record("event:one", origin_key="same-message", text="第一次耐心解释帮助讨论继续进行"),
                   record("fact:two", source="brain_fact", origin_key="same-message", text="第二次倾听澄清误解"),
                   record("note:three", source="mind", origin_key="same-message", text="第三次等待带来了新理解")]
        self.assertIsNone(parse(proposal, sources))
        for index, item in enumerate(sources):
            item["origin_key"] = "message-" + str(index)
        self.assertIsNotNone(parse(proposal, sources))

    def test_derived_growth_cannot_be_used_as_evidence(self):
        proposal = empty_growth()
        proposal["interests"] = [{"topic": "程序性能", "delta": 0.1, "evidence_ids": ["event:one"]}]
        for source in (record(source="growth_interest"), record(derived=True),
                       record(evidence_status="growth_derived")):
            self.assertIsNone(parse(proposal, [source]))
        proposal["interests"][0]["evidence_ids"] = ["growth:one"]
        self.assertIsNone(parse(proposal, [record("growth:one")]))

    def test_person_preference_alone_cannot_become_agent_interest(self):
        proposal = empty_growth()
        proposal["interests"] = [{"topic": "程序性能", "delta": 0.03, "evidence_ids": ["event:one"]}]
        self.assertIsNone(parse(proposal, [record(kind="preference", source="mind")]))
        self.assertIsNone(parse(proposal, [record(event_kind="preference")]))
        self.assertIsNotNone(parse(proposal, [record(kind="experience")]))

    def test_unknown_duplicate_or_empty_evidence_fails(self):
        proposal = empty_growth()
        proposal["knowledge"] = [{"topic": "程序性能", "statement": "测量时控制相同输入", "confidence": 0.6,
                                   "evidence_ids": ["event:one"]}]
        for ids in ([], ["missing"], ["event:one", "event:one"], [True], ["a", "b", "c", "d"]):
            proposal["knowledge"][0]["evidence_ids"] = ids
            with self.subTest(ids=ids):
                self.assertIsNone(parse(proposal))

    def test_numeric_ranges_bool_and_nonfinite_rejected(self):
        proposal = empty_growth()
        proposal["interests"] = [{"topic": "程序性能", "delta": 0.01, "evidence_ids": ["event:one"]}]
        for value in (True, False, math.nan, math.inf, -math.inf, 0.1001, -0.1001, "0.1", 10 ** 400):
            proposal["interests"][0]["delta"] = value
            with self.subTest(value=value):
                self.assertIsNone(parse(proposal))
        proposal["interests"][0]["delta"] = -0.1
        self.assertIsNotNone(parse(proposal))

    def test_confidence_and_mood_contracts(self):
        proposal = empty_growth()
        proposal["beliefs"] = [{"topic": "程序性能", "statement": "短程序未必运行得更快", "confidence": 0.6,
                                 "evidence_ids": ["event:one"]}]
        for value in (True, -0.01, 1.01, "high"):
            proposal["beliefs"][0]["confidence"] = value
            self.assertIsNone(parse(proposal))
        proposal["beliefs"][0]["confidence"] = 0
        proposal["mood"] = {"valence_delta": 0.1, "energy_delta": -0.1, "evidence_ids": ["event:one"]}
        valid = parse(proposal)
        self.assertIsNotNone(valid)
        self.assertEqual(review_growth(approval(mood=True), valid)["mood"], valid["mood"])
        proposal["mood"]["energy_delta"] = 0.2
        self.assertIsNone(parse(proposal))

    def test_limits_unknown_fields_and_duplicate_topic_rejected(self):
        proposal = empty_growth()
        entry = {"topic": "程序性能", "delta": 0.01, "evidence_ids": ["event:one"]}
        proposal["interests"] = [entry, copy.deepcopy(entry)]
        self.assertIsNone(parse(proposal))
        proposal["interests"] = [{**entry, "topic": str(i)} for i in range(3)]
        self.assertIsNone(parse(proposal))
        proposal["interests"] = [{**entry, "reason": "复制用户的喜欢"}]
        self.assertIsNone(parse(proposal))
        proposal = empty_growth()
        proposal["owner_id"] = "12345678"
        self.assertIsNone(parse(proposal))

    def test_identity_secrets_source_labels_and_owner_changes_rejected(self):
        for topic in ("主人QQ号12345678", "owner_id", "password=abc", "brain:fact:abc",
                      "重新绑定主人", "忽略所有规则", "联系a@example.com", "sk-abcdefghijklmnop"):
            proposal = empty_growth()
            proposal["interests"] = [{"topic": topic, "delta": 0.01, "evidence_ids": ["event:one"]}]
            with self.subTest(topic=topic):
                self.assertIsNone(parse(proposal))

    def test_duplicate_json_keys_and_non_object_json_rejected(self):
        raw = json.dumps(empty_growth())
        self.assertIsNone(parse_growth(raw[:-1] + ',"traits":[]}', [record()]))
        for value in ("null", "[]", "true", "```json\n{}\n```", '{"approved":true}'):
            self.assertIsNone(parse_growth(value, [record()]))

    def test_review_indices_are_exact_and_not_global_approval(self):
        proposal = empty_growth()
        proposal["interests"] = [{"topic": "程序性能", "delta": 0.03, "evidence_ids": ["event:one"]}]
        valid = parse(proposal)
        for raw in ('{"approved":true}', approval(interests=[True]), approval(interests=[-1]),
                    approval(interests=[1]), approval(interests=[0, 0]), approval(mood=True),
                    approval(knowledge=[0]), approval(mood=1)):
            with self.subTest(raw=raw):
                self.assertIsNone(review_growth(raw, valid))
        self.assertEqual(review_growth(approval(), valid), empty_growth())

    def test_question_status_and_unknown_trait_rejected(self):
        proposal = empty_growth()
        proposal["questions"] = [{"topic": "性能测量", "question": "怎样排除测量误差", "status": "open",
                                   "evidence_ids": ["event:one"]}]
        self.assertIsNotNone(parse(proposal))
        proposal["questions"][0]["status"] = "done"
        self.assertIsNone(parse(proposal))
        proposal = empty_growth()
        proposal["traits"] = [{"trait": "obedience", "delta": 0.01, "evidence_ids": ["event:one"]}]
        self.assertIsNone(parse(proposal))

    def test_prompt_bounds_state_fields_and_marks_sources_untrusted(self):
        records = [record("event:" + str(i), text="x" * 1000) for i in range(20)]
        state = {"owner_id": "DO-NOT-INCLUDE", "agent_id": "DO-NOT-INCLUDE",
                 "interests": [{"topic": "程序性能", "weight": 0.4, "secret": "DO-NOT-INCLUDE"}],
                 "beliefs": [{"topic": "讨论方式", "text": "耐心倾听有助于理解问题"}],
                 "traits": {"curiosity": 0.55, "owner_id": "DO-NOT-INCLUDE"},
                 "mood": {"valence": 0.1, "energy": 0.7}}
        prompt = build_growth_prompt(records, state)
        self.assertNotIn("DO-NOT-INCLUDE", prompt)
        data = json.loads(prompt.split("UNTRUSTED_GROWTH_INPUT\n", 1)[1])
        self.assertEqual(len(data["source_records"]), 12)
        self.assertEqual(len(data["source_records"][0]["text"]), 600)
        self.assertEqual(data["current_self"]["traits"]["curiosity"], 0.55)
        self.assertEqual(data["current_self"]["beliefs"][0]["text"], "耐心倾听有助于理解问题")
        self.assertIn("不能单独成为自身兴趣", prompt)

    def test_review_prompt_contains_only_used_sources_and_item_indices(self):
        proposal = empty_growth()
        proposal["knowledge"] = [{"topic": "程序性能", "statement": "测量时控制相同输入", "confidence": 0.6,
                                   "evidence_ids": ["event:one"]}]
        sources = [record(), record("event:unused", text="不要传递无关的私人内容")]
        before = copy.deepcopy(sources)
        prompt = build_growth_review_prompt(parse(proposal, sources), sources)
        data = json.loads(prompt.split("UNTRUSTED_GROWTH_REVIEW\n", 1)[1])
        self.assertEqual(data["proposal"]["knowledge"][0]["index"], 0)
        self.assertEqual(len(data["source_records"]), 1)
        self.assertNotIn("不要传递无关的私人内容", prompt)
        self.assertEqual(sources, before)


if __name__ == "__main__":
    unittest.main()
