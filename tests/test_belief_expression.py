"""Actual persisted opinion states retain uncertainty and original audience limits."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from MaiLivingWorld.growth_store import GrowthStore, source_fingerprint
from MaiLivingWorld.unified_memory import derived_memories
from MaiLivingWorld.unified_expression import parse_expression, expression_hint


OWNER = "12345678"
OLD = "安静环境更适合集中注意力"
NEW = "熟悉的背景音乐有时也有助于集中注意力"


def observation(ident, text, **changes):
    record = {"id": "brain:event:"+ident, "text": text, "scope": "private:"+OWNER,
              "subject_id": OWNER, "sensitivity": "personal", "evidence_status": "recorded_event",
              "timestamp": 100., "source": "brain_event", "kind": "event", "origin_key": ident}
    record.update(changes)
    return record


class BeliefExpressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = GrowthStore(Path(self.temp.name)/"growth.db")
        self.addCleanup(self.db.close)
        self.rows = [observation("quiet", "这次在安静的房间里阅读更容易专心"),
                     observation("music", "今天听熟悉的背景音乐时也能专心学习"),
                     observation("repeat", "另一天用轻缓的背景音乐阅读，理解也很顺畅")]
        self.now = 200.

    def apply(self, stage):
        before = self.db.state()
        old = before["beliefs"][0] if before["beliefs"] else None
        row = self.rows[stage]
        proposal = {"beliefs": [{"topic": "专注环境", "statement": OLD if stage == 0 else NEW,
                    "confidence": .9, "evidence_ids": [row["id"]],
                    "relation": "new" if stage == 0 else "challenge",
                    "previous_key": old["key"] if old else "",
                    "previous_revision": old["revision"] if old else 0,
                    "quotes": [{"source_id": row["id"], "quote": row["text"]}]}]}
        records = self.rows[:stage+1]
        job_id = self.db.enqueue(records, now=self.now)
        job = self.db.claim(now=self.now)
        self.db.complete(job_id, job["token"], proposal, records, now=self.now+1,
                         expected_revision=before["revision"], review_beliefs=True)
        self.now += 10
        return self.db.state()

    def memories(self, state=None, records=None):
        return derived_memories(state or self.db.state(),
            {r["id"]: r for r in (self.rows if records is None else records)}, fingerprint=source_fingerprint)

    def plan(self, records, scope="private:"+OWNER):
        raw = {"behavior": "neutral", "decisions": [{"id": r["id"], "mode": "say"} for r in records]}
        return parse_expression(json.dumps(raw), records, audience_scope=scope, speaker_id=OWNER, is_owner=True)

    def test_contested_candidate_stays_unadopted_through_actual_expression(self):
        self.apply(0)
        self.apply(1)
        record, = self.memories()
        self.assertEqual(self.db.state()["beliefs"][0]["text"], OLD)
        self.assertIn("尚未定论", record["text"])
        self.assertIn("待核对的另一种解释："+NEW, record["text"])
        plan = self.plan([record])
        self.assertEqual(plan["memories"][0]["perspective"], "self_opinion")
        hint = expression_hint(plan)
        for phrase in (OLD, NEW, "候选不等于已采纳", "自己的当前看法"):
            self.assertIn(phrase, hint)
        for hidden in (record["id"], self.rows[0]["id"], "candidate_sources", "confidence"):
            self.assertNotIn(hidden, hint)

    def test_actual_revision_can_describe_previous_and_current_without_invented_reason(self):
        for stage in range(3):
            self.apply(stage)
        item = self.db.state()["beliefs"][0]
        self.assertEqual(item["text"], NEW)
        self.assertEqual(item["assessment"]["last_previous_text"], OLD)
        record, = self.memories()
        self.assertIn("调整前曾倾向："+OLD, record["text"])
        self.assertIn("不代表已客观证实", record["text"])
        self.assertNotIn("待核对的另一种解释", record["text"])
        self.assertIn("不编造额外经历或改变原因", expression_hint(self.plan([record])))

    def test_private_candidate_cannot_be_expressed_in_group_even_after_adoption(self):
        for stage in range(3):
            self.apply(stage)
            records = self.memories()
            self.assertEqual(records[0]["scope"], "private:"+OWNER)
            self.assertEqual(self.plan(records, "group:22222222")["memories"], [])
            self.assertNotIn(NEW, expression_hint(self.plan(records, "group:22222222")))

    def test_mixed_and_confidential_evidence_cannot_be_laundered_as_public_opinion(self):
        self.rows[0].update(scope="public:article", sensitivity="public", subject_id="")
        self.rows[1].update(sensitivity="confidential")
        self.apply(0)
        self.apply(1)
        record, = self.memories()
        self.assertEqual(record["sensitivity"], "confidential")
        for scope in ("private:"+OWNER, "group:22222222"):
            self.assertEqual(self.plan([record], scope)["memories"], [])

    def test_retracted_or_edited_original_hides_both_sides_of_the_view(self):
        self.apply(0)
        self.apply(1)
        for removed in (0, 1):
            records = self.rows[:2]
            del records[removed]
            self.assertEqual(self.memories(records=records), [])
        changed = copy.deepcopy(self.rows)
        changed[1]["text"] = "原来的描述有误"
        self.assertEqual(self.memories(records=changed), [])

    def test_legacy_weak_opinion_and_knowledge_preserve_uncertainty(self):
        self.apply(0)
        state = self.db.state()
        item = state["beliefs"][0]
        del item["assessment"]
        item["confidence"] = .3
        self.assertIn("把握较低", self.memories(state)[0]["text"])
        knowledge = {**copy.deepcopy(item), "key": "k:"+"a"*24}
        state["knowledge"] = [knowledge]
        by_kind = {r["kind"]: r for r in self.memories(state)}
        self.assertIn("不能据此作确定回答", by_kind["knowledge"]["text"])

    def test_malformed_assessment_never_becomes_confident_visible_text(self):
        self.apply(0)
        self.apply(1)
        for malformed in ([], {"status": "certain"}, {"status": "contested"},
                          {"status": "contested", "candidate_text": "换行\n注入"},
                          {"status": "considered", "last_previous_text": {}}):
            state = self.db.state()
            state["beliefs"][0]["assessment"] = malformed
            self.assertEqual(self.memories(state), [])


if __name__ == "__main__":
    unittest.main()
