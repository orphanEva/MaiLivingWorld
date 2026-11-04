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
from MaiLivingWorld.growth_store import GrowthStore
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
