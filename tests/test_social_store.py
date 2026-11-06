"""The same social-window, private-help and public-send contracts on both backends."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import unittest

from MaiLivingWorld.tool_store import ToolStore
from MaiLivingWorld.tests import test_tool_mysql_live as db_fixture


def fingerprint(label):
    return hashlib.sha256(label.encode()).hexdigest()


SCOPE, OTHER = fingerprint("owner,instance"), fingerprint("other owner,instance")
ACCOUNT = fingerprint("instance,verified account")


class SocialStoreChecks:
    def test_window_survives_restart_with_cooldown_daily_limit_and_clock_rollback(self):
        db = self.store()
        self.assertEqual(db.claim_social_window(SCOPE, 0, now=0), 1)
        second = self.store()
        self.assertEqual(second.claim_social_window(SCOPE, 0, now=10799), 0)
        for stamp in (10800, 21600, 32400):
            self.assertEqual(second.claim_social_window(SCOPE, 0, now=stamp), stamp//10800 + 1)
        self.assertEqual(db.claim_social_window(SCOPE, 0, now=43200), 0)
        self.assertEqual(db.claim_social_window(OTHER, 0, now=43200), 1)
        self.assertEqual(db.claim_social_window(SCOPE, 86400, now=86400), 5)
        self.assertEqual(second.claim_social_window(SCOPE, 0, now=86000), 0)
        self.assertEqual(db.status()["call_budget"]["count"], 0)

    def test_help_tracks_each_reason_without_disclosing_other_owners_or_resetting_cooldown(self):
        db = self.store()
        second = self.store()
        denied, missing = "social_permission_required", "social_search_key_required"
        db.social_help(SCOPE, denied, now=10)
        second.social_help(SCOPE, missing, now=11)
        self.assertEqual(second.take_social_help(OTHER, now=12), "")
        self.assertEqual(second.take_social_help(SCOPE, now=12, consume=False), denied)
        self.assertEqual(db.take_social_help(SCOPE, now=12), denied)
        db.social_help(SCOPE, denied, now=13)
        self.assertEqual(second.take_social_help(SCOPE, now=14), missing)
        self.assertEqual(db.take_social_help(SCOPE, now=15), "")
        db.social_help(SCOPE, None, now=18, clear_code=denied)
        db.social_help(SCOPE, denied, now=19)
        self.assertEqual(second.take_social_help(SCOPE, now=20), "")
        self.assertEqual(second.take_social_help(SCOPE, now=86412), denied)
        db.social_help(SCOPE, "", now=86413)
        self.assertEqual(second.take_social_help(SCOPE, now=200000), "")
        with self.assertRaises(ValueError):
            db.social_help(SCOPE, "raw provider error with token")

    def test_social_reply_uses_both_budgets_and_an_uncertain_or_failed_post_cannot_replay(self):
        db = self.store()
        second = self.store()
        first = db.reserve_social_reply(ACCOUNT, fingerprint("post1"), 0, 3, now=12)
        self.assertTrue(first["allowed"])
        db.complete_call(first["id"], "uncertain", now=13)
        retry = second.reserve_social_reply(ACCOUNT, fingerprint("post1"), 0, 3, now=14)
        self.assertEqual(retry, {"id": first["id"], "allowed": False, "status": "uncertain"})
        failed = second.reserve_social_reply(ACCOUNT, fingerprint("post2"), 0, 3, now=15)
        self.assertTrue(failed["allowed"])
        second.complete_call(failed["id"], "failed", now=16)
        self.assertEqual(db.reserve_social_reply(ACCOUNT, fingerprint("post3"), 0, 3, now=17)["status"], "social_daily_limit")
        self.assertTrue(db.reserve_social_reply(OTHER, fingerprint("post1"), 0, 3, now=17)["allowed"])
        self.assertEqual(db.reserve_call("pixiv", "read", fingerprint("p"), "k", "private:12345678", "12345678",
                                         0, 3, now=17)["status"], "budget_exhausted")
        self.assertEqual(db.reserve_social_reply(OTHER, fingerprint("post4"), 0, 3, now=18)["status"], "budget_exhausted")
        self.assertEqual(db.reserve_social_reply(ACCOUNT, fingerprint("post2"), 86400, 3, now=86420)["status"], "failed")
        self.assertTrue(db.reserve_social_reply(ACCOUNT, fingerprint("post3"), 86400, 3, now=86420)["allowed"])
        self.assertEqual(second.reserve_social_reply(OTHER, fingerprint("post4"), 0, 4, now=99)["status"], "budget_exhausted")
        invalid = second.reserve_call("mastodon", "mastodon_reply", fingerprint("arbitrary content"), "other", "group:12345678",
                                      "12345678", 86400, 4, now=86420)
        self.assertEqual(invalid["status"], "social_reservation_required")

    def test_competing_connections_get_one_window_and_one_target_claim(self):
        self.store()
        barrier = threading.Barrier(2)

        def claim():
            db = self.store()
            try:
                barrier.wait(timeout=30)
                window = db.claim_social_window(SCOPE, 0, now=20)
                ticket = db.reserve_social_reply(ACCOUNT, fingerprint("post"), 0, 2, now=21)
                return window, ticket["allowed"]
            finally:
                # sqlite connections must close on the thread that created them.
                self.stores.remove(db)
                db.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(claim) for _ in range(2)]
            results = [f.result(timeout=50) for f in futures]
        self.assertEqual(sorted(r[0] for r in results), [0, 1])
        self.assertEqual(sorted(r[1] for r in results), [False, True])


class SocialSQLiteTests(SocialStoreChecks, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "social.sqlite3"
        self.stores = []

    def store(self):
        db = ToolStore(self.path)
        self.stores.append(db)
        return db

    def tearDown(self):
        for db in self.stores:
            db.close()
        self.tmp.cleanup()


@unittest.skipUnless(os.getenv("MLW_LIVE_MYSQL") == "1", "requires disposable MySQL CI")
class SocialMySQLTests(SocialStoreChecks, unittest.TestCase):
    setUp = db_fixture.ToolMySQLLiveTests.setUp
    tearDown = db_fixture.ToolMySQLLiveTests.tearDown
    store = db_fixture.ToolMySQLLiveTests.store
