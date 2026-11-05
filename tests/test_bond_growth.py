"""Owner-authored recollections remain attributable, private and revocable evidence."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from MaiLivingWorld.bond_runtime import BondRuntime
from MaiLivingWorld.growth_model import (
    build_growth_prompt, build_growth_review_prompt, empty_growth, parse_growth, review_growth,
)
from MaiLivingWorld.growth_store import GrowthStore, source_fingerprint
from MaiLivingWorld.unified_memory import _source_id, derived_memories
from MaiLivingWorld.unified_expression import parse_expression, expression_hint


OWNER = "12345678"


def message(text, mid="m1"):
    return {"platform": "qq", "account_id": "99999999", "session_id": "owner-private",
            "message_id": mid, "message_info": {"user_info": {"user_id": OWNER}},
            "processed_plain_text": text, "raw_message": [{"type": "text", "data": text}]}


def approve(**changes):
    result = {"interests": [], "beliefs": [], "knowledge": [], "questions": [], "traits": [], "mood": False}
    result.update(changes)
    return json.dumps({"approved": result})


class BondGrowthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        bot = NS(ctx=NS(paths=NS(data_dir=Path(self.tmp.name))),
                 config=NS(plugin=NS(enabled=True), bond=NS(enabled=True),
                           owner=NS(enabled=True, qq=OWNER), general=NS(timezone="UTC")),
                 _unified_active=lambda: True, _ledger_storage_options=lambda: {})
        self.bond = BondRuntime(bot)
        self.growth = GrowthStore(Path(self.tmp.name) / "growth.sqlite3")
        self.bond.capture(message("/相处 记住 我们讨论过森林小说里的一段故事"))
        self.record = self.bond.recall("森林小说")[0]

    def tearDown(self):
        self.bond.close()
        self.growth.close()
        self.tmp.cleanup()

    def proposal(self):
        proposal = empty_growth()
        proposal["beliefs"] = [{"topic": "共同阅读", "statement": "对方提到过共同阅读，具体细节仍待确认",
                                "confidence": .4, "evidence_ids": [self.record["id"]]}]
        return proposal

    def learn(self):
        records = [self.record]
        self.growth.enqueue(records, now=1000)
        job = self.growth.claim(now=1000)
        parsed = parse_growth(json.dumps(self.proposal(), ensure_ascii=False), records)
        self.assertIsNotNone(parsed)
        checked = review_growth(approve(beliefs=[0]), parsed)
        self.growth.complete(job["id"], job["token"], checked, records, expected_revision=0, now=1001)
        return self.growth.state(now=1002)

    def test_real_owner_memory_reaches_both_model_stages_as_unverified_account(self):
        prompt = build_growth_prompt([self.record], {})
        data = json.loads(prompt.split("UNTRUSTED_GROWTH_INPUT\n", 1)[1])
        self.assertEqual(len(data["source_records"]), 1)
        self.assertEqual(data["source_records"][0]["id"], self.record["id"])
        self.assertEqual(data["source_records"][0]["evidence_status"], "owner_reported")
        self.assertIn("身份可信不等于内容已核实", prompt)
        self.assertIn("不能把自述的共同经历写成程序确认发生的事实", prompt)
        proposal = parse_growth(json.dumps(self.proposal(), ensure_ascii=False), [self.record])
        review = build_growth_review_prompt(proposal, [self.record])
        self.assertIn("owner_reported", review)
        self.assertIn("升级为已核实事实", review)
        # Semantic truth is checked by the independent review, not a keyword proof.
        self.assertEqual(review_growth(approve(), proposal)["beliefs"], [])

    def test_real_growth_ledger_and_derived_refs_keep_owner_attribution_and_privacy(self):
        state = self.learn()
        rows = derived_memories(state, self.bond.resolve([self.record["id"]]), fingerprint=source_fingerprint)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["sources"][0]["id"], self.record["id"])
        self.assertEqual(row["sources"][0]["digest"], source_fingerprint(self.record))
        self.assertEqual((row["scope"], row["sensitivity"], row["evidence_status"]),
                         ("private:" + OWNER, "personal", "inferred"))
        self.assertIn("仍待确认", row["text"])
        decision = json.dumps({"behavior": "warm", "decisions": [{"id": row["id"], "mode": "say"}]})
        private = parse_expression(decision, rows, audience_scope="private:" + OWNER,
                                   speaker_id=OWNER, is_owner=True)
        group = parse_expression(decision, rows, audience_scope="group:22222222",
                                 speaker_id=OWNER, is_owner=True)
        self.assertIn("仍待确认", expression_hint(private))
        self.assertNotIn("具体细节", expression_hint(group))
        self.assertEqual(group["memories"], [])

    def test_actual_retirement_removes_the_learned_view_without_erasing_audit(self):
        state = self.learn()
        row = self.bond.store.moments(OWNER)[0]
        self.assertTrue(self.bond.capture(message(f"/相处 忘记 {row['id']} {row['revision']}", "m2")))
        resolved = self.bond.resolve([row["id"]])
        self.assertEqual(resolved, {})
        self.assertEqual(derived_memories(state, resolved, fingerprint=source_fingerprint), [])
        self.assertEqual(len(self.growth.history()), 1)

    def test_changed_original_version_invalidates_the_learned_view(self):
        state = self.learn()
        current = self.bond.resolve([self.record["id"]])
        changed = {**current[self.record["id"]], "text": "主人更正了这段自述"}
        current[self.record["id"]] = changed
        self.assertNotEqual(source_fingerprint(changed), state["beliefs"][0]["sources"][0]["digest"])
        self.assertEqual(derived_memories(state, current, fingerprint=source_fingerprint), [])

    def test_only_owner_accounts_or_user_preferences_cannot_create_self_interest(self):
        proposal = empty_growth()
        proposal["interests"] = [{"topic": "森林小说", "delta": .03, "evidence_ids": [self.record["id"]]}]
        self.assertIsNone(parse_growth(json.dumps(proposal), [self.record]))
        preference = {"id": "mind:note:" + OWNER + ":preference", "source": "mind", "kind": "preference",
                      "scope": "private:" + OWNER, "subject_id": OWNER, "text": "主人喜欢森林小说",
                      "sensitivity": "personal", "evidence_status": "active"}
        proposal["interests"][0]["evidence_ids"].append(preference["id"])
        self.assertIsNone(parse_growth(json.dumps(proposal), [self.record, preference]))

    def test_owner_memory_can_supplement_actual_exploration_but_still_requires_review(self):
        actual = {"id": "brain:event:actual", "source": "brain_event", "kind": "event",
                  "scope": "private:" + OWNER, "subject_id": OWNER, "sensitivity": "personal",
                  "evidence_status": "recorded_event", "text": "读过一章后主动查阅叙事手法并继续比较同类作品"}
        proposal = empty_growth()
        proposal["interests"] = [{"topic": "叙事手法", "delta": .02,
                                  "evidence_ids": [self.record["id"], actual["id"]]}]
        parsed = parse_growth(json.dumps(proposal), [self.record, actual])
        self.assertIsNotNone(parsed)
        self.assertEqual(review_growth(approve(), parsed)["interests"], [])
        self.assertEqual(len(review_growth(approve(interests=[0]), parsed)["interests"]), 1)

    def test_bond_identifiers_are_exact_and_claimed_verified_status_is_rejected(self):
        valid = self.record["id"]
        self.assertEqual(_source_id(valid), ("bond", (valid,)))
        invalid = ("bond:moment:" + "a" * 63, "bond:moment:" + "a" * 65,
                   "bond:moment:" + "A" * 64, "bond:moment:" + "g" * 64,
                   "bond:moment:" + "a" * 64 + ":tail", "bond:memory:" + "a" * 64)
        for ident in invalid:
            with self.subTest(ident=ident):
                with self.assertRaises(ValueError):
                    _source_id(ident)
                proposal = self.proposal()
                proposal["beliefs"][0]["evidence_ids"] = [ident]
                self.assertIsNone(parse_growth(json.dumps(proposal), [{**self.record, "id": ident}]))
        for status in ("verified", "active", "growth_derived", "unknown"):
            with self.subTest(status=status):
                self.assertIsNone(parse_growth(json.dumps(self.proposal()),
                                               [{**self.record, "evidence_status": status}]))
        leaked = self.proposal()
        leaked["beliefs"][0]["statement"] = "这条推断来自bond:moment:unlistedmarker"
        self.assertIsNone(parse_growth(json.dumps(leaked), [self.record]))


if __name__ == "__main__":
    unittest.main()
