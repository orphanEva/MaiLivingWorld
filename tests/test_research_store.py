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
from MaiLivingWorld.tool_store import PUBLIC_READ_ATTEMPT_SUMMARIES, ToolStore
from MaiLivingWorld.tests import test_tool_mysql_live as db_fixture


OWNER, OTHER = "12345678", "87654321"
URL = "https://www.ithome.com/0/875/123.htm"
QUESTION, VERSION = digest("question"), digest("listing")


class ResearchStoreChecks:
    def claim(self, db, now, *, version=QUESTION, interval=3600, daily_limit=100, key="q:public"):
        return db.claim_research([key], question_versions={key: version}, interval_seconds=interval,
                                 daily_limit=daily_limit, day_start=int(now//86400)*86400, now=now)

    def research_row(self, db, key="q:public"):
        return db._read("SELECT * FROM tool_research WHERE question_key=?", (key,))[0]

    def test_empty_research_waits_longer_after_restart_and_caps_at_one_day(self):
        db, stamp = self.store(), 100
        for round_number, delay in enumerate((7200, 14400, 28800, 57600, 86400, 86400), 1):
            claim = self.claim(db, stamp)
            self.assertIsNotNone(claim)
            db.finish_research("q:public", True, now=stamp+1, token=claim["token"],
                               no_evidence=True, question_version=QUESTION)
            row = self.research_row(db)
            self.assertEqual((row["status"], row["failures"], row["daily_count"], row["lease_token"]),
                             ("no_evidence", 0, round_number, QUESTION))
            self.assertEqual(row["next_attempt"], stamp+1+delay)
            db = self.store()
            stamp = row["next_attempt"]
            self.assertIsNone(self.claim(db, stamp-1))

    def test_empty_count_is_bounded_and_preserves_long_configured_intervals(self):
        db, stamp = self.store(), 100
        for index in range(21):
            claim = self.claim(db, stamp, interval=1)
            db.finish_research("q:public", True, now=stamp+0.5, token=claim["token"],
                               no_evidence=True, question_version=QUESTION)
            row = self.research_row(db)
            self.assertEqual(row["daily_count"], min(20, index+1))
            self.assertLessEqual(row["next_attempt"]-stamp-0.5, 86400)
            stamp = row["next_attempt"]
        stamp += 172800
        claim = self.claim(db, stamp, interval=172800)
        db.finish_research("q:public", True, now=stamp+1, token=claim["token"],
                           no_evidence=True, question_version=QUESTION)
        self.assertEqual(self.research_row(db)["next_attempt"], stamp+1+172800)

    def test_evidence_and_failures_reset_empty_rounds_with_independent_failure_count(self):
        db = self.store()
        first = self.claim(db, 100)
        db.finish_research("q:public", True, now=101, token=first["token"],
                           no_evidence=True, question_version=QUESTION)
        evidence = self.claim(db, 7301)
        db.finish_research("q:public", True, now=7302, token=evidence["token"])
        row = self.research_row(db)
        self.assertEqual((row["status"], row["daily_count"], row["lease_token"], row["next_attempt"]),
                         ("succeeded", 0, "", 10902))
        empty = self.claim(db, 10902)
        db.finish_research("q:public", True, now=10903, token=empty["token"],
                           no_evidence=True, question_version=QUESTION)
        failed = self.claim(db, 18103)
        db.finish_research("q:public", False, now=18104, token=failed["token"])
        row = self.research_row(db)
        self.assertEqual((row["status"], row["failures"], row["daily_count"], row["next_attempt"]),
                         ("failed", 1, 0, 21704))
        failed = self.claim(db, 21704)
        db.finish_research("q:public", False, now=21705, token=failed["token"])
        self.assertEqual(self.research_row(db)["next_attempt"], 28905)
        empty = self.claim(db, 28905)
        db.finish_research("q:public", True, now=28906, token=empty["token"],
                           no_evidence=True, question_version=QUESTION)
        row = self.research_row(db)
        self.assertEqual((row["failures"], row["daily_count"], row["next_attempt"]), (0, 1, 36106))

    def test_revised_question_resets_empty_wait_after_global_interval_and_quota(self):
        db = self.store()
        first = self.claim(db, 100)
        db.finish_research("q:public", True, now=101, token=first["token"],
                           no_evidence=True, question_version=QUESTION)
        revised = digest("new sources and question")
        self.assertIsNone(self.claim(db, 3699, version=revised))
        self.assertIsNone(self.claim(db, 3700, version=revised, daily_limit=1))
        self.assertIsNone(self.claim(db, 3700))
        claim = self.claim(db, 3700, version=revised)
        self.assertIsNotNone(claim)
        db.finish_research("q:public", True, now=3701, token=claim["token"],
                           no_evidence=True, question_version=revised)
        row = self.research_row(db)
        self.assertEqual((row["daily_count"], row["lease_token"], row["next_attempt"]),
                         (1, revised, 10901))
        with self.assertRaisesRegex(ValueError, "research_lease_lost"):
            db.finish_research("q:public", False, now=3702, token=first["token"])
        self.assertEqual(self.research_row(db), row)

    def test_version_change_keeps_live_lease_and_expired_finish_cannot_override_reclaim(self):
        db = self.store()
        old = self.claim(db, 100, interval=60)
        revised = digest("new question")
        self.assertIsNone(self.claim(db, 160, version=revised, interval=60))
        new = self.claim(db, 700, version=revised, interval=60)
        self.assertIsNotNone(new)
        with self.assertRaisesRegex(ValueError, "research_lease_lost"):
            db.finish_research("q:public", True, now=701, token=old["token"],
                               no_evidence=True, question_version=QUESTION)
        self.assertEqual(self.research_row(db)["lease_token"], new["token"])
        db.finish_research("q:public", True, now=702, token=new["token"],
                           no_evidence=True, question_version=revised)
        self.assertEqual(self.research_row(db)["daily_count"], 1)

    def test_legacy_research_call_still_uses_ordinary_success_interval(self):
        db = self.store()
        first = db.claim_research(["q:legacy"], interval_seconds=60, daily_limit=10, day_start=0, now=100)
        db.finish_research("q:legacy", True, now=101)
        row = self.research_row(db, "q:legacy")
        self.assertEqual((row["status"], row["daily_count"], row["next_attempt"]), ("succeeded", 0, 161))
        self.assertIsNotNone(first)
        second = db.claim_research(["q:legacy"], interval_seconds=60, daily_limit=10, day_start=0, now=161)
        self.assertIsNotNone(second)

    def test_research_versions_and_empty_outcomes_are_validated(self):
        db = self.store()
        for versions in ([], {"q:other": QUESTION}, {"q:public": "raw question"}, {"q:public": None}):
            with self.assertRaisesRegex(ValueError, "invalid_question_versions"):
                db.claim_research(["q:public"], interval_seconds=60, daily_limit=10,
                                  day_start=0, now=100, question_versions=versions)
        claim = self.claim(db, 100)
        row = self.research_row(db)
        for success, options in ((False, {"no_evidence": True, "question_version": QUESTION}),
                                 (True, {"no_evidence": True}),
                                 (True, {"no_evidence": 1, "question_version": QUESTION}),
                                 (True, {"question_version": "raw question"})):
            with self.assertRaisesRegex(ValueError, "invalid_research_result"):
                db.finish_research("q:public", success, now=101, token=claim["token"], **options)
        with self.assertRaisesRegex(ValueError, "invalid_research_result"):
            db.finish_research("q:public", True, now=101, no_evidence=True, question_version=QUESTION)
        self.assertEqual(self.research_row(db), row)

    def test_concurrent_revised_question_claims_consume_one_slot(self):
        db = self.store()
        first = self.claim(db, 100)
        db.finish_research("q:public", True, now=101, token=first["token"],
                           no_evidence=True, question_version=QUESTION)
        barrier = threading.Barrier(2)
        def claim():
            connection = self.store()
            try:
                barrier.wait(timeout=30)
                return self.claim(connection, 3700, version=digest("revised question"))
            finally:
                self.stores.remove(connection)
                connection.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(claim) for _ in range(2)]
            results = [future.result(timeout=50) for future in futures]
        self.assertEqual(sum(result is not None for result in results), 1)
        row = self.research_row(db)
        self.assertEqual((row["attempts"], row["daily_count"], row["status"]), (2, 0, "running"))

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

    def attempt(self, db, *, now=10, outcome="unreadable", provider="news", tool="news_read"):
        arguments = digest({"id": "synthetic-article", "at": now})
        call = db.reserve_call(provider, tool, arguments, "research", "private:"+OWNER, OWNER,
                               0, 100, now=now)
        db.complete_call(call["id"], "succeeded" if outcome == "unreadable" else "uncertain",
                         PUBLIC_READ_ATTEMPT_SUMMARIES[outcome], now=now+1)
        return {"provider": provider, "tool": tool, "args_digest": arguments,
                "call_id": call["id"], "outcome": outcome}

    def test_unreadable_and_failed_attempts_survive_restart_and_expire_from_call_time(self):
        db = self.store()
        for index, (outcome, delay) in enumerate((("unreadable", 21600), ("read_failed", 7200))):
            question = digest(outcome)
            ticket = self.attempt(db, now=10+index, outcome=outcome)
            self.assertTrue(db.mark_research_attempt(OWNER, question, URL, VERSION, **ticket, now=30))
            before = db._read("SELECT * FROM tool_research ORDER BY question_key")
            self.assertTrue(db.mark_research_attempt(OWNER, question, URL, VERSION, **ticket, now=40))
            self.assertEqual(db._read("SELECT * FROM tool_research ORDER BY question_key"), before)
            restarted = self.store()
            self.assertEqual(restarted.research_candidate_status(OWNER, question, {URL: VERSION}, now=41),
                             {"available": set(), "deferred": {URL}, "retrying": {}})
            self.assertEqual(restarted.new_research_candidates(OWNER, question, {URL: VERSION}, now=41), set())
            for stamp in (10, 11+index+delay, 100000):
                self.assertEqual(restarted.research_candidate_status(OWNER, question, {URL: VERSION}, now=stamp),
                                 {"available": {URL}, "deferred": set(), "retrying": {URL: 11+index} if stamp > 10 else {}})
        self.assertEqual(db.observations(), [])

    def test_retry_window_is_bound_to_owner_question_and_listing(self):
        db = self.store()
        ticket = self.attempt(db)
        self.assertTrue(db.mark_research_attempt(OWNER, QUESTION, URL, VERSION, **ticket, now=12))
        for owner, question, version in ((OTHER, QUESTION, VERSION), (OWNER, digest("revised"), VERSION),
                                         (OWNER, QUESTION, digest("new listing"))):
            self.assertEqual(db.research_candidate_status(owner, question, {URL: version}, now=13),
                             {"available": {URL}, "deferred": set(), "retrying": {}})
        other_url = "https://observatory.example/next"
        self.assertEqual(db.research_candidate_status(OWNER, QUESTION, {URL: VERSION, other_url: VERSION}, now=13),
                         {"available": {other_url}, "deferred": {URL}, "retrying": {}})

    def test_attempt_requires_matching_completed_autonomous_read_receipt(self):
        db = self.store()
        ticket = self.attempt(db)
        for change in ({"args_digest": digest("other arguments")}, {"provider": "reference", "tool": "reference_read"},
                       {"tool": "web_read"}, {"outcome": "read_failed"}, {"call_id": "tool:call:"+digest("absent")}):
            with self.subTest(change=change):
                self.assertFalse(db.mark_research_attempt(OWNER, QUESTION, URL, VERSION, **{**ticket, **change}, now=12))
        self.assertFalse(db.mark_research_attempt(OTHER, QUESTION, URL, VERSION, **ticket, now=12))
        for stamp in (10, 72):
            self.assertFalse(db.mark_research_attempt(OWNER, QUESTION, URL, VERSION, **ticket, now=stamp))
        for index, (status, summary) in enumerate((("failed", PUBLIC_READ_ATTEMPT_SUMMARIES["read_failed"]),
                ("uncertain", ""), ("succeeded", ""), ("succeeded", "untrusted source text"))):
            args = digest(index)
            call = db.reserve_call("news", "news_read", args, "research", "private:"+OWNER, OWNER, 0, 100, now=20)
            db.complete_call(call["id"], status, summary, now=21)
            outcome = "read_failed" if status in {"failed", "uncertain"} else "unreadable"
            self.assertFalse(db.mark_research_attempt(OWNER, QUESTION, URL, VERSION,
                **{**ticket, "call_id": call["id"], "args_digest": args, "outcome": outcome}, now=22))
        reserved = db.reserve_call("news", "news_read", digest("reserved"), "research", "private:"+OWNER,
                                   OWNER, 0, 100, now=23)
        self.assertFalse(db.mark_research_attempt(OWNER, QUESTION, URL, VERSION,
            **{**ticket, "call_id": reserved["id"], "args_digest": digest("reserved")}, now=24))
        self.assertEqual(db.research_candidate_status(OWNER, QUESTION, {URL: VERSION}, now=25),
                         {"available": {URL}, "deferred": set(), "retrying": {}})

    def test_attempt_input_validation_and_all_three_public_readers(self):
        db = self.store()
        ticket = self.attempt(db)
        for change in ({"provider": "cap"}, {"tool": "news_search"}, {"outcome": "unknown"},
                       {"outcome": []}, {"args_digest": "raw arguments"}, {"call_id": "invalid"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                db.mark_research_attempt(OWNER, QUESTION, URL, VERSION, **{**ticket, **change}, now=12)
        for index, (provider, tool) in enumerate((("news", "web_read"), ("reference", "reference_read"))):
            ticket = self.attempt(db, now=20+index, provider=provider, tool=tool)
            self.assertTrue(db.mark_research_attempt(OWNER, digest(tool), URL, VERSION, **ticket, now=25))
            self.assertEqual(db.research_candidate_status(OWNER, digest(tool), {URL: VERSION}, now=26)["deferred"], {URL})

    def test_concurrent_attempts_keep_latest_receipt_and_success_replaces_cooldown(self):
        db = self.store()
        tickets = [self.attempt(db, now=stamp) for stamp in (10, 20)]
        barrier = threading.Barrier(2)
        def mark(index):
            connection = self.store()
            try:
                barrier.wait(timeout=30)
                return connection.mark_research_attempt(OWNER, QUESTION, URL, digest(index), **tickets[index], now=30)
            finally:
                self.stores.remove(connection)
                connection.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(mark, index) for index in range(2)]
            self.assertTrue(all(future.result(timeout=50) for future in futures))
        self.assertEqual(db.research_candidate_status(OWNER, QUESTION, {URL: digest(1)}, now=31)["deferred"], {URL})
        self.assertEqual(db.new_research_candidates(OWNER, QUESTION, {URL: digest(0)}, now=31), {URL})
        record, call = self.read(db, now=32)
        self.assertTrue(db.mark_research_read(OWNER, QUESTION, URL, VERSION, record, call_id=call, now=34))
        self.assertTrue(db.mark_research_attempt(OWNER, QUESTION, URL, digest(1), **tickets[1], now=35))
        self.assertEqual(db.research_candidate_status(OWNER, QUESTION, {URL: VERSION}, now=36),
                         {"available": set(), "deferred": set(), "retrying": {}})
        rows = db._read("SELECT next_attempt FROM tool_research WHERE question_key LIKE ?", ("__tool_research_read__:%",))
        self.assertEqual([row["next_attempt"] for row in rows], [0])

    def test_attempt_write_failure_rolls_back_and_allows_retry(self):
        db = self.store()
        ticket = self.attempt(db)
        with patch.object(db, "_prune_research_markers", side_effect=RuntimeError("synthetic prune failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic prune failure"):
                db.mark_research_attempt(OWNER, QUESTION, URL, VERSION, **ticket, now=12)
        self.assertEqual(db.research_candidate_status(OWNER, QUESTION, {URL: VERSION}, now=13),
                         {"available": {URL}, "deferred": set(), "retrying": {}})
        self.assertTrue(db.mark_research_attempt(OWNER, QUESTION, URL, VERSION, **ticket, now=14))
        self.assertEqual(db.research_candidate_status(OWNER, QUESTION, {URL: VERSION}, now=15)["deferred"], {URL})

    def test_success_and_attempt_markers_share_one_bounded_reserved_namespace(self):
        db = self.store()
        record, call = self.read(db)
        ticket = self.attempt(db, now=11)
        for index in range(258):
            question = digest(index)
            if index % 2:
                self.assertTrue(db.mark_research_attempt(OWNER, question, URL, VERSION, **ticket, now=13))
            else:
                self.assertTrue(db.mark_research_read(OWNER, question, URL, VERSION, record, call_id=call, now=13))
        rows = db._read("SELECT question_key,status,lease_token FROM tool_research WHERE question_key>=? AND question_key<?",
                       ("__tool_research_read__:", "__tool_research_read__:~"))
        self.assertEqual(len(rows), 256)
        self.assertTrue(any(row["status"].startswith("unreadable:") for row in rows))
        self.assertTrue(any(not row["status"].startswith("unreadable:") for row in rows))
        self.assertTrue(all(len(row[field]) <= 191 for row in rows for field in row))
        with self.assertRaises(ValueError):
            self.claim(db, 14, key=rows[0]["question_key"])
        with self.assertRaises(ValueError):
            db.next_local_question([rows[0]["question_key"]])


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
