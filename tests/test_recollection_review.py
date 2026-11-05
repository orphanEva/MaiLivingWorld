"""Focused integration regressions found during the v0.18 review."""
import json
from pathlib import Path
import tempfile
import unittest

from MaiLivingWorld.growth_model import build_growth_prompt, empty_growth, parse_growth
from MaiLivingWorld.growth_store import source_fingerprint
from MaiLivingWorld.recollection_runtime import RecollectionRuntime
from MaiLivingWorld.recollection_store import RecollectionStore
from MaiLivingWorld.unified_expression import expression_hint, parse_expression


OWNER = "12345678"
CORRECTION = "你记错了，我不是讨厌猫，而是对猫毛过敏。"


def original(text):
    return {"id": "brain:fact:compound", "source": "brain_fact", "kind": "fact", "scope": "private:"+OWNER,
            "subject_id": OWNER, "text": text, "timestamp": 10., "sensitivity": "personal", "evidence_status": "active"}


def entry(record, *, old_quote="主人讨厌猫。", quote=CORRECTION):
    return {"id": "recollection:revision:"+"a"*64, "target_id": record["id"], "target_digest": source_fingerprint(record),
            "input_timestamp": 100., "input_id": "recollection:input:"+"b"*64, "old_quote": old_quote,
            "quote": quote, "relation": "clarify", "sensitivity": "personal"}


def runtime_for(entries):
    runtime = object.__new__(RecollectionRuntime)
    runtime.active = lambda: list(entries)
    return runtime


def message(text, mid):
    return {"platform": "qq", "account_id": "99999999", "session_id": "owner-private", "message_id": mid,
            "message_info": {"user_info": {"user_id": OWNER}}, "processed_plain_text": text}


