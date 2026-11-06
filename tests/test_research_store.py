"""One source-version and capacity contract on the SQLite and MySQL research ledger."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from MaiLivingWorld.mcp_runtime import digest
from MaiLivingWorld.social_discovery import browse_receipt
from MaiLivingWorld.tool_store import ToolStore
from MaiLivingWorld.tests import test_tool_mysql_live as db_fixture


OWNER, OTHER = "12345678", "87654321"
URL = "https://www.ithome.com/0/875/123.htm"
QUESTION, VERSION = digest("question"), digest("listing")


class ResearchStoreChecks:
    def read(self, db, *, provider="news", text="报道节选：望远镜", now=10):
        url = URL if provider == "news" else "https://zh.wikipedia.org/wiki/Telescope"
        record = db.put_observation(provider, "article", text, "public:"+provider,
                                    sensitivity="public", url=url, now=now)
        call = db.reserve_call(provider, "news_read" if provider == "news" else "reference_read",
                               digest([provider, now]), "research", "private:"+OWNER, OWNER,
                               0, 100, now=now)
        db.complete_call(call["id"], "succeeded", browse_receipt(record), now=now+1)
        return record, call["id"]

    def test_completed_read_excludes_exact_question_listing_and_version_after_restart(self):
        db = self.store()
        record, call = self.read(db)
        self.assertTrue(db.mark_research_read(OWNER, QUESTION, URL, VERSION, record, call_id=call, now=12))
        restarted = self.store()
        candidates = {URL: VERSION, "https://observatory.example/report": VERSION}
        with patch.object(restarted, "_read", wraps=restarted._read) as reads:
            self.assertEqual(restarted.new_research_candidates(OWNER, QUESTION, candidates, now=13),
                             {"https://observatory.example/report"})
            self.assertEqual(reads.call_count, 2)
        self.assertEqual(restarted.new_research_candidates(OWNER, digest("revised question"), {URL: VERSION}, now=13), {URL})
        self.assertEqual(restarted.new_research_candidates(OTHER, QUESTION, {URL: VERSION}, now=13), {URL})
        self.assertEqual(restarted.new_research_candidates(OWNER, QUESTION, {URL: digest("updated listing")}, now=13), {URL})
        self.assertEqual(restarted.new_research_candidates(OWNER, QUESTION, {URL: VERSION}, now=12+86400), {URL})
        self.assertEqual(restarted.new_research_candidates(OWNER, QUESTION, {URL: VERSION}, now=11), {URL})

    def test_missing_incomplete_or_other_owner_receipt_cannot_mark_read(self):
        db = self.store()
        record, call = self.read(db)
        for ticket in ("tool:call:"+digest("missing"), call):
            owner = OWNER if ticket != call else OTHER
            self.assertFalse(db.mark_research_read(owner, QUESTION, URL, VERSION, record, call_id=ticket, now=12))
        missing = db.reserve_call("news", "news_read", digest("missing observation"), "research", "private:"+OWNER,
                                 OWNER, 0, 100, now=13)
        db.complete_call(missing["id"], "succeeded", now=14)
        self.assertFalse(db.mark_research_read(OWNER, QUESTION, URL, VERSION, record, call_id=missing["id"], now=15))
        self.assertFalse(db.mark_research_read(OWNER, QUESTION, URL, VERSION, record, call_id=call, now=72))
        self.assertEqual(db.new_research_candidates(OWNER, QUESTION, {URL: VERSION}, now=15), {URL})

    def test_changed_or_withdrawn_original_is_eligible_again(self):
        db = self.store()
        record, call = self.read(db)
        db.mark_research_read(OWNER, QUESTION, URL, VERSION, record, call_id=call, now=12)
        db.put_observation("news", "article", "报道节选：更正观测", "public:news", sensitivity="public", url=URL, now=13)
        self.assertEqual(db.new_research_candidates(OWNER, QUESTION, {URL: VERSION}, now=14), {URL})
        self.assertFalse(db.mark_research_read(OWNER, QUESTION, URL, VERSION, record, call_id=call, now=14))
        db.retract_observation("news", "article", now=15)
        self.assertEqual(db.new_research_candidates(OWNER, QUESTION, {URL: VERSION}, now=16), {URL})

    def test_wikipedia_uses_the_same_validated_read_receipt(self):
        db = self.store()
        record, call = self.read(db, provider="reference", text="百科节选：望远镜")
        self.assertTrue(db.mark_research_read(OWNER, QUESTION, record["url"], VERSION, record, call_id=call, now=12))
        self.assertEqual(db.new_research_candidates(OWNER, QUESTION, {record["url"]: VERSION}, now=13), set())

    def test_markers_are_bounded_and_never_selected_as_regular_questions(self):
        db = self.store()
        record, call = self.read(db)
        for index in range(258):
            self.assertTrue(db.mark_research_read(OWNER, digest(index), URL, VERSION, record,
                                                call_id=call, now=12+index/1000))
        rows = db._read("SELECT question_key,status,lease_token FROM tool_research WHERE question_key>=? AND question_key<?",
                        ("__tool_research_read__:", "__tool_research_read__:~"))
        self.assertEqual(len(rows), 256)
        self.assertTrue(all(len(row[field]) <= 191 for row in rows for field in row))
        self.assertEqual(db.new_research_candidates(OWNER, digest(0), {URL: VERSION}, now=13), {URL})
        self.assertEqual(db.new_research_candidates(OWNER, digest(257), {URL: VERSION}, now=13), set())
        with self.assertRaises(ValueError):
            db.claim_research([rows[0]["question_key"]], interval_seconds=60, daily_limit=5, day_start=0, now=13)
        with self.assertRaises(ValueError):
            db.next_local_question([rows[0]["question_key"]])
        self.assertIsNotNone(db.claim_research(["q:ordinary"], interval_seconds=60, daily_limit=5, day_start=0, now=13))

    def test_candidate_arguments_are_bounded_and_validated_before_queries(self):
        db = self.store()
        with patch.object(db, "_read", wraps=db._read) as read:
            self.assertEqual(db.new_research_candidates(OWNER, QUESTION, {}, now=10), set())
            for candidates in ({URL+"?token=secret": VERSION}, {URL: "raw listing"},
                               {URL+str(index): VERSION for index in range(4)}):
                with self.assertRaises(ValueError):
                    db.new_research_candidates(OWNER, QUESTION, candidates, now=10)
            read.assert_not_called()

    def test_concurrent_markers_keep_the_newer_listing(self):
        db = self.store()
        record, call = self.read(db)
        barrier = threading.Barrier(2)
        def mark(stamp):
            connection = self.store()
            try:
                barrier.wait(timeout=30)
                connection.mark_research_read(OWNER, QUESTION, URL, digest(stamp), record, call_id=call, now=stamp)
            finally:
                self.stores.remove(connection)
                connection.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(mark, stamp) for stamp in (12, 20)]
            for future in futures:
                future.result(timeout=50)
        self.assertEqual(db.new_research_candidates(OWNER, QUESTION, {URL: digest(20)}, now=21), set())
        self.assertEqual(db.new_research_candidates(OWNER, QUESTION, {URL: digest(12)}, now=21), {URL})


class ResearchSQLiteTests(ResearchStoreChecks, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/"research.sqlite3"
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
class ResearchMySQLTests(ResearchStoreChecks, unittest.TestCase):
    setUp = db_fixture.ToolMySQLLiveTests.setUp
    tearDown = db_fixture.ToolMySQLLiveTests.tearDown
    store = db_fixture.ToolMySQLLiveTests.store
