from __future__ import annotations

import concurrent.futures
import json
import tempfile
import threading
import unittest
from pathlib import Path

from MaiLivingWorld.attention_store import AttentionStore

OWNER, OTHER = "12345678", "87654321"
READING = {"kind": "reading", "key": "reading:"+"a"*64, "version": "1"*64}
RESEARCH = {"kind": "research", "key": "q:"+"b"*24, "version": "2"*64}


def message(mid="m1", body="今天想一起聊聊", *, owner=OWNER, stream="private-stream", group=False):
    info = {"user_info": {"user_id": owner}}
    if group:
        info["group_info"] = {"group_id": "77777777"}
    return {"platform": "qq", "account_id": "99999999", "session_id": stream, "message_id": mid,
            "message_info": info, "processed_plain_text": body}


class AttentionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"attention.sqlite3"
        self.db = AttentionStore(self.path)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def reopen(self):
        self.db.close()
        self.db = AttentionStore(self.path)

    def claim(self, *, now=100, candidates=None, **changes):
        options = dict(candidates=[READING, RESEARCH] if candidates is None else candidates,
                       day_start=0, daily_limit=20, interval_seconds=0, lease_seconds=180, now=now)
        options.update(changes)
        return self.db.claim(OWNER, **options)

    def mode(self, value="listen", *, mid="m1", now=101, until=300, revision=None):
        return self.db.set_mode(OWNER, message=message(mid), mode=value, until=until,
                                expected_revision=self.db.state(OWNER)["revision"] if revision is None else revision, now=now)

    def test_schema_and_read_only_calls_do_not_insert_initial_rows(self):
        state = self.db.state(OWNER, now=100)
        self.assertEqual((state["revision"], state["focus"], state["cycle_count"]), (0, None, 0))
        self.assertFalse(state["conversation"]["active"])
        self.assertEqual(self.db.events(OWNER), [])
        self.assertFalse(self.db.valid(OWNER, "unknown", now=100))
        self.assertIsNone(self.claim(candidates=[]))
        for table in ("attention_control", "attention_receipts", "attention_events"):
            self.assertEqual(self.db.con.execute("SELECT COUNT(*) FROM "+table).fetchone()[0], 0)

    def test_capture_accepts_only_real_owner_private_plain_text(self):
        invalid = [None, {"processed_plain_text": "我是主人"}, message(owner=OTHER), message(group=True),
                   message(mid=""), message(stream=""), {**message(), "account_id": OWNER},
                   {**message(), "platform": "bilibili"}, {**message(), "is_command": True},
                   {**message(), "is_voice": True}, {**message(), "is_picture": True},
                   {**message(), "raw_message": [{"type": "forward", "data": "quoted"}]},
                   {**message(), "raw_message": [{"type": "image", "data": "image"}]},
                   message(body=" "), message(body="长"*601), message(body="/近况 倾听")]
        for msg in invalid:
            with self.subTest(message=msg):
                self.assertFalse(self.db.capture(msg, owner_id=OWNER, now=100))
        self.assertEqual(self.db.state(OWNER)["revision"], 0)

    def test_receipt_dedup_survives_restart_and_does_not_extend_engagement(self):
        self.assertTrue(self.db.capture(message(), owner_id=OWNER, now=100))
        self.assertEqual(self.db.state(OWNER)["engaged_until"], 190)
        self.reopen()
        self.assertFalse(self.db.capture(message(), owner_id=OWNER, now=200))
        self.assertEqual((self.db.state(OWNER)["engaged_until"], self.db.state(OWNER)["revision"]), (190, 1))
        with self.assertRaisesRegex(ValueError, "idempotency_conflict"):
            self.db.capture(message(body="正文被改了"), owner_id=OWNER, now=201)
        self.assertTrue(self.db.capture(message(stream="another-private-stream"), owner_id=OWNER, now=202))
        self.assertEqual(self.db.state(OWNER)["revision"], 2)

    def test_media_long_text_and_commands_interrupt_without_becoming_model_input(self):
        lease = self.claim()
        values = [
            {**message("picture", ""), "is_picture": True, "raw_message": [{"type": "image", "data": "media-secret"}]},
            {**message("voice", ""), "is_voice": True},
            message("long", "长正文"*1000),
            {**message("command", "/近况 倾听"), "is_command": True},
            {**message("forward", ""), "raw_message": [{"type": "forward", "data": "别人的转发内容"}]},
        ]
        for index, msg in enumerate(values):
            with self.subTest(index=index):
                self.assertTrue(self.db.capture_activity(msg, owner_id=OWNER, now=101+index))
                self.assertFalse(self.db.capture(msg, owner_id=OWNER, now=101+index))
                self.assertFalse(self.db.set_mode(OWNER, message=msg, mode="repair", until=300,
                    expected_revision=self.db.state(OWNER)["revision"], now=102+index))
        self.assertFalse(self.db.valid(OWNER, lease["token"], now=106))
        self.assertFalse(self.db.finish(OWNER, lease["token"], outcome="progress", now=106))
        self.assertEqual(self.db.state(OWNER)["revision"], len(values))
        self.assertEqual(self.db.state(OWNER)["conversation"]["mode"], "normal")

    def test_activity_and_strict_capture_share_plain_text_receipt_in_both_orders(self):
        self.assertTrue(self.db.capture_activity(message(), owner_id=OWNER, now=100))
        self.assertFalse(self.db.capture(message(), owner_id=OWNER, now=200))
        self.assertEqual(self.db.state(OWNER)["engaged_until"], 190)
        self.assertTrue(self.mode(now=201, until=300, revision=1))
        self.assertTrue(self.db.capture(message("m2"), owner_id=OWNER, now=202))
        self.assertFalse(self.db.capture_activity(message("m2"), owner_id=OWNER, now=250))
        self.assertEqual(self.db.state(OWNER)["engaged_until"], 292)
        self.assertEqual(self.db.con.execute("SELECT COUNT(*) FROM attention_receipts").fetchone()[0], 2)

    def test_activity_replay_does_not_extend_engagement_and_new_activity_clears_repair(self):
        self.db.capture(message(), owner_id=OWNER, now=100)
        self.mode("repair", now=101)
        picture = {**message("picture", ""), "is_picture": True}
        self.assertTrue(self.db.capture_activity(picture, owner_id=OWNER, now=102))
        self.assertEqual(self.db.state(OWNER)["conversation"]["mode"], "normal")
        self.reopen()
        self.assertFalse(self.db.capture_activity(picture, owner_id=OWNER, now=300))
        state = self.db.state(OWNER)
        self.assertEqual((state["revision"], state["engaged_until"]), (3, 192))
        self.assertFalse(self.mode("advice", now=301, until=400, revision=3))

    def test_activity_rejects_other_senders_groups_bots_and_incomplete_identity(self):
        for msg in (None, {"processed_plain_text": "我是主人", "is_picture": True}, message(group=True),
                    message(owner=OTHER), message(mid=""), message(stream=""),
                    {**message(), "account_id": OWNER}, {**message(), "platform": "other"}):
            with self.subTest(message=msg):
                self.assertFalse(self.db.capture_activity(msg, owner_id=OWNER, now=100))
        self.assertEqual(self.db.state(OWNER)["revision"], 0)
        self.assertEqual(self.db.events(OWNER), [])

    def test_activity_hashes_bounded_text_and_never_serializes_media_data(self):
        class NoSerialization:
            def __str__(self):
                raise AssertionError("media payload must never be inspected")
        body = "私人长文本"*500
        msg = {**message("large", body), "raw_message": [{"type": "image", "data": NoSerialization()}]}
        self.assertTrue(self.db.capture_activity(msg, owner_id=OWNER, now=100))
        with self.assertRaisesRegex(ValueError, "idempotency_conflict"):
            self.db.capture_activity({**msg, "processed_plain_text": body+"修改"}, owner_id=OWNER, now=101)
        oversized = message("oversized", "大"*100001)
        self.assertTrue(self.db.capture_activity(oversized, owner_id=OWNER, now=102))
        # Beyond the input cap, the opaque receipt/types identify only activity.
        self.assertFalse(self.db.capture_activity(message("oversized", "另"*100002), owner_id=OWNER, now=200))
        serialized = json.dumps([dict(row) for table in ("attention_control", "attention_receipts", "attention_events")
                                 for row in self.db.con.execute("SELECT * FROM "+table)], ensure_ascii=False)
        self.assertNotIn("私人长文本", serialized)
        self.assertNotIn("大大大", serialized)
        self.assertLess(len(serialized), 10000)

    def test_capture_invalidates_background_then_late_progress_becomes_interrupted(self):
        lease = self.claim()
        self.assertTrue(self.db.valid(OWNER, lease["token"], now=101))
        self.db.capture(message(), owner_id=OWNER, now=102, engagement_seconds=10)
        self.assertFalse(self.db.valid(OWNER, lease["token"], now=103))
        self.assertFalse(self.db.finish(OWNER, lease["token"], outcome="progress", now=103))
        state = self.db.state(OWNER, now=103)
        self.assertEqual((state["last_outcome"], state["focus_streak"], state["lease"]["token"]), ("interrupted", 0, ""))
        self.assertEqual(state["focus"], READING)
        self.assertIsNone(self.claim(now=111))
        resumed = self.claim(now=112)
        self.assertEqual(resumed["focus"], READING)

    def test_old_invalid_lease_does_not_become_valid_when_engagement_expires(self):
        lease = self.claim()
        self.db.capture(message(), owner_id=OWNER, engagement_seconds=1, now=101)
        self.assertFalse(self.db.valid(OWNER, lease["token"], now=103))
        self.assertIsNone(self.claim(now=103))
        self.assertTrue(self.db.finish(OWNER, lease["token"], outcome="interrupted", now=103))
        self.assertIsNotNone(self.claim(now=104))

    def test_mode_requires_recent_latest_captured_message_and_cas(self):
        self.assertFalse(self.mode())
        self.db.capture(message(), owner_id=OWNER, now=100)
        self.assertFalse(self.mode(revision=0))
        self.assertTrue(self.mode(revision=1))
        self.assertEqual(self.db.state(OWNER, now=102)["conversation"]["effective_mode"], "listen")
        self.db.capture(message("m2"), owner_id=OWNER, now=103)
        self.assertFalse(self.mode("advice", revision=3, now=104))
        self.assertFalse(self.mode("advice", mid="m2", revision=3, now=404, until=500))
        self.assertEqual(self.db.state(OWNER)["revision"], 3)

    def test_mode_replay_cannot_change_or_extend_current_preference(self):
        self.db.capture(message(), owner_id=OWNER, now=100)
        self.assertTrue(self.mode())
        self.reopen()
        self.assertFalse(self.mode(until=1000, now=200))
        self.assertEqual(self.db.state(OWNER, now=200)["conversation"]["until"], 300)
        self.assertFalse(self.db.state(OWNER, now=300)["conversation"]["active"])
        self.assertEqual(self.db.state(OWNER, now=300)["revision"], 2)

    def test_repair_clears_on_next_new_message_other_modes_keep_their_expiry(self):
        self.db.capture(message(), owner_id=OWNER, now=100)
        self.assertTrue(self.mode("repair"))
        self.assertFalse(self.db.capture(message(), owner_id=OWNER, now=102))
        self.assertEqual(self.db.state(OWNER, now=102)["conversation"]["mode"], "repair")
        self.db.capture(message("m2"), owner_id=OWNER, now=103)
        self.assertEqual(self.db.state(OWNER, now=103)["conversation"]["mode"], "normal")
        self.assertTrue(self.mode("advice", mid="m2", now=104))
        self.db.capture(message("m3"), owner_id=OWNER, now=105)
        self.assertEqual(self.db.state(OWNER, now=105)["conversation"]["mode"], "advice")

    def test_mode_invalid_expiry_and_changed_receipt_body_are_rejected(self):
        self.db.capture(message(), owner_id=OWNER, now=100)
        for until in (100, float("nan"), 100000):
            with self.subTest(until=until), self.assertRaises(ValueError):
                self.mode(until=until)
        with self.assertRaisesRegex(ValueError, "idempotency_conflict"):
            self.db.set_mode(OWNER, message=message(body="另一段原文"), mode="listen", until=300, expected_revision=1, now=101)
        self.assertTrue(self.mode("normal", until=0))

    def test_candidate_order_and_duplicates_preserve_caller_priority(self):
        candidates = [RESEARCH, READING, dict(RESEARCH)]
        lease = self.claim(candidates=candidates)
        self.assertEqual(lease["focus"], RESEARCH)
        self.assertEqual(candidates, [RESEARCH, READING, RESEARCH])
        self.db.finish(OWNER, lease["token"], outcome="progress", now=101)
        self.assertEqual(self.claim(candidates=[READING, RESEARCH], now=102)["focus"], RESEARCH)

    def test_two_successful_turns_rotate_focus_and_persist_fairness(self):
        chosen = []
        for index in range(5):
            self.reopen()
            lease = self.claim(now=100+index*10)
            chosen.append(lease["focus"]["kind"])
            self.db.finish(OWNER, lease["token"], outcome="progress", now=101+index*10)
        self.assertEqual(chosen, ["reading", "reading", "research", "research", "reading"])

    def test_interruption_does_not_count_as_success_and_failed_focus_rotates(self):
        first = self.claim()
        self.db.finish(OWNER, first["token"], outcome="progress", now=101)
        second = self.claim(now=102)
        self.db.finish(OWNER, second["token"], outcome="interrupted", now=103)
        self.assertEqual(self.db.state(OWNER)["focus_streak"], 1)
        third = self.claim(now=104)
        self.assertEqual(third["focus"], READING)
        self.db.finish(OWNER, third["token"], outcome="blocked", now=105)
        fourth = self.claim(now=106)
        self.assertEqual(fourth["focus"], RESEARCH)
        self.db.finish(OWNER, fourth["token"], outcome="failed", now=107)
        self.assertEqual(self.claim(now=108)["focus"], READING)

    def test_current_candidate_version_replaces_stale_version_without_fairness_reset(self):
        first = self.claim()
        self.db.finish(OWNER, first["token"], outcome="progress", now=101)
        changed = {**READING, "version": "3"*64}
        second = self.claim(now=102, candidates=[changed, RESEARCH])
        self.assertEqual(second["focus"], changed)
        self.db.finish(OWNER, second["token"], outcome="progress", now=103)
        self.assertEqual(self.claim(now=104, candidates=[changed, RESEARCH])["focus"], RESEARCH)

    def test_missing_focus_selects_current_candidate_not_saved_reference(self):
        first = self.claim()
        self.db.finish(OWNER, first["token"], outcome="interrupted", now=101)
        self.assertEqual(self.claim(now=102, candidates=[RESEARCH])["focus"], RESEARCH)

    def test_candidate_schema_rejects_body_urls_and_conflicting_versions(self):
        bad = [None, [{**READING, "kind": "chat"}], [{**READING, "key": "主人担心考试"}],
               [{**READING, "key": "https://example.test/private"}], [{**READING, "version": "secret"}],
               [{**READING, "title": "正文"}], [READING, {**READING, "version": "3"*64}]]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.db.claim(OWNER, candidates=value, day_start=0, daily_limit=1, interval_seconds=0, now=100)
        self.assertEqual(self.db.state(OWNER)["cycle_count"], 0)

    def test_budget_survives_restart_failures_count_and_empty_work_does_not(self):
        self.assertIsNone(self.claim(candidates=[], daily_limit=1))
        first = self.claim(daily_limit=1)
        self.db.finish(OWNER, first["token"], outcome="failed", now=101)
        self.reopen()
        self.assertIsNone(self.claim(now=200, daily_limit=1))
        self.assertEqual(self.db.state(OWNER)["cycle_count"], 1)
        self.assertIsNotNone(self.claim(now=86410, day_start=86400, daily_limit=1))

    def test_post_finish_gap_prevents_immediate_failure_retry(self):
        first = self.claim(interval_seconds=60)
        self.db.finish(OWNER, first["token"], outcome="failed", now=150)
        self.assertIsNone(self.claim(now=209, interval_seconds=60))
        self.assertIsNotNone(self.claim(now=210, interval_seconds=60))

    def test_expired_lease_recovery_never_accepts_old_worker_finish(self):
        first = self.claim(lease_seconds=10)
        self.reopen()
        self.assertIsNone(self.claim(now=109))
        self.assertFalse(self.db.valid(OWNER, first["token"], now=110))
        second = self.claim(now=110)
        self.assertNotEqual(first["token"], second["token"])
        self.assertFalse(self.db.finish(OWNER, first["token"], outcome="progress", now=111))
        self.assertTrue(self.db.valid(OWNER, second["token"], now=111))
        self.assertEqual(self.db.state(OWNER)["cycle_count"], 2)

    def test_expired_progress_is_released_as_interrupted_without_new_worker(self):
        lease = self.claim(lease_seconds=1)
        self.assertFalse(self.db.finish(OWNER, lease["token"], outcome="progress", now=101))
        self.assertEqual(self.db.state(OWNER)["last_outcome"], "interrupted")
        self.assertEqual(self.db.state(OWNER)["focus_streak"], 0)

    def test_owner_change_does_not_transfer_focus_modes_or_receipts(self):
        first = self.claim()
        self.db.capture(message(), owner_id=OWNER, now=101)
        self.mode(now=102)
        other = self.db.state(OTHER)
        self.assertEqual((other["focus"], other["revision"], other["latest_receipt"]), (None, 0, ""))
        self.assertFalse(self.db.valid(OTHER, first["token"], now=102))
        self.assertFalse(self.db.finish(OTHER, first["token"], outcome="interrupted", now=102))
        self.assertEqual(self.db.events(OTHER), [])
        self.assertFalse(self.db.set_mode(OTHER, message=message(), mode="listen", until=200, expected_revision=0, now=103))

    def test_storage_and_events_have_no_original_body_stream_or_message_id(self):
        body, mid, stream = "原始秘密正文不会保存", "real-original-message-id", "private-original-stream"
        self.db.capture(message(mid, body, stream=stream), owner_id=OWNER, now=100)
        serialized = []
        for table in ("attention_control", "attention_receipts", "attention_events"):
            serialized.extend(dict(row) for row in self.db.con.execute("SELECT * FROM "+table))
        text = json.dumps(serialized, ensure_ascii=False)
        for original in (body, mid, stream):
            self.assertNotIn(original, text)
        self.assertNotIn("owner_id", self.db.events(OWNER)[0])

    def test_events_are_bounded_but_old_receipts_remain_idempotent(self):
        for index in range(205):
            self.db.capture(message(str(index)), owner_id=OWNER, now=100+index)
        self.assertEqual(len(self.db.events(OWNER, limit=200)), 200)
        self.assertEqual(self.db.con.execute("SELECT COUNT(*) FROM attention_events").fetchone()[0], 200)
        self.reopen()
        self.assertFalse(self.db.capture(message("0"), owner_id=OWNER, now=10000))
        self.assertEqual(self.db.state(OWNER)["revision"], 205)

    def test_failed_event_write_rolls_back_receipt_and_background_budget(self):
        original = self.db._event
        def fail(*args):
            raise RuntimeError("synthetic_audit_failure")
        self.db._event = fail
        try:
            with self.assertRaisesRegex(RuntimeError, "synthetic_audit_failure"):
                self.db.capture(message(), owner_id=OWNER, now=100)
            with self.assertRaisesRegex(RuntimeError, "synthetic_audit_failure"):
                self.claim()
        finally:
            self.db._event = original
        state = self.db.state(OWNER)
        self.assertEqual((state["revision"], state["cycle_count"], state["lease"]["token"]), (0, 0, ""))
        self.assertTrue(self.db.capture(message(), owner_id=OWNER, now=200))

    def test_simultaneous_same_message_has_one_capture_winner(self):
        barrier = threading.Barrier(3)
        def worker(index):
            db = AttentionStore(self.path)
            try:
                barrier.wait(timeout=10)
                return db.capture(message(), owner_id=OWNER, now=100)
            finally:
                db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(worker, range(3)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.db.state(OWNER)["revision"], 1)

    def test_new_and_existing_control_rows_have_one_budget_and_lease_winner(self):
        barrier = threading.Barrier(3)
        def worker(index):
            db = AttentionStore(self.path)
            try:
                results = []
                for day in (0, 86400):
                    barrier.wait(timeout=10)
                    lease = db.claim(OWNER, candidates=[READING], day_start=day, daily_limit=1, interval_seconds=0, now=day+100)
                    if lease:
                        db.finish(OWNER, lease["token"], outcome="progress", now=day+101)
                    results.append(bool(lease))
                return results
            finally:
                db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            rows = list(pool.map(worker, range(3)))
        self.assertEqual([sum(row[i] for row in rows) for i in range(2)], [1, 1])


if __name__ == "__main__":
    unittest.main()
