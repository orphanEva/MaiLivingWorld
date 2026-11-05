"""Persistent project states, source references, limits and concurrent lease ownership."""
from pathlib import Path
import tempfile
import unittest

from MaiLivingWorld.projects_store import ProjectsStore, digest

OWNER, OTHER = "12345678", "87654321"


def reference(letter="a"):
    return {"id": "tool:observation:"+letter*64, "digest": "b"*64, "scope": "private:"+OWNER, "sensitivity": "personal"}


def seed(key="a"):
    return {"question_key": "q:"+key*24, "interest_key": "i:"+"d"*24,
            "topic": "天文作品", "question": "这些天文作品提供了哪些资料？", "seed_refs": [reference()], "source_signature": "e"*64}


def result(outcome="supported", letter="c"):
    return {"outcome": outcome, "conclusion": "这份资料列有天文主题。", "quotes": [{"source_id": reference(letter)["id"], "quote": "天文主题"}]}


class ProjectsChecks:
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
