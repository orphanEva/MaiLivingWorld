"""Growth transactions and migration against an explicitly enabled disposable MySQL."""
from __future__ import annotations

import concurrent.futures
import os
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from MaiLivingWorld.brain_core import BrainBook
from MaiLivingWorld.cognition import CognitiveStore
from MaiLivingWorld.experience_loop import ExperienceLoop
from MaiLivingWorld.growth_store import GrowthStore, MAX_INTEREST_SOURCES
from MaiLivingWorld.mind import MindStore
from MaiLivingWorld.proactive_feedback import ProactiveFeedback
from MaiLivingWorld.self_state import SelfState
from MaiLivingWorld.store import Store
from MaiLivingWorld.scripts.migrate_all_sqlite_to_mysql import ALL_TABLES, apply, snapshot


def evidence(prefix="A"):
    return [{"id": prefix+str(i), "text": f"{prefix}的第{i}项独立经历",
             "scope": "private:12345678", "subject_id": "12345678",
             "sensitivity": "personal", "evidence_status": "recorded_event",
             "source": "brain_event", "kind": "event", "timestamp": 100.0+i}
            for i in range(3)]


def proposal(rows):
    ids = [r["id"] for r in rows]
    return {"interests": [{"topic": "编程", "delta": .2, "evidence_ids": ids}],
            "traits": [{"trait": "curiosity", "delta": .5, "evidence_ids": ids}]}


