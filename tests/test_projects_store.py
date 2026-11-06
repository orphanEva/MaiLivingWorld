"""Persistent project states, source references, limits and concurrent lease ownership."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from MaiLivingWorld.projects_store import (
    MAX_PROJECT_SOURCES, MAX_SEED_SOURCES, ProjectsStore, digest, references, seed_value,
)
from MaiLivingWorld.growth_limits import MAX_INTEREST_SOURCES

OWNER, OTHER = "12345678", "87654321"


def reference(letter="a"):
    return {"id": "tool:observation:"+letter*64, "digest": "b"*64, "scope": "private:"+OWNER, "sensitivity": "personal"}


def seed(key="a"):
    return {"question_key": "q:"+key*24, "interest_key": "i:"+"d"*24,
            "topic": "天文作品", "question": "这些天文作品提供了哪些资料？", "seed_refs": [reference()], "source_signature": "e"*64}


def long_seed():
    refs = [{"id": "tool:observation:"+digest(["project", index]), "digest": digest(["source", index]),
             "scope": "private:"+OWNER, "sensitivity": "personal"}
            for index in range(MAX_SEED_SOURCES)]
    refs[-1] = {**refs[-1], "scope": "private:"+OTHER, "sensitivity": "confidential"}
    return {**seed(), "seed_refs": refs, "interest_refs": refs[:MAX_INTEREST_SOURCES]}


def result(outcome="supported", letter="c"):
    return {"outcome": outcome, "conclusion": "这份资料列有天文主题。", "quotes": [{"source_id": reference(letter)["id"], "quote": "天文主题"}]}


class ProjectsChecks:
    def test_adaptive_plan_retains_sources_and_finishes_after_three_reviews(self):
        db = self.store()
        row = self.created(db)
        stamp = 101
        for index, letter in enumerate(("c", "d", "e")):
            claim = self.claimed(db, row, now=stamp, daily_limit=12)
            self.assertTrue(db.finish(OWNER, row["id"], claim["lease_token"], claim["revision"],
                action="gathered", evidence_refs=[reference(letter)], now=stamp+1, adaptive=True))
            stamp += 301
            claim = self.claimed(db, row, now=stamp, daily_limit=12)
            value = {**result("insufficient", letter), "next_step": {
                "question": "还需要核查第"+str(index+1)+"项资料？", "evidence_needed": "有出处的正文节选"}}
            self.assertTrue(db.finish(OWNER, row["id"], claim["lease_token"], claim["revision"],
                action="reviewed", result=value, now=stamp+1, adaptive=True))
            row = self.store().get(OWNER, row["id"])
            self.assertEqual(len(row["plan"]["rounds"]), index+1)
            self.assertEqual(len(row["evidence_refs"]), index+1)
            self.assertEqual(row["status"], "active" if index < 2 else "unresolved")
            stamp += 301
        self.assertEqual(row["reason"], "plan_needs_help")
        self.assertEqual({ref["id"] for ref in row["evidence_refs"]}, {reference(x)["id"] for x in ("c", "d", "e")})

    def test_adaptive_plan_audit_failure_rolls_back_and_owner_stop_wins(self):
        db = self.store()
        row = self.gathered(db)
        claim = self.claimed(db, row, now=403)
        value = {**result("insufficient"), "next_step": {"question": "正文还有哪些证据？", "evidence_needed": "下一份正文"}}
        with patch.object(db, "_audit", side_effect=RuntimeError("synthetic failure")):
            with self.assertRaises(RuntimeError):
                db.finish(OWNER, row["id"], claim["lease_token"], claim["revision"],
                          action="reviewed", result=value, now=404, adaptive=True)
        self.assertNotIn("plan", db.get(OWNER, row["id"]))
        self.assertTrue(db.command(OWNER, row["id"], claim["revision"], "stop", "a"*64, "b"*64, now=405))
        self.assertFalse(db.finish(OWNER, row["id"], claim["lease_token"], claim["revision"],
                                  action="reviewed", result=value, now=406, adaptive=True))

    def created(self, db, **options):
        return db.create(OWNER, seed(options.pop("key", "a")), now=options.pop("now", 100), **options)

    def claimed(self, db, row, now=101, **options):
        return db.claim(OWNER, [row["id"]], day_start=0, interval_seconds=300, now=now, **options)

    def gathered(self, db):
        row = self.created(db)
        claim = self.claimed(db, row)
        self.assertTrue(db.finish(OWNER, row["id"], claim["lease_token"], claim["revision"], action="gathered", evidence_refs=[reference("c")], now=102))
        return db.get(OWNER, row["id"])

    def test_two_stage_completion_and_restart_preserve_original_sources(self):
        db = self.store()
        row = self.gathered(db)
        self.assertEqual((row["status"], row["stage"]), ("active", "review"))
        reader = self.store()
        claim = self.claimed(reader, row, now=403)
        self.assertTrue(reader.finish(OWNER, row["id"], claim["lease_token"], claim["revision"], action="reviewed", result=result(), now=404))
        completed = db.get(OWNER, row["id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["seed"]["seed_refs"], [reference()])
        self.assertEqual(completed["result"], result())
        self.assertEqual(self.created(db, now=405)["status"], "completed")
        self.assertIsNone(db.get(OTHER, row["id"]))

    def test_long_seed_and_separate_evidence_survive_restart_and_completion(self):
        db = self.store()
        source = long_seed()
        canonical = seed_value(source)
        row = db.create(OWNER, source, now=100)
        self.assertEqual(len(row["seed"]["seed_refs"]), 184)
        self.assertEqual(len(row["seed"]["interest_refs"]), 160)
        reader = self.store()
        self.assertEqual(reader.get(OWNER, row["id"])["seed"], canonical)
        evidence = [{**reference(), "id": "tool:observation:"+digest(["evidence", index])}
                    for index in range(24)]
        claim = self.claimed(reader, row)
        self.assertTrue(reader.finish(OWNER, row["id"], claim["lease_token"], claim["revision"],
                                      action="gathered", evidence_refs=evidence, now=102))
        gathered = db.get(OWNER, row["id"])
        self.assertEqual(len(references(gathered["seed"]["seed_refs"]+gathered["evidence_refs"],
                                        limit=MAX_PROJECT_SOURCES)), 208)
        conclusion = {**result(), "quotes": [{"source_id": evidence[0]["id"], "quote": "天文主题"}]}
        review = self.claimed(db, gathered, now=403)
        self.assertTrue(db.finish(OWNER, row["id"], review["lease_token"], review["revision"],
                                  action="reviewed", result=conclusion, now=404))
        completed = reader.get(OWNER, row["id"])
        self.assertEqual((completed["status"], completed["seed"]), ("completed", canonical))
        self.assertEqual(completed["evidence_refs"], references(evidence))
        self.assertTrue(any(ref["scope"] == "private:"+OTHER and ref["sensitivity"] == "confidential"
                            for ref in completed["seed"]["seed_refs"]))

    def test_long_seed_rollback_on_audit_failure_keeps_the_retry_complete(self):
        db = self.store()
        source = long_seed()
        with patch.object(db, "_audit", side_effect=RuntimeError("synthetic audit failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic audit failure"):
                db.create(OWNER, source, now=100)
        self.assertEqual(db.items(OWNER), [])
        self.assertEqual(db.state(OWNER)["revision"], 0)
        row = db.create(OWNER, source, now=101)
        self.assertEqual(row["seed"], seed_value(source))
        self.assertEqual(len(db.items(OWNER)), 1)

    def test_source_limits_and_interest_membership_are_validated_before_writes(self):
        db = self.store()
        source = long_seed()
        extra = {**reference(), "id": "tool:observation:"+digest("extra")}
        wrong_ref = {**source["interest_refs"][0], "digest": "f"*64}
        invalid = [
            {**source, "seed_refs": source["seed_refs"]+[extra]},
            {**source, "interest_refs": source["seed_refs"][:MAX_INTEREST_SOURCES+1]},
            {**source, "interest_refs": []},
            {**source, "interest_refs": [extra]},
            {**source, "interest_refs": [wrong_ref]},
            {**source, "unknown": "unexpected"},
        ]
        for proposed in invalid:
            with self.subTest(proposed=proposed.keys()), self.assertRaises(ValueError):
                db.create(OWNER, proposed, now=100)
        self.assertEqual(db.items(OWNER), [])
        self.assertEqual(db.state(OWNER)["revision"], 0)
        legacy = db.create(OWNER, seed(), now=101)
        self.assertEqual(legacy["seed"], seed())
        self.assertNotIn("interest_refs", legacy["seed"])

    def test_default_reference_and_evidence_limits_stay_bounded(self):
        refs = long_seed()["seed_refs"]
        self.assertEqual(len(references(refs[:24])), 24)
        for limit in (True, 0, -1, MAX_PROJECT_SOURCES+1, 24.0):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                references(refs[:1], limit=limit)
        with self.assertRaises(ValueError):
            references(refs[:25])
        self.assertEqual(len(references(refs, limit=MAX_SEED_SOURCES)), 184)
        self.assertEqual(references([], empty=True), [])
        db = self.store()
        row = self.created(db)
        claim = self.claimed(db, row)
        with self.assertRaises(ValueError):
            db.finish(OWNER, row["id"], claim["lease_token"], claim["revision"],
                      action="gathered", evidence_refs=refs[:25], now=102)
        self.assertEqual(db.get(OWNER, row["id"])["stage"], "gather")

    def test_cannot_complete_without_evidence_or_with_unbound_quotes(self):
        db = self.store()
        row = self.created(db)
        claim = self.claimed(db, row)
        with self.assertRaises(ValueError):
            db.finish(OWNER, row["id"], claim["lease_token"], claim["revision"], action="reviewed", result=result(), now=102)
        db.finish(OWNER, row["id"], claim["lease_token"], claim["revision"], action="gathered", evidence_refs=[reference("c")], now=102)
        claim = self.claimed(db, row, now=403)
        with self.assertRaises(ValueError):
            db.finish(OWNER, row["id"], claim["lease_token"], claim["revision"], action="reviewed", result=result(letter="d"), now=404)
        self.assertEqual(db.get(OWNER, row["id"])["status"], "active")

    def test_single_owner_lease_expiry_blocks_old_worker(self):
        db = self.store()
        first, second = self.created(db), self.created(db, key="c")
        claim = self.claimed(db, first, lease_seconds=600)
        other = self.store()
        self.assertIsNone(self.claimed(other, second, now=501))
        takeover = self.claimed(other, first, now=702)
        self.assertNotEqual(takeover["lease_token"], claim["lease_token"])
        self.assertFalse(db.finish(OWNER, first["id"], claim["lease_token"], claim["revision"], action="gathered", evidence_refs=[reference("c")], now=703))
        self.assertTrue(other.finish(OWNER, first["id"], takeover["lease_token"], takeover["revision"], action="gathered", evidence_refs=[reference("c")], now=703))

    def test_commands_are_cas_idempotent_and_stop_never_auto_revives(self):
        db = self.store()
        row = self.created(db)
        self.assertTrue(db.command(OWNER, row["id"], row["revision"], "pause", digest("m1"), digest("pause"), now=101))
        paused = db.get(OWNER, row["id"])
        self.assertFalse(db.command(OWNER, row["id"], row["revision"], "resume", digest("m2"), digest("resume"), now=102))
        self.assertTrue(db.command(OWNER, row["id"], paused["revision"], "resume", digest("m2"), digest("resume"), now=102))
        self.assertFalse(db.command(OWNER, row["id"], row["revision"], "pause", digest("m1"), digest("pause"), now=103))
        current = db.get(OWNER, row["id"])
        self.assertTrue(db.command(OWNER, row["id"], current["revision"], "stop", digest("m3"), digest("stop"), now=104))
        self.assertEqual(self.created(db, now=105)["status"], "stopped")
        with self.assertRaises(ValueError):
            db.command(OWNER, row["id"], current["revision"], "stop", digest("m1"), digest("other body"), now=105)

    def test_source_pause_revokes_lease_and_does_not_reset_budget(self):
        db = self.store()
        row = self.created(db)
        claim = self.claimed(db, row, daily_limit=1)
        self.assertTrue(db.pause_invalid(OWNER, row["id"], claim["revision"], now=102))
        self.assertFalse(db.finish(OWNER, row["id"], claim["lease_token"], claim["revision"], action="gathered", evidence_refs=[reference("c")], now=103))
        self.assertEqual(db.state(OWNER)["daily_actions"], 1)
        paused = db.get(OWNER, row["id"])
        db.command(OWNER, row["id"], paused["revision"], "resume", digest("resume"), digest("cmd"), now=104)
        self.assertIsNone(self.claimed(db, row, now=1000, daily_limit=1))

    def test_daily_budget_invalid_candidates_and_restart_do_not_spend_twice(self):
        db = self.store()
        row = self.created(db)
        self.assertIsNone(db.claim(OWNER, [], day_start=0, now=101))
        self.assertEqual(db.state(OWNER)["daily_actions"], 0)
        self.assertIsNone(self.claimed(db, row, daily_limit=0))
        claim = self.claimed(db, row, daily_limit=1)
        db.finish(OWNER, row["id"], claim["lease_token"], claim["revision"], action="deferred", now=102)
        reader = self.store()
        self.assertIsNone(self.claimed(reader, row, now=403, daily_limit=1))
        next_day = reader.claim(OWNER, [row["id"]], day_start=86400, daily_limit=1, interval_seconds=300, now=86500)
        self.assertIsNotNone(next_day)
        self.assertEqual(reader.state(OWNER)["daily_actions"], 1)

    def test_no_material_does_not_invent_a_result_and_is_finite(self):
        db = self.store()
        row = self.created(db)
        for now in (101, 1000, 2400):
            claim = self.claimed(db, row, now=now)
            db.finish(OWNER, row["id"], claim["lease_token"], claim["revision"], action="no_evidence", now=now+1)
        current = db.get(OWNER, row["id"])
        self.assertEqual(current["status"], "unresolved")
        self.assertEqual(current["result"], {})
        self.assertEqual(current["stage"], "gather")

    def test_age_limit_and_active_capacity_are_real_bounds(self):
        db = self.store()
        row = self.created(db, max_active=1)
        self.assertIsNone(self.created(db, key="c", max_active=1))
        self.assertIsNone(db.claim(OWNER, [row["id"]], day_start=14*86400, now=14*86400+101))
        self.assertEqual(db.get(OWNER, row["id"])["status"], "unresolved")
        self.assertEqual(db.state(OWNER)["daily_actions"], 0)


class ProjectsStoreTests(ProjectsChecks, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"projects.sqlite3"
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.close()
        self.temp.cleanup()

    def store(self):
        store = ProjectsStore(self.path)
        self.stores.append(store)
        return store
