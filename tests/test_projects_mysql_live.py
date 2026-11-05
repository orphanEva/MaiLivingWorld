"""Disposable MySQL project CAS/budget checks, explicitly opted in by CI."""
import concurrent.futures
import os
from pathlib import Path
import tempfile
import threading
import unittest
import uuid

from MaiLivingWorld.projects_store import ProjectsStore, digest
from test_projects_store import ProjectsChecks, OWNER, reference, result, seed


@unittest.skipUnless(os.getenv("MLW_LIVE_MYSQL") == "1", "Only with ephemeral MySQL CI")
class ProjectsMySQLLiveTests(ProjectsChecks, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"unused.sqlite3"
        self.settings = {"host": os.environ["MLW_MYSQL_HOST"], "port": int(os.getenv("MLW_MYSQL_PORT", "3306")),
                         "user": os.environ["MLW_MYSQL_USER"], "password": os.environ["MLW_MYSQL_PASSWORD"],
                         "database": os.environ["MLW_MYSQL_DATABASE"], "table_prefix": "prj_"+uuid.uuid4().hex[:10]+"_"}
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.close()
        import pymysql
        connection = pymysql.connect(**{key: value for key, value in self.settings.items() if key != "table_prefix"}, charset="utf8mb4", autocommit=True)
        try:
            with connection.cursor() as cursor:
                for table in ("projects_audit", "projects_items", "projects_control"):
                    cursor.execute("DROP TABLE IF EXISTS `"+self.settings["table_prefix"]+table+"`")
        finally:
            connection.close()
            self.temp.cleanup()

    def store(self):
        store = ProjectsStore(self.path, mysql_settings=self.settings)
        self.stores.append(store)
        return store

    def race(self, action):
        barrier = threading.Barrier(3)
        def invoke(index):
            db = ProjectsStore(self.path, mysql_settings=self.settings)
            try:
                barrier.wait(timeout=10)
                return action(db, index)
            finally:
                db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            return list(pool.map(invoke, range(3)))

    def test_concurrent_selection_is_deduplicated_and_capacity_limited(self):
        db = self.store()
        selected = self.race(lambda worker, index: worker.create(OWNER, seed("abc"[index]), max_active=1, now=100))
        self.assertEqual(sum(row is not None for row in selected), 1)
        self.assertEqual(len(db.items(OWNER)), 1)

    def test_concurrent_claims_spend_one_action_and_one_review_commits(self):
        db = self.store()
        row = self.gathered(db)
        claims = self.race(lambda worker, index: worker.claim(OWNER, [row["id"]], day_start=0,
            daily_limit=2, interval_seconds=300, now=403))
        self.assertEqual(sum(claim is not None for claim in claims), 1)
        claim = next(claim for claim in claims if claim)
        self.assertEqual(db.state(OWNER)["daily_actions"], 2)
        finished = self.race(lambda worker, index: worker.finish(OWNER, row["id"], claim["lease_token"],
            claim["revision"], action="reviewed", result=result(), now=404))
        self.assertEqual(sum(finished), 1)
        self.assertEqual(db.get(OWNER, row["id"])["status"], "completed")

    def test_concurrent_owner_pause_revokes_only_the_current_lease_once(self):
        db = self.store()
        row = self.created(db)
        claim = self.claimed(db, row)
        applied = self.race(lambda worker, index: worker.command(OWNER, row["id"], claim["revision"],
            "pause", digest(["receipt", index]), digest("pause"), now=102))
        self.assertEqual(sum(applied), 1)
        self.assertFalse(db.finish(OWNER, row["id"], claim["lease_token"], claim["revision"],
            action="gathered", evidence_refs=[reference("c")], now=103))
        self.assertEqual(db.get(OWNER, row["id"])["status"], "paused")
        self.assertEqual(db.state(OWNER)["lease_token"], "")
