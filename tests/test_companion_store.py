from __future__ import annotations

import concurrent.futures
import hashlib
import importlib
import json
import re
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from MaiLivingWorld.companion_store import CompanionStore
from MaiLivingWorld.mysql_compat import mysql_create_table
from MaiLivingWorld.scripts.migrate_all_sqlite_to_mysql import LEDGERS, snapshot

OWNER = "12345678"
OTHER = "87654321"
HASH = hashlib.sha256(b"trusted original command").hexdigest()


def ref(seed="root", *, digest=None, scope="private:12345678", sensitivity="personal"):
    return {"id": "tool:observation:"+hashlib.sha256(seed.encode()).hexdigest(),
            "digest": digest or hashlib.sha256((seed+"-version").encode()).hexdigest(),
            "scope": scope, "sensitivity": sensitivity}


def legacy_backup(directory, count=9):
    for module, cls, filename in (
        ("store", "Store", "living_world"), ("brain_core", "BrainBook", "brain_book"),
        ("mind", "MindStore", "mind"), ("cognition", "CognitiveStore", "cognition"),
        ("experience_loop", "ExperienceLoop", "experience_loop"),
        ("proactive_feedback", "ProactiveFeedback", "proactive_feedback"),
        ("self_state", "SelfState", "self_state"), ("growth_store", "GrowthStore", "growth"),
        ("tool_store", "ToolStore", "tool_memory"),
    )[:count]:
        db = getattr(importlib.import_module("MaiLivingWorld."+module), cls)(directory/(filename+".sqlite3"))
        db.close()


class CompanionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"companion.sqlite3"
        self.db = CompanionStore(self.path)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def reopen(self):
        self.db.close()
        self.db = CompanionStore(self.path)

    def reading(self, **changes):
        values = dict(provider="pixiv", resource_type="novel", resource_id="1", version="v1",
                      owner_id=OWNER, scope="private:"+OWNER, total_units=6, source_refs=[ref()], now=100)
        values.update(changes)
        return self.db.open_reading(**values)

    def claim(self, row, **changes):
        values = dict(owner_id=OWNER, expected_revision=row["revision"], now=101)
        values.update(changes)
        return self.db.claim_reading(row["id"], **values)

    def complete(self, row, **changes):
        values = dict(owner_id=OWNER, expected_revision=row["revision"], version="v1", index=row["cursor"],
                      unit_id="unit-"+str(row["cursor"]), source_refs=[ref()], excerpt="甲\n乙", now=102)
        values.update(changes)
        return self.db.complete_unit(row["id"], row["token"], **values)

    def propose(self, **changes):
        values = dict(owner_id=OWNER, scope="private:"+OWNER, title="一起学习画画", next_step="读第一章",
                      message_id="trusted-message-1", summary_hash=HASH, now=100)
        values.update(changes)
        return self.db.propose_plan(**values)

    def command(self, row, action="accept", **changes):
        values = dict(owner_id=OWNER, action=action, expected_revision=row["revision"],
                      message_id="command-"+str(row["revision"]), summary_hash=HASH, now=101)
        values.update(changes)
        return self.db.command_plan(row["id"], **values)

    def test_constructor_and_status_leave_migration_tables_empty(self):
        self.assertEqual(self.db.readings(), [])
        self.assertEqual(self.db.plans(), [])
        self.assertEqual(self.db.status()["reading_budget"]["daily_count"], 0)
        for table in LEDGERS["companion.sqlite3"]:
            self.assertEqual(self.db.con.execute("SELECT COUNT(*) FROM "+table).fetchone()[0], 0)

    def test_published_mysql_columns_match_runtime_schema_and_are_commented(self):
        root = Path(__file__).resolve().parents[1]
        ddl = (root/"sql/mysql.sql").read_text(encoding="utf-8")
        comments = (root/"sql/mysql_comments_existing.sql").read_text(encoding="utf-8")
        columns = re.compile(r"(?:^|,)\s*(\w+)\s+(VARCHAR\(\d+\)|BIGINT|DOUBLE|LONGTEXT|TEXT)", re.M)
        for table in LEDGERS["companion.sqlite3"]:
            sql = self.db.con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
            converted = mysql_create_table(sql)
            runtime_columns = dict(columns.findall(converted.split("(", 1)[1]))
            published = re.search(r"CREATE TABLE IF NOT EXISTS `mlw_"+table+r"` \(([\s\S]*?)\) ENGINE", ddl).group(1)
            self.assertEqual(dict(columns.findall(published)), runtime_columns)
            self.assertEqual(published.count(" COMMENT '"), len(runtime_columns))
            self.assertIn("ALTER TABLE `mlw_"+table+"`", comments)

    def test_reading_identity_restart_and_provenance_across_windows(self):
        a = self.reading()
        self.reopen()
        b = self.reading(scope="group:22222222", now=200)
        self.assertEqual(a, b)
        self.assertEqual(len(self.db.readings()), 1)
        other = self.reading(owner_id=OTHER)
        self.assertNotEqual(a["id"], other["id"])
        self.assertEqual(len(self.db.readings()), 2)
        with self.assertRaisesRegex(ValueError, "owner_mismatch"):
            self.db.reading(a["id"], owner_id=OTHER)

    def test_old_active_novel_remains_schedulable_after_many_recent_other_readings(self):
        oldest = self.reading()
        for index in range(55):
            self.reading(resource_id=str(index+2), resource_type="pdf", now=200+index)
        self.assertNotIn(oldest["id"], [row["id"] for row in self.db.readings(owner_id=OWNER)])
        selected = self.db.readings(owner_id=OWNER, status="active", resource_type="novel", order="oldest", limit=1)
        self.assertEqual([row["id"] for row in selected], [oldest["id"]])

    def test_contiguous_raw_unicode_progress_and_idempotent_submission(self):
        lease = self.claim(self.reading())
        row = self.complete(lease)
        self.assertEqual((row["cursor"], row["status"]), (3, "active"))
        self.assertEqual(self.complete(lease), row)
        unit = self.db.units(row["id"])[0]
        self.assertEqual(unit["excerpt"], "甲\n乙")
        self.assertEqual(unit["content_sha256"], hashlib.sha256("甲\n乙".encode()).hexdigest())
        self.reopen()
        last = self.claim(self.db.reading(row["id"]), now=103)
        done = self.complete(last, excerpt="丙😀丁", now=104)
        self.assertEqual((done["cursor"], done["status"]), (6, "completed"))
        self.assertIsNone(self.claim(done, now=105))

    def test_gap_wrong_version_overflow_and_missing_revision_cannot_advance(self):
        lease = self.claim(self.reading())
        for change, error in (({"index": 1}, "noncontiguous"), ({"version": "v2"}, "source_version"),
                              ({"excerpt": "a"*7}, "out_of_bounds"), ({"expected_revision": 1}, "revision_conflict"),
                              ({"expected_revision": None}, "invalid_integer")):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, error):
                self.complete(lease, **change)
        self.assertEqual(self.db.reading(lease["id"])["cursor"], 0)
        self.assertEqual(self.db.units(lease["id"]), [])

    def test_unknown_total_never_claims_whole_work_finished(self):
        lease = self.claim(self.reading(total_units=None))
        row = self.complete(lease)
        self.assertIsNone(row["total_units"])
        self.assertEqual(row["status"], "active")

    def test_pdf_and_illustration_require_actual_unit_content(self):
        for kind in ("pdf", "illust"):
            lease = self.claim(self.reading(resource_type=kind, total_units=1))
            with self.assertRaisesRegex(ValueError, "empty_reading_unit"):
                self.complete(lease, excerpt="")
            done = self.complete(lease, excerpt="", image_sha256=HASH)
            self.assertEqual((done["cursor"], done["status"]), (1, "completed"))

    def test_lease_expiry_release_restart_and_stale_completion(self):
        lease = self.claim(self.reading(), lease_seconds=10)
        self.reopen()
        self.assertIsNone(self.db.claim_reading(lease["id"], owner_id=OWNER, now=110))
        replacement = self.db.claim_reading(lease["id"], owner_id=OWNER, now=111)
        self.assertNotEqual(lease["token"], replacement["token"])
        with self.assertRaisesRegex(ValueError, "revision_conflict"):
            self.complete(lease, now=112)
        released = self.db.release_reading(lease["id"], replacement["token"], owner_id=OWNER, reason="read_failed", now=112)
        self.assertEqual(released["cursor"], 0)
        self.assertIsNotNone(self.claim(released, now=112))

    def test_competing_claims_have_one_winner(self):
        row = self.reading()
        barrier = threading.Barrier(3)
        def worker(_):
            db = CompanionStore(self.path)
            try:
                barrier.wait(timeout=10)
                return db.claim_reading(row["id"], owner_id=OWNER, now=101)
            finally:
                db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            self.assertEqual(sum(item is not None for item in pool.map(worker, range(3))), 1)

    def test_source_withdrawal_pauses_inflight_and_reviewed_notes(self):
        row = self.complete(self.claim(self.reading()), note={"summary": "这一段描写了场景"}, note_status="reviewed")
        self.assertEqual(self.db.notes(row["id"], current_refs=[ref()])[0]["evidence_status"], "inferred")
        lease = self.claim(row, now=103)
        paused = self.db.check_reading(row["id"], owner_id=OWNER, version="v1", current_refs=[], available=False, now=104)
        self.assertEqual((paused["status"], paused["cursor"], paused["lease_token"]), ("paused", 3, ""))
        self.assertEqual(self.db.notes(row["id"], current_refs=[ref()]), [])
        with self.assertRaises(ValueError):
            self.complete(lease, now=105)
        with self.assertRaisesRegex(ValueError, "source_reference_changed"):
            self.db.resume_reading(row["id"], owner_id=OWNER, expected_revision=paused["revision"],
                                   version="v2", current_refs=[ref()], now=105)
        resumed = self.db.resume_reading(row["id"], owner_id=OWNER, expected_revision=paused["revision"],
                                       version="v1", current_refs=[ref()], now=105)
        self.assertEqual(resumed["cursor"], 3)

    def test_per_unit_refs_do_not_expand_resource_roots_and_invalid_notes_hide(self):
        row = self.complete(self.claim(self.reading()), source_refs=[ref(), ref("page-1")], note="第一段", note_status="reviewed")
        self.assertEqual(row["source_refs"], [ref()])
        self.assertEqual(self.db.notes(row["id"], current_refs=[ref()]), [])
        self.assertEqual(len(self.db.notes(row["id"], current_refs=[ref(), ref("page-1")])), 1)
        changed = ref("page-1", digest="f"*64)
        self.assertEqual(self.db.notes(row["id"], current_refs=[ref(), changed]), [])
        self.assertEqual(self.reading()["cursor"], 3)

    def test_reobserved_same_content_gets_new_progress_without_rebinding_old_notes(self):
        old = self.complete(self.claim(self.reading()), note="旧来源的笔记", note_status="reviewed")
        self.db.check_reading(old["id"], owner_id=OWNER, version="v1", current_refs=[], available=False, now=103)
        new_root = ref(digest="f"*64)
        reopened = self.reading(source_refs=[new_root], now=104)
        self.assertNotEqual(reopened["id"], old["id"])
        self.assertEqual((reopened["cursor"], reopened["status"]), (0, "active"))
        self.assertEqual(self.reading(source_refs=[new_root], now=105)["id"], reopened["id"])
        self.assertEqual(self.db.notes(old["id"], current_refs=[new_root]), [])
        frozen = self.db.reading(old["id"])
        self.assertEqual((frozen["cursor"], frozen["status"]), (3, "paused"))
        with self.assertRaisesRegex(ValueError, "source_reference_changed"):
            self.db.resume_reading(old["id"], owner_id=OWNER, expected_revision=frozen["revision"],
                                   version="v1", current_refs=[new_root], now=106)

    def test_review_latest_unit_does_not_rewrite_raw_content_or_progress(self):
        lease = self.claim(self.reading(total_units=3))
        row = self.complete(lease, note="待复核")
        self.assertEqual(self.db.notes(row["id"], current_refs=[ref()]), [])
        unit = self.db.units(row["id"])[0]
        reviewed = self.db.review_unit(row["id"], unit["id"], owner_id=OWNER, expected_revision=row["revision"],
                                      note={"summary": "复核后的笔记", "quotes": ["甲"]}, note_status="reviewed", current_refs=[ref()], now=103)
        self.assertEqual((reviewed["cursor"], reviewed["status"]), (3, "completed"))
        self.assertEqual(self.db.units(row["id"])[0]["excerpt"], unit["excerpt"])
        self.assertEqual(self.complete(lease, note="待复核")["revision"], reviewed["revision"])
        self.assertEqual(len(self.db.notes(row["id"], current_refs=[ref()])), 1)

    def test_unfounded_quotes_and_derived_sources_are_rejected(self):
        lease = self.claim(self.reading())
        for values in ({"note": {"quotes": ["原文里没有这一句"]}},
                       {"source_refs": [{**ref(), "id": "growth:knowledge:123"}]},
                       {"source_refs": [ref(digest="f"*64)]},
                       {"note_status": "approved"}, {"excerpt": "a"*12001}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.complete(lease, **values)
        self.assertEqual(self.db.units(lease["id"]), [])

    def test_reviewed_notes_preserve_supported_multiline_quotes_and_reject_other_controls(self):
        lease = self.claim(self.reading())
        note = {"summary": "第一行\n第二行", "questions": ["如何理解\t这里？"], "quotes": ["甲\n乙"],
                "self_response": "感受\r\n继续观察"}
        row = self.complete(lease, note=note, note_status="reviewed")
        self.assertEqual(self.db.notes(row["id"], current_refs=[ref()])[0]["note"], note)
        for marker in ("\0", "\x01", "\x1b"):
            with self.assertRaises(ValueError):
                self.db.review_unit(row["id"], "unit-0", owner_id=OWNER, expected_revision=row["revision"],
                                    note={"summary": "bad"+marker}, current_refs=[ref()])

    def test_unit_and_cursor_commit_together(self):
        lease = self.claim(self.reading())
        self.db.con.execute("CREATE TRIGGER reject_progress BEFORE UPDATE ON companion_readings "
                            "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.complete(lease)
        self.assertEqual(self.db.units(lease["id"]), [])
        self.assertEqual(self.db.reading(lease["id"])["cursor"], 0)

    def test_plan_requires_acceptance_and_every_transition_is_audited(self):
        row = self.propose()
        self.assertEqual(row["status"], "proposed")
        with self.assertRaisesRegex(ValueError, "invalid_plan_transition"):
            self.command(row, "complete")
        for action, status in (("accept", "active"), ("pause", "paused"), ("resume", "active"), ("complete", "completed")):
            row = self.command(row, action)
            self.assertEqual(row["status"], status)
        self.assertEqual(len(self.db.history(row["id"])), 5)
        self.reopen()
        self.assertEqual(self.db.plan(row["id"]), row)
        with self.assertRaisesRegex(ValueError, "invalid_plan_transition"):
            self.command(row, "edit", title="修改已完成计划")

    def test_plan_replays_are_idempotent_and_conflicting_message_is_rejected(self):
        proposal = self.propose()
        active = self.command(proposal)
        self.assertEqual(self.command(proposal), active)
        self.assertEqual(self.propose(), active)
        with self.assertRaisesRegex(ValueError, "idempotency_conflict"):
            self.command(active, "cancel", message_id="command-1")
        with self.assertRaisesRegex(ValueError, "revision_conflict"):
            self.command(proposal, "cancel", message_id="new-command")
        self.assertEqual(len(self.db.history(active["id"])), 2)
        self.assertNotIn("trusted-message-1", json.dumps(self.db.history(active["id"])))

    def test_plan_cannot_transfer_to_new_owner_and_reads_keep_internal_unity(self):
        row = self.propose()
        with self.assertRaisesRegex(ValueError, "owner_mismatch"):
            self.command(row, owner_id=OTHER)
        self.propose(owner_id=OTHER)
        self.assertEqual(len(self.db.plans()), 2)
        self.assertEqual(len(self.db.plans(owner_id=OWNER)), 1)
        self.assertEqual(self.db.plan(row["id"])["status"], "proposed")

    def test_plan_edits_and_cancel_do_not_imply_acceptance(self):
        row = self.propose()
        edited = self.command(row, "edit", title="改为一起看画集", next_step="看第一页", due_at=1000)
        self.assertEqual(edited["status"], "proposed")
        cancelled = self.command(edited, "cancel")
        self.assertEqual(cancelled["status"], "cancelled")

    def test_due_filter_precedes_limit_and_keeps_fractional_deadlines(self):
        oldest = self.command(self.propose(due_at=200.0005))
        for index in range(55):
            future = self.propose(message_id="future-"+str(index), due_at=10000, now=200+index)
            self.command(future, message_id="future-accept-"+str(index), now=300+index)
        self.assertEqual(self.db.plans(owner_id=OWNER, status="active", due_before=200.0004, oldest=True, limit=1), [])
        selected = self.db.plans(owner_id=OWNER, status="active", due_before=200.0005, oldest=True, limit=1)
        self.assertEqual([row["id"] for row in selected], [oldest["id"]])

    def test_plan_and_audit_are_atomic(self):
        row = self.propose()
        self.db.con.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON companion_audit "
                            "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.command(row)
        self.assertEqual(self.db.plan(row["id"]), row)
        self.assertEqual(len(self.db.history(row["id"])), 1)

    def test_competing_plan_commands_preserve_one_revision_and_audit(self):
        row = self.propose()
        barrier = threading.Barrier(3)
        def worker(index):
            db = CompanionStore(self.path)
            try:
                barrier.wait(timeout=10)
                try:
                    return db.command_plan(row["id"], owner_id=OWNER, action="accept", expected_revision=1,
                                           message_id="competing-"+str(index), summary_hash=HASH, now=101)["status"]
                except ValueError as error:
                    return str(error)
            finally:
                db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(worker, range(3)))
        self.assertEqual(results.count("active"), 1)
        self.assertEqual(results.count("revision_conflict"), 2)
        self.assertEqual(len(self.db.history(row["id"])), 2)

    def test_followup_is_persistent_does_not_complete_or_revise_plan(self):
        row = self.command(self.propose(due_at=200))
        kwargs = dict(owner_id=OWNER, expected_revision=row["revision"], interval_seconds=60)
        self.assertFalse(self.db.claim_plan_followup(row["id"], **kwargs, now=199))
        self.assertTrue(self.db.claim_plan_followup(row["id"], **kwargs, now=200))
        self.reopen()
        self.assertFalse(self.db.claim_plan_followup(row["id"], **kwargs, now=259))
        self.assertTrue(self.db.claim_plan_followup(row["id"], **kwargs, now=260))
        self.assertEqual(self.db.plan(row["id"])["revision"], row["revision"])
        self.assertEqual(self.db.plan(row["id"])["status"], "active")

    def test_reading_budget_persists_cooldown_failures_and_clock_regression(self):
        args = dict(owner_id=OWNER, day_start=0, interval_seconds=60, daily_limit=2)
        self.assertTrue(self.db.reserve_reading_cycle(**args, now=100))
        self.reopen()
        self.assertFalse(self.db.reserve_reading_cycle(**args, now=159))
        self.assertTrue(self.db.reserve_reading_cycle(**args, now=160))
        self.assertFalse(self.db.reserve_reading_cycle(**args, now=300))
        self.assertTrue(self.db.reserve_reading_cycle(**{**args, "day_start": 86400}, now=86500))
        self.assertFalse(self.db.reserve_reading_cycle(**args, now=400))
        self.assertEqual(self.db.status()["reading_budget"]["daily_count"], 1)

    def test_concurrent_reading_budget_has_one_winner(self):
        barrier = threading.Barrier(3)
        def worker(_):
            db = CompanionStore(self.path)
            try:
                barrier.wait(timeout=10)
                return db.reserve_reading_cycle(owner_id=OWNER, day_start=0, interval_seconds=0, daily_limit=1, now=100)
            finally:
                db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            self.assertEqual(sum(pool.map(worker, range(3))), 1)

    def test_invalid_inputs_do_not_create_plans(self):
        for values in ({"owner_id": ""}, {"due_at": float("nan")}, {"due_at": True},
                       {"summary_hash": "x"}, {"message_id": ""}, {"title": "x"*241}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.propose(**values)
        self.assertEqual(self.db.plans(), [])

    def test_snapshot_accepts_old_nine_ledger_backup_and_checks_present_companion(self):
        for count in (6, 9):
            old = Path(self.temp.name)/("old-"+str(count))
            old.mkdir()
            legacy_backup(old, count)
            rows, missing = snapshot(old)
            self.assertEqual(missing, [])
            self.assertTrue(all(rows[table] == [] for table in LEDGERS["companion.sqlite3"]))
            self.assertFalse((old/"companion.sqlite3").exists())
        self.propose()
        rows, _ = snapshot(Path(self.temp.name))
        self.assertEqual(len(rows["companion_plans"]), 1)
        self.db.con.execute("DROP TABLE companion_units")
        with self.assertRaisesRegex(RuntimeError, "companion.sqlite3 缺少预期表 companion_units"):
            snapshot(Path(self.temp.name))


if __name__ == "__main__":
    unittest.main()