@unittest.skipUnless(os.getenv("MLW_LIVE_MYSQL") == "1", "Only with ephemeral MySQL CI")
class GrowthMySQLLiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.settings = {
            "host": os.environ["MLW_MYSQL_HOST"],
            "port": int(os.getenv("MLW_MYSQL_PORT", "3306")),
            "user": os.environ["MLW_MYSQL_USER"],
            "password": os.environ["MLW_MYSQL_PASSWORD"],
            "database": os.environ["MLW_MYSQL_DATABASE"],
            "table_prefix": "gqa_"+uuid.uuid4().hex[:10]+"_",
        }
        self.stores = []

    def tearDown(self):
        for db in self.stores:
            db.close()
        # Each test owns its random prefix in the explicitly enabled CI database.
        import pymysql
        connection = pymysql.connect(
            **{k: v for k, v in self.settings.items() if k != "table_prefix"},
            charset="utf8mb4", autocommit=True)
        try:
            with connection.cursor() as cursor:
                # Schema lists parents before dependents; drop child tables first.
                for table in reversed(ALL_TABLES):
                    cursor.execute("DROP TABLE IF EXISTS `"+self.settings["table_prefix"]+table+"`")
        finally:
            connection.close()
            self.temp.cleanup()

    def store(self):
        db = GrowthStore(self.path / "mysql-placeholder.sqlite3", mysql_settings=self.settings)
        self.stores.append(db)
        return db

    def test_independent_connections_see_growth_and_reject_stale_revision(self):
        first, second = self.store(), self.store()
        self.assertEqual(first.con.execute("SELECT COUNT(*) FROM growth_state").fetchone()[0], 0)
        self.assertEqual(second.state(now=100)["revision"], 0)
        a, b = evidence(), evidence("B")
        aid, bid = first.enqueue(a, now=100), second.enqueue(b, now=101)
        ajob, bjob = first.claim(now=102), second.claim(now=102)
        self.assertEqual(first.complete(aid, ajob["token"], proposal(a), a,
                                        now=103, expected_revision=0)["revision"], 1)
        self.assertEqual(second.state(now=104)["interests"][0]["weight"], .1)
        with self.assertRaisesRegex(ValueError, "revision_conflict"):
            second.complete(bid, bjob["token"], proposal(b), b, now=104, expected_revision=0)
        self.assertEqual(second.status()["jobs"]["running"], 1)
        self.assertEqual(second.status()["consumed"], 3)
        second.complete(bid, bjob["token"], proposal(b), b, now=105, expected_revision=1)
        self.assertEqual(first.state(now=106)["revision"], 2)
        self.assertEqual(first.rollback(2, now=107)["interests"][0]["weight"], .1)
        self.assertEqual(second.status()["consumed"], 6)
        self.assertEqual(second.history()[0]["kind"], "rollback")

    def test_interest_change_history_survives_reconnect_and_honors_rollback(self):
        from MaiLivingWorld.growth_store import source_fingerprint
        from MaiLivingWorld.unified_memory import annotate_interest_changes, derived_memories
        db = self.store()
        roots = [{**row, "id": "brain:event:history"+str(index)}
                 for index, row in enumerate(evidence("history"))]
        for index in range(2):
            source = roots[index]
            stamp = 100 + index*10
            job_id = db.enqueue([source], now=stamp)
            job = db.claim(now=stamp)
            candidate = {"interests": [{"topic": "编程", "delta": 0.1, "evidence_ids": [source["id"]]}]}
            db.complete(job_id, job["token"], candidate, [source], now=stamp+1)
        reopened = self.store()
        resolved = {root["id"]: root for root in roots}
        records = derived_memories(reopened.state(now=120), resolved, fingerprint=source_fingerprint)
        changed = annotate_interest_changes(records, reopened.history(limit=20))
        self.assertIn("兴趣有所回升", changed[0]["text"])
        self.assertEqual(changed[0]["sources"], records[0]["sources"])
        db.rollback(2, now=121)
        records = derived_memories(reopened.state(now=122), resolved, fingerprint=source_fingerprint)
        self.assertEqual(annotate_interest_changes(records, reopened.history(limit=20)), records)

    def test_interest_overflow_commits_with_siblings_atomically_across_connections(self):
        from unittest.mock import patch
        from MaiLivingWorld.tests.test_growth_store import fill_interest_sources, interest, records
        db, peer = self.store(), self.store()
        original = fill_interest_sources(db, records("old-private-", MAX_INTEREST_SOURCES))
        before = peer.state(now=131)["revision"]
        new = records("fresh-", 2)
        proposal = {"interests": [*interest(new[:1])["interests"], *interest(new[1:], topic="天文")["interests"]]}
        jid = db.enqueue(new, now=140)
        job = db.claim(now=140)
        execute = db.con.execute
        def fail_history(sql, parameters=()):
            if sql.startswith("INSERT INTO growth_history"):
                raise RuntimeError("synthetic-history-failure")
            return execute(sql, parameters)
        with patch.object(db.con, "execute", side_effect=fail_history):
            with self.assertRaisesRegex(RuntimeError, "synthetic-history-failure"):
                db.complete(jid, job["token"], proposal, new, now=141, skip_interest_overflow=True)
        self.assertEqual(peer.state(now=142)["revision"], before)
        self.assertEqual(peer.status()["consumed"], MAX_INTEREST_SOURCES)
        self.assertEqual(peer.status()["jobs"]["running"], 1)
        result = db.complete(jid, job["token"], proposal, new, now=143, skip_interest_overflow=True)
        self.assertEqual((result["interests"], result["skipped_interest_overflow"]), (1, 1))
        self.assertEqual(peer.state(now=144)["interests"][0], original)
        self.assertEqual(peer.status()["consumed"], MAX_INTEREST_SOURCES+1)
        self.assertEqual([ref["id"] for ref in peer.history()[0]["sources"]], [new[1]["id"]])
        self.assertEqual(peer.rollback(result["revision"], now=145)["interests"], [original])

    def test_extended_interest_preserves_all_sources_across_revision_race_and_reconnect(self):
        from MaiLivingWorld.tests.test_growth_store import fill_interest_sources, interest, records
        db, peer = self.store(), self.store()
        original = fill_interest_sources(db, records("old-private-", 24))
        previous_revision = db.state(now=130)["revision"]
        first, second = records("later-", 2, "public")
        for row in (first, second):
            row.update(subject_id="", sensitivity="public")
        first_id = db.enqueue([first], now=140)
        second_id = peer.enqueue([second], now=141)
        first_job = db.claim(now=142)
        second_job = peer.claim(now=142)
        db.complete(first_id, first_job["token"], interest([first]), [first],
                    now=143, expected_revision=previous_revision)
        with self.assertRaisesRegex(ValueError, "revision_conflict"):
            peer.complete(second_id, second_job["token"], interest([second], -.02), [second],
                          now=144, expected_revision=previous_revision)
        extended = peer.state(now=145)["interests"][0]
        self.assertEqual(len(extended["sources"]), 25)
        self.assertTrue(all(ref in extended["sources"] for ref in original["sources"]))
        self.assertEqual(peer.status()["consumed"], 25)
        self.assertEqual(peer.status()["jobs"]["running"], 1)
        result = peer.complete(second_id, second_job["token"], interest([second], -.02), [second],
                               now=146, expected_revision=previous_revision+1)
        reopened = self.store()
        current = reopened.state(now=147)["interests"][0]
        self.assertEqual(len(current["sources"]), 26)
        self.assertAlmostEqual(current["weight"], extended["weight"]-.02)
        self.assertEqual(reopened.history()[0]["after"]["interests"], [current])
        self.assertEqual(reopened.rollback(result["revision"], now=148)["interests"], [extended])
        self.assertEqual(db.state(now=149)["interests"], [extended])
        self.assertEqual(db.status()["consumed"], 26)

    def test_racing_claims_and_expired_lease_recovery(self):
        db = self.store()
        rows = evidence()
        jid = db.enqueue(rows, now=100)
        barrier = threading.Barrier(2)

        def worker(_):
            store = GrowthStore(self.path / "worker.sqlite3", mysql_settings=self.settings)
            try:
                barrier.wait(timeout=10)
                return store.claim(now=100, lease_seconds=5)
            finally:
                store.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            claimed = [job for job in pool.map(worker, range(2)) if job]
        self.assertEqual(len(claimed), 1)
        next_job = db.claim(now=105)
        self.assertNotEqual(next_job["token"], claimed[0]["token"])
        with self.assertRaisesRegex(ValueError, "lease_lost"):
            db.complete(jid, claimed[0]["token"], proposal(rows), rows, now=106)
        db.complete(jid, next_job["token"], proposal(rows), rows, now=106)
        self.assertEqual(db.status()["jobs"]["done"], 1)

    def test_late_statement_failure_rolls_back_the_entire_growth_transaction(self):
        db = self.store()
        rows = evidence()
        jid = db.enqueue(rows, now=100)
        job = db.claim(now=100)
        execute = db.con.execute

        def fail_history(sql, parameters=()):
            if sql.startswith("INSERT INTO growth_history"):
                raise RuntimeError("synthetic-history-failure")
            return execute(sql, parameters)

        db.con.execute = fail_history
        try:
            with self.assertRaisesRegex(RuntimeError, "synthetic-history-failure"):
                db.complete(jid, job["token"], proposal(rows), rows, now=101)
        finally:
            db.con.execute = execute
        peer = self.store()
        self.assertEqual(peer.state(now=102)["revision"], 0)
        self.assertEqual(peer.status()["consumed"], 0)
        self.assertEqual(peer.status()["jobs"]["running"], 1)
        self.assertEqual(peer.history(), [])
        db.complete(jid, job["token"], proposal(rows), rows, now=103)

    def test_eight_ledger_migration_preserves_identity_and_resumable_growth(self):
        backup = self.path / "backup"
        backup.mkdir()
        for name, cls in (("living_world", Store), ("brain_book", BrainBook),
                          ("mind", MindStore), ("cognition", CognitiveStore),
                          ("experience_loop", ExperienceLoop), ("proactive_feedback", ProactiveFeedback)):
            ledger = cls(backup / (name+".sqlite3"))
            ledger.close()
        identity = SelfState(backup / "self_state.sqlite3")
        original = identity.configure_owner("12345678", now=100)
        identity.close()
        growth = GrowthStore(backup / "growth.sqlite3")
        a, b = evidence(), evidence("B")
        aid = growth.enqueue(a, now=100)
        job = growth.claim(now=100)
        growth.complete(aid, job["token"], proposal(a), a, now=101)
        bid = growth.enqueue(b, now=102)
        pending = growth.claim(now=102)
        before = growth.state(now=103)
        growth.close()
        rows, missing = snapshot(backup)
        self.assertEqual(missing, [])
        migrated = apply(rows, self.settings)
        self.assertEqual(migrated["growth_state"], 1)
        self.assertEqual(migrated["growth_history"], 1)
        self.assertEqual(migrated["growth_jobs"], 2)
        target = self.store()
        self.assertEqual(target.state(now=103), before)
        target.complete(bid, pending["token"], proposal(b), b, now=104, expected_revision=1)
        owner = SelfState(self.path / "owner-placeholder.sqlite3", mysql_settings=self.settings)
        try:
            self.assertEqual(owner.state()["agent_id"], original["agent_id"])
            self.assertEqual(owner.state()["owner_id"], "12345678")
        finally:
            owner.close()
        with self.assertRaisesRegex(ValueError, "目标表非空"):
            apply(rows, self.settings)


if __name__ == "__main__":
    unittest.main()