class RecollectionReviewTests(unittest.TestCase):
    def long_overlay(self):
        affected, unrelated = "主人讨厌猫。", "最常听的乐器是大提琴。"
        record = original("记"*(600-len(affected)-len(unrelated))+affected+unrelated)
        overlay = runtime_for([entry(record)]).overlay([record])[0]
        self.assertEqual(len(record["text"]), 600)
        self.assertIn(unrelated, overlay["text"])
        self.assertIn(CORRECTION, overlay["text"])
        return record, overlay, unrelated

    def test_long_valid_correction_and_unrelated_tail_reach_expression_together(self):
        record, overlay, unrelated = self.long_overlay()
        result = json.dumps({"behavior": "neutral", "decisions": [{"id": record["id"], "mode": "say"}]})
        plan = parse_expression(result, [overlay], audience_scope="private:"+OWNER, speaker_id=OWNER, is_owner=True)
        self.assertIsNotNone(plan, "A valid bounded correction must not silently disappear at the old 600-char limit")
        rendered = expression_hint(plan)
        self.assertIn(CORRECTION, rendered)
        self.assertIn(unrelated, rendered)

    def test_growth_reads_complete_correction_not_a_cropped_negation_or_old_fact(self):
        _, overlay, unrelated = self.long_overlay()
        prompt = build_growth_prompt([overlay], {})
        records = json.loads(prompt.split("UNTRUSTED_GROWTH_INPUT\n", 1)[1])["source_records"]
        self.assertEqual(len(records), 1)
        self.assertIn(CORRECTION, records[0]["text"])
        self.assertIn(unrelated, records[0]["text"])
        self.assertEqual(records[0]["evidence_status"], "owner_reported")

    def test_larger_bound_requires_all_trusted_correction_metadata(self):
        record = {**original("长"*800), "correction_id": "recollection:revision:"+"a"*64,
                  "evidence_status": "owner_reported"}
        result = json.dumps({"behavior": "neutral", "decisions": [{"id": record["id"], "mode": "say"}]})
        changes = ({"correction_id": "recollection:revision:bad"}, {"correction_id": None},
                   {"evidence_status": "active"}, {"scope": "group:"+OWNER}, {"subject_id": "87654321"},
                   {"source": "tool"}, {"source": "brain_event"})
        for change in changes:
            candidate = {**record, **change}
            with self.subTest(change=change):
                self.assertIsNone(parse_expression(result, [candidate], audience_scope="private:"+OWNER,
                                                   speaker_id=OWNER, is_owner=True))
                prompt = build_growth_prompt([candidate], {})
                sources = json.loads(prompt.split("UNTRUSTED_GROWTH_INPUT\n", 1)[1])["source_records"]
                self.assertTrue(all(len(row["text"]) <= 600 and "correction_id" not in row for row in sources))

    def test_corrected_text_has_fixed_upper_bound_and_marker_never_reaches_reply(self):
        marker = "recollection:revision:"+"a"*64
        record = {**original("长"*1800), "correction_id": marker, "evidence_status": "owner_reported"}
        result = json.dumps({"behavior": "neutral", "decisions": [{"id": record["id"], "mode": "say"}]})
        plan = parse_expression(result, [record], audience_scope="private:"+OWNER, speaker_id=OWNER, is_owner=True)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["memories"][0]["correction_id"], marker)
        self.assertIn(record["text"], expression_hint(plan))
        self.assertNotIn(marker, expression_hint(plan))
        group = parse_expression(result, [record], audience_scope="group:77777777", speaker_id=OWNER, is_owner=True)
        self.assertEqual(group["memories"], [])
        self.assertNotIn("长", expression_hint(group))
        oversized = {**record, "text": "长"*1801}
        self.assertIsNone(parse_expression(result, [oversized], audience_scope="private:"+OWNER, speaker_id=OWNER, is_owner=True))
        prompt = build_growth_prompt([oversized], {})
        self.assertEqual(json.loads(prompt.split("UNTRUSTED_GROWTH_INPUT\n", 1)[1])["source_records"], [])

    def test_internal_correction_identifier_cannot_become_generalized_output(self):
        record = {**original("猫毛可能让过敏的人感到不适。"), "evidence_status": "owner_reported",
                  "correction_id": "recollection:revision:"+"a"*64}
        response = json.dumps({"behavior": "neutral", "decisions": [{"id": record["id"], "mode": "generalize",
            "text": "资料标识是"+record["correction_id"]}]})
        plan = parse_expression(response, [record], audience_scope="group:77777777", speaker_id=OWNER, is_owner=True)
        self.assertEqual(plan["pending_generalizations"], [])

    def test_corrected_owner_preference_cannot_alone_become_agents_own_interest(self):
        corrected = {**original("主人更正：我喜欢的是爵士音乐。"), "evidence_status": "owner_reported",
                     "correction_id": "recollection:revision:"+"a"*64}
        proposal = empty_growth()
        proposal["interests"] = [{"topic": "爵士音乐", "delta": .02, "evidence_ids": [corrected["id"]]}]
        self.assertIsNone(parse_growth(json.dumps(proposal), [corrected]))
        exploration = {**original("比较了不同爵士乐曲的节奏，想继续探索。"), "id": "brain:event:exploration",
                       "source": "brain_event", "kind": "event", "evidence_status": "recorded_event"}
        proposal["interests"][0]["evidence_ids"].append(exploration["id"])
        self.assertIsNotNone(parse_growth(json.dumps(proposal), [corrected, exploration]))

    def test_new_correction_to_different_span_never_reverts_previous_corrected_fact(self):
        record = original("面试在周三。主人讨厌猫。")
        with tempfile.TemporaryDirectory() as directory:
            store = RecollectionStore(Path(directory)/"review.sqlite3")
            try:
                for index, (old_quote, quote) in enumerate((("主人讨厌猫。", CORRECTION),
                        ("面试在周三。", "更正一下，我的面试改到周五了。"))):
                    now = 100.+100*index
                    store.enqueue(message(quote, str(index)), owner_id=OWNER, now=now)
                    claim = store.claim(OWNER, now=now+1)
                    proposal = {"updates": [{key: value for key, value in entry(record, old_quote=old_quote, quote=quote).items()
                                             if key in {"target_id", "target_digest", "old_quote", "quote", "relation"}}]}
                    committed = store.complete(claim["id"], claim["token"], proposal, owner_id=OWNER,
                        expected_revision=claim["base_revision"], targets={record["id"]: record}, now=now+2)
                    self.assertEqual(committed["applied"], 1)
                overlay = runtime_for(store.active(OWNER)).overlay([record])[0]
                self.assertIn(CORRECTION, overlay["text"])
                self.assertIn("我的面试改到周五了。", overlay["text"])
                self.assertNotIn("主人讨厌猫。", overlay["text"])
                self.assertNotIn("面试在周三。", overlay["text"])
            finally:
                store.close()

    def test_followup_marker_is_only_exposed_with_permitted_owner_private_content(self):
        record = original("主人准备面试。")
        record["recently_asked"] = True
        result = json.dumps({"behavior": "patient", "decisions": [{"id": record["id"], "mode": "say"}]})
        own = parse_expression(result, [record], audience_scope="private:"+OWNER, speaker_id=OWNER, is_owner=True)
        self.assertIn("近期已问过", expression_hint(own))
        for scope, speaker, owner in (("group:77777777", OWNER, True), ("private:87654321", "87654321", False)):
            plan = parse_expression(result, [record], audience_scope=scope, speaker_id=speaker, is_owner=owner)
            hint = expression_hint(plan)
            self.assertNotIn("面试", hint)
            self.assertNotIn("近期已问过", hint)


if __name__ == "__main__":
    unittest.main()
