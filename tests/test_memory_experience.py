from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from MaiLivingWorld.growth_store import GrowthStore, source_fingerprint
from MaiLivingWorld.tests.test_growth_archive import archive_evidence, archive_proposal
from MaiLivingWorld.unified_memory import annotate_experience_timeline, derived_memories


class ExperienceTimelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "growth.sqlite3"
        self.db = GrowthStore(self.path)
        self.sources = {}

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def learn(self, index, **options):
        source = archive_evidence(index)
        self.sources[source["id"]] = source
        data = archive_proposal(source, "摄影", all_categories=not options)
        jid = self.db.enqueue([source], now=100 + index * 2)
        job = self.db.claim(now=100 + index * 2)
        return self.db.complete(jid, job["token"], data, [source], now=101 + index * 2, **options)

    def records(self):
        return derived_memories(self.db.state(), self.sources, fingerprint=source_fingerprint)

    def test_timeline_joins_three_real_audit_entries_after_reconnect_and_preserves_sources(self):
        for index in range(4):
            self.learn(index)
        self.db.close()
        self.db = GrowthStore(self.path)
        records = self.records()
        result = annotate_experience_timeline(records, self.db.history(100))
        self.assertEqual(len(result), 4)
        for old, current in zip(records, result):
            self.assertEqual(current["sources"], old["sources"])
            self.assertEqual(current["sensitivity"], old["sensitivity"])
            self.assertEqual(current["id"], old["id"])
            self.assertIn("按成长记录的先后顺序", current["text"])
            self.assertEqual([point["item_revision"] for point in current["experience_timeline"]], [2, 3, 4])
            self.assertEqual([point["recorded_at"] for point in current["experience_timeline"]], [103, 105, 107])
            self.assertLessEqual(len(current["text"]), 600)

    def test_timeline_excludes_rolled_back_edits_and_new_records_without_a_transition(self):
        self.learn(0)
        initial = self.records()
        self.assertEqual(annotate_experience_timeline(initial, self.db.history()), initial)
        self.learn(1)
        self.db.rollback(2, now=120)
        current = self.records()
        self.assertEqual(annotate_experience_timeline(current, self.db.history()), current)

    def test_timeline_does_not_cross_renewed_source_generation(self):
        self.learn(0)
        previous = self.db.state()["interests"][0]
        self.learn(1, expected_revision=1, renew_interests={previous["key"]: previous["revision"]})
        renewed = [record for record in self.records() if record["kind"] == "interest"]
        self.assertEqual(annotate_experience_timeline(renewed, self.db.history()), renewed)
        self.learn(2)
        current = [record for record in self.records() if record["kind"] == "interest"]
        result = annotate_experience_timeline(current, self.db.history())
        timeline = result[0]["experience_timeline"]
        self.assertEqual([point["item_revision"] for point in timeline], [2, 3])
        self.assertTrue(all(archive_evidence(0)["id"] not in point["source_ids"] for point in timeline))

    def test_timeline_rejects_mismatched_history_and_preserves_current_qualification(self):
        self.learn(0)
        self.learn(1)
        records = self.records()
        history = copy.deepcopy(self.db.history())
        for entry in history:
            for category in ("interests", "beliefs", "knowledge", "questions"):
                for item in entry["after"][category]:
                    item["sources"][0]["digest"] = "0" * 64
        self.assertEqual(annotate_experience_timeline(records, history), records)
        result = annotate_experience_timeline(records, self.db.history())
        belief = next(record for record in result if record["kind"] == "belief")
        self.assertIn("保留不确定性", belief["text"])
