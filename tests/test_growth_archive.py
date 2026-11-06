"""The same archive lifecycle checks run against SQLite and disposable MySQL."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from MaiLivingWorld.growth_store import GrowthStore
from MaiLivingWorld.memory_semantics import archive_memories


def archive_evidence(index):
    return {"id": "brain:event:archive-" + str(index), "text": "学习条目" + str(index),
            "scope": "private:12345678", "subject_id": "12345678", "sensitivity": "personal",
            "evidence_status": "recorded_event", "timestamp": 100.0 + index,
            "source": "brain_event", "kind": "event"}


def archive_proposal(source, topic, *, all_categories=True):
    refs = [source["id"]]
    result = {"interests": [{"topic": topic, "delta": .1, "evidence_ids": refs}]}
    if all_categories:
        result.update({"beliefs": [{"topic": topic, "statement": "目前倾向继续学习", "confidence": .6, "evidence_ids": refs}],
                       "knowledge": [{"topic": topic, "statement": "从材料记录学习方法", "confidence": .6, "evidence_ids": refs}],
                       "questions": [{"topic": topic, "question": "还有哪些学习方法", "status": "open", "evidence_ids": refs}]})
    return result


class GrowthArchiveChecks:
    def archive_submit(self, db, index, *, topic=None, all_categories=True, archive=True, mode="active"):
        source = archive_evidence(index)
        data = archive_proposal(source, topic or "主题" + str(index), all_categories=all_categories)
        job_id = db.enqueue([source], mode=mode, now=100 + index * 2)
        job = db.claim(now=100 + index * 2, mode=mode)
        return db.complete(job_id, job["token"], data, [source], now=101 + index * 2, archive_overflow=archive)

    def fill_archive(self, db, count=33, *, all_categories=True):
        for index in range(count):
            self.archive_submit(db, index, all_categories=all_categories)

    def test_archive_all_categories_continue_learning_and_survive_reconnect(self):
        db = self.store()
        self.fill_archive(db)
        peer = self.store()
        self.assertEqual(peer.capacity_counts(), {name: {"active": 32, "active_limit": 32, "archived": 1}
                                                  for name in ("interests", "beliefs", "knowledge", "questions")})
        archived = peer.archived()
        self.assertEqual(len(archived), 4)
        self.assertEqual({row["item"]["topic"] for row in archived}, {"主题0"})
        self.assertTrue(all(row["archive_revision"] == 33 for row in archived))
        self.assertNotIn("_archive", peer.state())
        self.assertEqual(len(peer.history()[0]["archived"]), 4)
        self.assertTrue(all(row["item"]["sources"][0]["id"] == archive_evidence(0)["id"] for row in archived))

    def test_archive_return_restores_lineage_and_rollback_restores_pointer(self):
        db = self.store()
        self.fill_archive(db)
        old = {row["category"]: row["item"] for row in db.archived()}
        self.archive_submit(db, 40, topic="主题0")
        state = self.store().state()
        for category in old:
            restored = next(item for item in state[category] if item["key"] == old[category]["key"])
            self.assertEqual(restored["revision"], 2)
            self.assertEqual({ref["id"] for ref in restored["sources"]},
                             {archive_evidence(0)["id"], archive_evidence(40)["id"]})
        self.assertEqual({row["item"]["topic"] for row in db.archived()}, {"主题1"})
        db.rollback(state["revision"], now=200)
        self.assertEqual({row["category"]: row["item"] for row in self.store().archived()}, old)
        self.assertEqual(db.capacity_counts()["interests"]["active"], 32)

    def test_archive_retirement_does_not_restore_retired_sources(self):
        db = self.store()
        self.fill_archive(db, all_categories=False)
        old = db.archived()[0]["item"]
        db.retire(old["key"], db.state()["revision"], now=180)
        self.assertEqual(db.archived(), [])
        self.archive_submit(db, 50, topic="主题0", all_categories=False)
        current = next(item for item in self.store().state()["interests"] if item["key"] == old["key"])
        self.assertEqual(current["revision"], 1)
        self.assertEqual([ref["id"] for ref in current["sources"]], [archive_evidence(50)["id"]])
        self.assertEqual(db.history()[1]["kind"], "retire")

    def test_archive_same_batch_eviction_then_update_preserves_original_lineage(self):
        db = self.store()
        self.fill_archive(db, count=32, all_categories=False)
        source = archive_evidence(40)
        data = archive_proposal(source, "新主题", all_categories=False)
        data["interests"].extend(archive_proposal(source, "主题0", all_categories=False)["interests"])
        jid = db.enqueue([source], now=200)
        job = db.claim(now=200)
        db.complete(jid, job["token"], data, [source], now=201, archive_overflow=True)
        restored = next(item for item in db.state()["interests"] if item["topic"] == "主题0")
        self.assertEqual(restored["revision"], 2)
        self.assertEqual({ref["id"] for ref in restored["sources"]},
                         {archive_evidence(0)["id"], archive_evidence(40)["id"]})
        self.assertEqual([entry["item"]["topic"] for entry in db.archived()], ["主题1"])

    def test_archive_legacy_caller_reuses_archived_lineage_when_active_slot_is_available(self):
        db = self.store()
        self.fill_archive(db, all_categories=False)
        previous = db.archived()[0]["item"]
        state = db.state()
        db.retire(state["interests"][0]["key"], state["revision"], now=180)
        self.archive_submit(db, 50, topic="主题0", all_categories=False, archive=False)
        restored = next(item for item in self.store().state()["interests"] if item["key"] == previous["key"])
        self.assertEqual(restored["revision"], 2)
        self.assertEqual({ref["id"] for ref in restored["sources"]},
                         {archive_evidence(0)["id"], archive_evidence(50)["id"]})
        self.assertEqual(db.archived(), [])

    def test_archive_audit_stores_changed_pointers_instead_of_growing_index_copies(self):
        db = self.store()
        self.fill_archive(db, all_categories=False)
        first = db.history()[0]
        for index in range(33, 70):
            self.archive_submit(db, index, all_categories=False)
        latest = db.history()[0]
        self.assertEqual(db.capacity_counts()["interests"]["archived"], 38)
        self.assertNotIn("_archive", latest["before"])
        self.assertNotIn("_archive", latest["after"])
        self.assertEqual(len(latest["archive_delta"]), 1)
        self.assertLess(len(json.dumps(latest)), len(json.dumps(first)) * 1.1)
        previous = db.archived(limit=128)
        self.archive_submit(db, 80, topic="主题0", all_categories=False)
        self.assertEqual(len(db.history()[0]["archive_delta"]), 2)
        db.rollback(db.state()["revision"], now=300)
        self.assertEqual(self.store().archived(limit=128), previous)

    def test_archive_rollback_still_reads_full_snapshot_history(self):
        db = self.store()
        self.fill_archive(db, all_categories=False)
        before = json.loads(db.con.execute("SELECT payload FROM growth_state").fetchone()[0])
        archived = db.archived()
        self.archive_submit(db, 40, all_categories=False)
        after = json.loads(db.con.execute("SELECT payload FROM growth_state").fetchone()[0])
        entry = db.history()[0]
        entry["before"], entry["after"] = before, after
        for key in ("archive_index_version", "archive_before_present", "archive_delta",
                    "revision", "job_id", "created", "rolled_back"):
            entry.pop(key, None)
        with db.con:
            db.con.execute("UPDATE growth_history SET payload=? WHERE revision=?",
                           (json.dumps(entry, ensure_ascii=False), db.state()["revision"]))
        db.rollback(db.state()["revision"], now=200)
        self.assertEqual(self.store().archived(), archived)

    def test_archive_observation_keeps_state_and_legacy_capacity_is_strict(self):
        db = self.store()
        self.fill_archive(db, count=32)
        before = db.state(now=170)
        result = self.archive_submit(db, 40, mode="observe")
        self.assertFalse(result["changed"])
        self.assertEqual(db.state(now=170), before)
        self.assertEqual(db.archived(), [])
        with self.assertRaisesRegex(ValueError, "state_capacity"):
            self.archive_submit(db, 50, archive=False)
        self.assertEqual(db.state(now=170), before)

    def test_archive_sources_revalidate_and_changed_root_hides_only_derived_records(self):
        db = self.store()
        self.fill_archive(db)
        original = archive_evidence(0)
        result = archive_memories(db, lambda ids: {original["id"]: original}, "主题0")
        self.assertEqual(len(result), 4)
        self.assertTrue(all(row["archived"] and row["kind"].startswith("historical_") for row in result))
        self.assertTrue(all(row["sensitivity"] == "personal" for row in result))
        self.assertEqual(archive_memories(db, lambda ids: {original["id"]: original}), [])
        self.assertEqual(archive_memories(db, lambda ids: {}, "主题0"), [])
        changed = {**original, "text": "已更正的材料"}
        self.assertEqual(archive_memories(db, lambda ids: {original["id"]: changed}, "主题0"), [])
        self.assertEqual(len(db.archived()), 4)

    def test_archive_failed_audit_rolls_back_atomically_and_retry_preserves_all_categories(self):
        db = self.store()
        self.fill_archive(db, count=32)
        source = archive_evidence(40)
        data = archive_proposal(source, "新主题")
        jid = db.enqueue([source], now=200)
        job = db.claim(now=200)
        original = db.con

        class FailingHistory:
            def __enter__(self):
                original.__enter__()
                return self

            def __exit__(self, *args):
                return original.__exit__(*args)

            def execute(self, sql, parameters=()):
                if sql.startswith("INSERT INTO growth_history"):
                    raise RuntimeError("synthetic-history-failure")
                return original.execute(sql, parameters)

        with patch.object(db, "con", FailingHistory()):
            with self.assertRaisesRegex(RuntimeError, "synthetic-history-failure"):
                db.complete(jid, job["token"], data, [source], now=201, archive_overflow=True)
        self.assertEqual(db.archived(), [])
        self.assertEqual(db.status()["revision"], 32)
        self.assertEqual(db.status()["consumed"], 32)
        result = db.complete(jid, job["token"], data, [source], now=202, archive_overflow=True)
        self.assertEqual(result["archived_items"], 4)
        self.assertEqual(len(self.store().archived()), 4)


class GrowthArchiveTests(GrowthArchiveChecks, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "growth.sqlite3"
        self.stores = []

    def store(self):
        db = GrowthStore(self.path)
        self.stores.append(db)
        return db

    def tearDown(self):
        for db in self.stores:
            db.close()
        self.temp.cleanup()
