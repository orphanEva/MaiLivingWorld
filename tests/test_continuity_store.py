from __future__ import annotations

import concurrent.futures
import json
import tempfile
import threading
import unittest
from pathlib import Path

from MaiLivingWorld.continuity_store import ContinuityStore

OWNER, OTHER = "12345678", "87654321"


def message(mid="m1", body="明天考试，有点担心", owner=OWNER, stream="private-stream", *, group=False):
    info = {"user_info": {"user_id": owner}}
    if group:
        info["group_info"] = {"group_id": "77777777"}
    return {"platform": "qq", "account_id": "99999999", "session_id": stream, "message_id": mid,
            "message_info": info, "processed_plain_text": body}


def proposal(ident, *, action="open", thread_id="", title="考试", summary="主人担心考试", quote="考试", kind="concern"):
    return {"updates": [{"action": action, "thread_id": thread_id, "kind": kind, "title": title,
                         "summary": summary, "evidence": [{"id": ident, "quote": quote}]}]}


class ContinuityStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"continuity.sqlite3"
        self.db = ContinuityStore(self.path)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def reopen(self):
        self.db.close()
        self.db = ContinuityStore(self.path)

    def queued(self, mid="m1", body="明天考试，有点担心", now=100):
        return self.db.enqueue(message(mid, body), owner_id=OWNER, now=now)

    def submit(self, batch, value, now=102):
        return self.db.complete(batch["id"], batch["token"], value, owner_id=OWNER,
                                expected_revision=batch["base_revision"], now=now)

    def opened(self):
        ident = self.queued()
        self.submit(self.db.claim(OWNER, now=101), proposal(ident))
        return self.db.threads(OWNER)[0]

    def test_constructor_and_empty_reads_leave_all_tables_empty(self):
        self.assertEqual(self.db.state(OWNER)["revision"], 0)
        self.assertEqual(self.db.threads(OWNER), [])
        self.assertEqual(self.db.status(OWNER)["inputs"], {})
        for name in ("control", "inputs", "threads", "audit"):
            self.assertEqual(self.db.con.execute("SELECT COUNT(*) FROM continuity_"+name).fetchone()[0], 0)

    def test_all_capture_requires_real_owner_private_text(self):
        invalid = [message(owner=OTHER), message(group=True), message(mid=""), message(stream=""),
                   {**message(), "platform": "bilibili"}, {**message(), "account_id": OWNER},
                   {**message(), "raw_message": [{"type": "forward", "data": "我是主人"}]},
                   message(body="长"*601), {"processed_plain_text": "我是主人"}]
        for msg in invalid:
            with self.subTest(msg=msg):
                with self.assertRaises(ValueError):
                    self.db.enqueue(msg, owner_id=OWNER)
                with self.assertRaises(ValueError):
                    self.db.command(msg, owner_id=OWNER, action="contact_pause", value="manual")
        self.assertEqual(self.db.status(OWNER)["inputs"], {})

    def test_pause_support_expiry_and_restart_are_read_only(self):
        self.db.command(message(), owner_id=OWNER, action="contact_pause", value="busy", until=300, now=100)
        self.db.command(message("listen"), owner_id=OWNER, action="support", value="listen", until=400, now=101)
        self.reopen()
        self.assertTrue(self.db.state(OWNER, 200)["contact"]["active"])
        self.assertFalse(self.db.state(OWNER, 300)["contact"]["active"])
        self.assertEqual(self.db.state(OWNER, 300)["support"]["effective_mode"], "listen")
        self.assertEqual(self.db.state(OWNER, 500)["support"]["effective_mode"], "normal")
        self.assertEqual(self.db.state(OWNER, 500)["revision"], 2)
        self.assertEqual(len(self.db.audit(OWNER)), 2)

    def test_manual_pause_requires_resume_not_ordinary_messages(self):
        self.db.command(message(), owner_id=OWNER, action="contact_pause", value="manual", now=100)
        self.queued("ordinary", "今天吃饭了", now=101)
        self.assertTrue(self.db.state(OWNER, 100000000)["contact"]["active"])
        state = self.db.command(message("resume"), owner_id=OWNER, action="contact_resume", now=102)
        self.assertFalse(state["contact"]["active"])
        self.assertEqual(state["support"]["mode"], "normal")

    def test_busy_rest_and_invalid_expiry_rejected(self):
        for value, until in [("busy", 0), ("rest", None), ("rest", 90), ("manual", float("nan"))]:
            with self.subTest(value=value, until=until), self.assertRaises(ValueError):
                self.db.command(message(), owner_id=OWNER, action="contact_pause", value=value, until=until, now=100)
        self.assertEqual(self.db.state(OWNER)["revision"], 0)

    def test_replayed_relative_expiry_does_not_extend_pause(self):
        first = self.db.command(message(), owner_id=OWNER, action="contact_pause", value="busy", until=200, now=100)
        repeat = self.db.command(message(), owner_id=OWNER, action="contact_pause", value="busy", until=500, now=400)
        self.assertEqual(repeat["contact"]["until"], first["contact"]["until"])
        self.assertFalse(repeat["contact"]["active"])
        self.assertEqual(repeat["revision"], 1)
        with self.assertRaisesRegex(ValueError, "idempotency_conflict"):
            self.db.command(message(body="改过的内容"), owner_id=OWNER, action="contact_pause", value="busy", until=500, now=400)

    def test_receipt_flag_prevents_replayed_resume_affecting_new_external_opt_out(self):
        resumed = self.db.command(message("resume"), owner_id=OWNER, action="contact_resume", with_receipt=True, now=100)
        # A fresh resume is meaningful even when the continuity state is already normal.
        self.assertEqual(resumed["state"]["revision"], 0)
        self.assertTrue(resumed["new_receipt"])
        self.reopen()
        repeated = self.db.command(message("resume"), owner_id=OWNER, action="contact_resume", with_receipt=True, now=200)
        self.assertFalse(repeated["new_receipt"])
        self.assertEqual(repeated["state"], resumed["state"])
        # Default callers retain exactly the original state return contract.
        plain = self.db.command(message("resume"), owner_id=OWNER, action="contact_resume", now=201)
        self.assertEqual(plain, repeated["state"])
        self.assertNotIn("new_receipt", plain)
        fresh = self.db.command(message("resume-new"), owner_id=OWNER, action="contact_resume", with_receipt=True, now=202)
        self.assertTrue(fresh["new_receipt"])
        self.assertEqual(len(self.db.audit(OWNER)), 2)

    def test_source_receipt_dedup_contains_stream_and_rejects_modified_body(self):
        ident = self.queued()
        self.assertEqual(self.queued(now=200), ident)
        with self.assertRaisesRegex(ValueError, "idempotency_conflict"):
            self.queued(body="后来改了说法")
        other = self.db.enqueue(message(stream="another-stream"), owner_id=OWNER, now=100)
        self.assertNotEqual(ident, other)
        self.assertEqual(len(self.db.claim(OWNER, now=101)["inputs"]), 2)

    def test_budget_is_separate_from_revision_and_survives_restart(self):
        args = dict(day_start=0, interval_seconds=10, daily_limit=2)
        self.assertTrue(self.db.reserve_cycle(OWNER, now=100, **args))
        self.assertFalse(self.db.reserve_cycle(OWNER, now=105, **args))
        self.assertTrue(self.db.reserve_cycle(OWNER, now=110, **args))
        self.reopen()
        self.assertFalse(self.db.reserve_cycle(OWNER, now=120, **args))
        self.assertEqual(self.db.state(OWNER)["revision"], 0)
        self.assertTrue(self.db.reserve_cycle(OWNER, now=86420, day_start=86400, interval_seconds=10, daily_limit=2))

    def test_batch_claim_is_exclusive_and_expired_lease_can_recover(self):
        for i in range(5):
            self.queued(str(i))
        first = self.db.claim(OWNER, now=101, lease_seconds=10)
        self.assertEqual(len(first["inputs"]), 4)
        self.assertIsNone(self.db.claim(OWNER, now=102))
        self.reopen()
        second = self.db.claim(OWNER, now=111)
        self.assertEqual(first["id"], second["id"])
        self.assertNotEqual(first["token"], second["token"])
        with self.assertRaisesRegex(ValueError, "lease_conflict"):
            self.submit(first, {"updates": []}, now=112)
        self.submit(second, {"updates": []}, now=112)
        self.assertEqual(len(self.db.claim(OWNER, now=113)["inputs"]), 1)

    def test_budget_defer_does_not_consume_three_model_failures(self):
        self.queued()
        batch = self.db.claim(OWNER, now=101)
        self.db.defer(batch["id"], batch["token"], owner_id=OWNER, reason="budget", retry_at=150, now=102)
        self.assertIsNone(self.db.claim(OWNER, now=149))
        for now in (150, 300, 600):
            batch = self.db.claim(OWNER, now=now)
            self.assertIsNotNone(batch)
            self.db.fail(batch["id"], batch["token"], owner_id=OWNER, reason="invalid_model", now=now)
        self.assertIsNone(self.db.claim(OWNER, now=2000))
        self.assertEqual(self.db.status(OWNER)["inputs"], {"failed": 1})

    def test_expired_pending_input_cannot_generate_memory(self):
        self.db.enqueue(message(), owner_id=OWNER, now=100, ttl_seconds=60)
        self.assertIsNone(self.db.claim(OWNER, now=161))
        self.assertEqual(self.db.status(OWNER)["inputs"], {"expired": 1})
        row = self.db.con.execute("SELECT payload FROM continuity_inputs").fetchone()
        self.assertEqual(json.loads(row[0])["text"], "")

    def test_quotes_are_contiguous_real_input_not_model_claims(self):
        ident = self.queued()
        batch = self.db.claim(OWNER, now=101)
        for invalid in [proposal("invented"), proposal(ident, quote="他已经通过考试"),
                        proposal(ident, quote="明天有点担心"), {"updates": [{"summary": "猜的"}]}]:
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                self.submit(batch, invalid)
        self.assertEqual(self.db.threads(OWNER), [])
        stats = self.submit(batch, proposal(ident))
        self.assertEqual((stats["opened"], stats["processed"]), (1, 1))

    def test_pending_model_cannot_commit_after_immediate_owner_boundary(self):
        ident = self.queued()
        batch = self.db.claim(OWNER, now=101)
        self.db.command(message("boundary"), owner_id=OWNER, action="support", value="listen", until=500, now=102)
        with self.assertRaisesRegex(ValueError, "revision_conflict"):
            self.submit(batch, proposal(ident), now=103)
        self.assertEqual(self.db.threads(OWNER), [])

    def test_only_quotes_persist_after_complete_and_reopen(self):
        row = self.opened()
        self.reopen()
        restored = self.db.threads(OWNER)[0]
        self.assertEqual(restored, row)
        self.assertEqual(restored["evidence_status"], "owner_reported")
        self.assertEqual(restored["evidence"][0]["quote"], "考试")
        self.assertEqual(restored["evidence"][0]["timestamp"], 100)
        self.assertEqual(json.loads(self.db.con.execute("SELECT payload FROM continuity_inputs").fetchone()[0])["text"], "")
        self.assertEqual(self.db.threads(OTHER), [])
        self.assertIsNone(self.db.thread(row["id"], owner_id=OTHER))

    def test_existing_thread_update_keeps_anchor_and_all_source_constraints(self):
        thread = self.opened()
        for index in range(1, 5):
            ident = self.queued("more"+str(index), "考试准备进展"+str(index), now=200+index*10)
            batch = self.db.claim(OWNER, now=201+index*10)
            self.submit(batch, proposal(ident, thread_id=thread["id"], summary="准备进展"+str(index)), now=202+index*10)
        row = self.db.threads(OWNER)[0]
        self.assertEqual(len(row["source_refs"]), 5)
        self.assertEqual(len(row["evidence"]), 4)
        self.assertEqual(row["evidence"][0], thread["evidence"][0])
        self.assertEqual(row["revision"], 5)
        self.db.revoke_input(thread["source_refs"][0]["id"], owner_id=OWNER, now=300)
        self.assertEqual(self.db.threads(OWNER), [])

    def test_confidential_sources_are_not_laundered_by_later_public_inputs(self):
        source_ref = {"id": "original-owner-message", "digest": "a"*64,
                      "scope": "private:"+OWNER, "sensitivity": "confidential"}
        ident = self.db.enqueue(message(), owner_id=OWNER, source_ref=source_ref, now=100)
        self.submit(self.db.claim(OWNER, now=101), proposal(ident))
        thread = self.db.threads(OWNER)[0]
        self.assertEqual(thread["sensitivity"], "confidential")
        fresh = self.queued("fresh", "考试准备还行", now=200)
        self.submit(self.db.claim(OWNER, now=201), proposal(fresh, thread_id=thread["id"]), now=202)
        self.assertEqual(self.db.threads(OWNER)[0]["sensitivity"], "confidential")
        with self.assertRaisesRegex(ValueError, "idempotency_conflict"):
            self.db.enqueue(message(), owner_id=OWNER, source_ref={**source_ref, "sensitivity": "public"}, now=300)

    def test_source_limit_rejects_whole_update_without_dropping_old_dependencies(self):
        thread = self.opened()
        for index in range(1, 12):
            now = 200+index*10
            ident = self.queued("source"+str(index), now=now)
            self.submit(self.db.claim(OWNER, now=now+1), proposal(ident, thread_id=thread["id"]), now=now+2)
        before = self.db.threads(OWNER)[0]
        self.assertEqual(len(before["source_refs"]), 12)
        ident = self.queued("too-many", now=400)
        batch = self.db.claim(OWNER, now=401)
        with self.assertRaisesRegex(ValueError, "source_capacity"):
            self.submit(batch, proposal(ident, thread_id=thread["id"]), now=402)
        self.assertEqual(self.db.threads(OWNER)[0], before)

    def test_audit_failure_rolls_back_forget_and_source_revocation(self):
        thread = self.opened()
        original = self.db._audit
        def fail(*args):
            raise RuntimeError("synthetic_audit_failure")
        self.db._audit = fail
        try:
            with self.assertRaisesRegex(RuntimeError, "synthetic_audit_failure"):
                self.db.forget_thread(message("forget"), owner_id=OWNER, thread_id=thread["id"], expected_revision=1, now=200)
        finally:
            self.db._audit = original
        self.assertEqual(self.db.threads(OWNER), [thread])
        self.assertEqual(len(self.db.audit(OWNER)), 1)

    def test_new_owner_report_can_start_fresh_round_after_resolution(self):
        thread = self.opened()
        ident = self.queued("end", "考试结束了", now=200)
        batch = self.db.claim(OWNER, now=201)
        self.submit(batch, proposal(ident, action="resolve", thread_id=thread["id"], summary="考试结束"), now=202)
        self.assertEqual(self.db.threads(OWNER), [])
        self.assertEqual(self.db.threads(OWNER, status="resolved")[0]["status"], "resolved")
        ident = self.queued("again", now=300)
        stats = self.submit(self.db.claim(OWNER, now=301), proposal(ident, summary="下一场考试"), now=302)
        self.assertEqual(stats["opened"], 1)
        self.assertEqual(len(self.db.threads(OWNER, status="all")), 1)
        renewed = self.db.threads(OWNER)[0]
        self.assertEqual((renewed["id"], renewed["revision"], renewed["summary"]), (thread["id"], 3, "下一场考试"))
        self.assertEqual([ref["id"] for ref in renewed["source_refs"]], [ident])
        self.assertEqual([quote["id"] for quote in renewed["evidence"]], [ident])
        history = self.db.audit(OWNER)
        self.assertEqual(history[0]["before"]["status"], "resolved")
        self.assertNotEqual(history[0]["before"]["source_refs"], renewed["source_refs"])

    def test_old_queued_owner_report_cannot_reopen_resolved_thread(self):
        thread = self.opened()
        closing = self.queued("close", "考试结束了", now=190)
        older = self.queued("waiting", "考试还有点担心", now=191)
        batch = self.db.claim(OWNER, limit=1, now=192)
        self.assertEqual(batch["id"], closing)
        self.submit(batch, proposal(closing, action="resolve", thread_id=thread["id"]), now=200)
        fresh = self.queued("fresh", "又要考试了", now=201)
        batch = self.db.claim(OWNER, now=202)
        mixed = proposal(fresh)
        mixed["updates"][0]["evidence"].append({"id": older, "quote": "考试"})
        result = self.submit(batch, mixed, now=203)
        self.assertEqual(result["ignored"], 1)
        self.assertEqual(self.db.threads(OWNER), [])
        self.assertEqual(self.db.threads(OWNER, status="resolved")[0]["revision"], 2)
        self.queued("close", "考试结束了", now=204)
        self.assertIsNone(self.db.claim(OWNER, now=205))

    def test_classification_change_cannot_revive_resolved_dismissed_or_forgotten_title(self):
        for index, terminal in enumerate(("resolve", "dismiss", "forget")):
            now, title = 1000+index*100, "考试-"+terminal
            ident = self.queued("initial-"+terminal, now=now)
            self.submit(self.db.claim(OWNER, now=now+1), proposal(ident, title=title), now=now+2)
            row = next(row for row in self.db.threads(OWNER) if row["title"] == title)
            if terminal == "forget":
                self.db.forget_thread(message("forget-"+terminal), owner_id=OWNER, thread_id=row["id"],
                                      expected_revision=1, now=now+3)
            else:
                ident = self.queued("close-"+terminal, "考试结束了", now=now+3)
                self.submit(self.db.claim(OWNER, now=now+4), proposal(ident, action=terminal,
                            thread_id=row["id"], title=title), now=now+5)
            revision = self.db.state(OWNER)["revision"]
            ident = self.queued("revive-"+terminal, now=now+10)
            result = self.submit(self.db.claim(OWNER, now=now+11), proposal(ident, title=title, kind="event"), now=now+12)
            self.assertEqual((result["ignored"], result["opened"], result["revision"]), (1, 0, revision))
            self.assertFalse(any(item["title"] == title for item in self.db.threads(OWNER)))

    def test_full_input_queue_has_explicit_capacity_state_and_preserves_existing_inputs(self):
        for index in range(64):
            self.queued("queued-"+str(index), now=100)
        self.assertIsNone(self.queued("new-resolution", "考试已经结束了", now=101))
        state = self.db.status(OWNER, now=101)
        self.assertEqual((state["queued_inputs"], state["input_capacity"], state["queue_full"]), (64, 64, True))
        batch = self.db.claim(OWNER, now=102)
        self.submit(batch, {"updates": []}, now=103)
        self.assertFalse(self.db.status(OWNER, now=103)["queue_full"])
        self.assertIsNotNone(self.queued("new-resolution", "考试已经结束了", now=104))

    def test_revoked_threads_do_not_exhaust_active_capacity_or_automatically_revive(self):
        original = self.opened()
        # Seed the capacity boundary using copies of one valid provenance record.
        with self.db.con:
            for index in range(63):
                self.db.con.execute("INSERT INTO continuity_threads(id,owner_id,kind,status,revision,payload,created,updated) "
                    "SELECT ?,owner_id,kind,status,revision,payload,created,updated FROM continuity_threads WHERE id=?",
                    ("fixture-capacity:"+str(index), original["id"]))
        ident = self.queued("fresh-topic", "面试有点担心", now=200)
        batch = self.db.claim(OWNER, now=201)
        new_topic = proposal(ident, title="面试", quote="面试")
        with self.assertRaisesRegex(ValueError, "thread_capacity"):
            self.submit(batch, new_topic, now=202)
        self.db.revoke_input(original["source_refs"][0]["id"], owner_id=OWNER, now=203)
        self.assertEqual(self.db.threads(OWNER), [])
        self.assertEqual(self.submit(self.db.claim(OWNER, now=204), new_topic, now=205)["opened"], 1)
        ident = self.queued("reopen-withdrawn", now=206)
        result = self.submit(self.db.claim(OWNER, now=207), proposal(ident, kind="wish"), now=208)
        self.assertEqual(result["ignored"], 1)
        self.assertEqual([row["title"] for row in self.db.threads(OWNER)], ["面试"])

    def test_model_cannot_target_another_owner_or_change_kind_title(self):
        thread = self.opened()
        ident = self.queued("update", now=200)
        batch = self.db.claim(OWNER, now=201)
        for value in [proposal(ident, thread_id="other"), proposal(ident, thread_id=thread["id"], title="另一个考试"),
                      proposal(ident, action="resolve", thread_id=thread["id"], kind="wish")]:
            with self.assertRaisesRegex(ValueError, "thread_not_open"):
                self.submit(batch, value, now=202)
        with self.assertRaisesRegex(ValueError, "lease_conflict"):
            self.db.complete(batch["id"], batch["token"], proposal(ident), owner_id=OTHER, expected_revision=0, now=202)

    def test_transaction_rolls_back_prior_update_if_second_is_invalid(self):
        ident = self.queued()
        batch = self.db.claim(OWNER, now=101)
        bad = proposal(ident)
        bad["updates"].append(proposal(ident, title="另一个", quote="不存在")["updates"][0])
        with self.assertRaisesRegex(ValueError, "unverified_quote"):
            self.submit(batch, bad)
        self.assertEqual(self.db.threads(OWNER), [])
        self.assertEqual(self.db.audit(OWNER), [])
        self.assertEqual(self.db.state(OWNER)["revision"], 0)
        self.submit(batch, proposal(ident))

    def test_forget_clears_current_and_audited_prose_and_dedup_prevents_revival(self):
        row = self.opened()
        forgotten = self.db.forget_thread(message("forget"), owner_id=OWNER, thread_id=row["id"], expected_revision=1, now=200)
        self.assertEqual((forgotten["summary"], forgotten["evidence"], forgotten["source_refs"]), ("", [], []))
        self.assertIsNone(self.db.thread(row["id"], owner_id=OWNER))
        self.assertNotIn("主人担心考试", json.dumps(self.db.audit(OWNER), ensure_ascii=False))
        self.reopen()
        repeat = self.db.forget_thread(message("forget"), owner_id=OWNER, thread_id=row["id"], expected_revision=1, now=300)
        self.assertEqual(repeat, forgotten)
        self.queued(now=400)
        self.assertIsNone(self.db.claim(OWNER, now=401))

    def test_forget_invalidates_other_threads_using_same_input(self):
        ident = self.queued(body="我担心考试，也想读书")
        batch = self.db.claim(OWNER, now=101)
        value = proposal(ident)
        value["updates"].append(proposal(ident, title="读书", kind="wish", summary="想读书", quote="想读书")["updates"][0])
        self.submit(batch, value)
        rows = self.db.threads(OWNER)
        self.assertEqual(len(rows), 2)
        self.db.forget_thread(message("forget"), owner_id=OWNER, thread_id=rows[0]["id"], expected_revision=1, now=200)
        self.assertEqual(self.db.threads(OWNER), [])

    def test_new_and_existing_owner_rows_serialize_concurrent_budget_reservations(self):
        barrier = threading.Barrier(3)
        def worker(index):
            db = ContinuityStore(self.path)
            try:
                results = []
                for day in (0, 86400):
                    barrier.wait(timeout=10)
                    results.append(db.reserve_cycle(OWNER, day_start=day, interval_seconds=0, daily_limit=1, now=day+100))
                return results
            finally:
                db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            rows = list(pool.map(worker, range(3)))
        self.assertEqual([sum(row[i] for row in rows) for i in (0, 1)], [1, 1])

    def test_parallel_claim_has_single_winner(self):
        for i in range(5):
            self.queued(str(i))
        barrier = threading.Barrier(3)
        def worker(index):
            db = ContinuityStore(self.path)
            try:
                barrier.wait(timeout=10)
                return db.claim(OWNER, now=101)
            finally:
                db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            rows = list(pool.map(worker, range(3)))
        self.assertEqual(sum(row is not None for row in rows), 1)


if __name__ == "__main__":
    unittest.main()
