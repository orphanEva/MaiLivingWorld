from __future__ import annotations

import concurrent.futures
import json
from pathlib import Path
import tempfile
import threading
import unittest

from MaiLivingWorld.habits_store import HabitsStore
from test_recollection_store import OWNER, OTHER, message


def proposal(text, *, situation="general", dimension="length", value="short", evidence="feedback"):
    return {"updates": [{"situation": situation, "dimension": dimension, "value": value,
                         "evidence": evidence, "quote": text}]}


class HabitLifecycleChecks:
    def store(self):
        return self.db

    def learn(self, text, now, **changes):
        db = self.store()
        db.enqueue(message("feedback-"+str(now), text), owner_id=OWNER, now=now)
        job = db.claim(OWNER, now=now+1)
        return db.complete(job["id"], job["token"], proposal(text, **changes), owner_id=OWNER,
                           expected_revision=job["base_revision"], now=now+2)

    def command(self, *, action="set", situation="general", dimension="length", value="short", now=100, mid=None):
        text = "/习惯 "+("设置" if action == "set" else "清除")+" "+situation+" "+dimension
        if action == "set":
            text += " "+value
        return self.store().command(message(mid or "cmd-"+str(now), text), owner_id=OWNER, action=action,
                                    situation=situation, dimension=dimension, value=value if action == "set" else "", now=now)

    def test_explicit_once_and_ordinary_feedback_do_not_overwrite_it(self):
        db = self.store()
        self.learn("以后回复请简短一些。", 100, evidence="explicit")
        first = db.preferences(OWNER)[0]
        self.assertEqual((first["status"], first["value"], first["evidence_count"]), ("active", "short", 1))
        for text, now in (("这次讲得太简略。", 200), ("刚才回答太简单。", 300), ("这个回答太短。", 90000)):
            self.assertEqual(self.learn(text, now, value="detailed")["ignored"], 1)
        self.assertEqual(db.preferences(OWNER)[0], first)
        self.learn("以后请给我详细的解释。", 91000, value="detailed", evidence="explicit")
        self.assertEqual(db.preferences(OWNER)[0]["value"], "detailed")

    def test_three_independent_feedbacks_must_span_at_least_a_day(self):
        db = self.store()
        for text, now in (("这次你回答太啰嗦。", 100), ("刚才的回复太长。", 200), ("这回的文字太冗长。", 300)):
            self.learn(text, now)
        row = db.preferences(OWNER)[0]
        self.assertEqual(row["status"], "pending")
        self.assertEqual(len(row["pending"]["short"]), 3)
        self.learn("今天这个回复太多字。", 86500)
        row = db.preferences(OWNER)[0]
        self.assertEqual((row["status"], row["value"], row["evidence_count"]), ("active", "short", 3))
        self.assertEqual(row["pending"], {})
        self.learn("这次的解释太简略。", 87000, value="detailed")
        self.assertEqual(db.preferences(OWNER)[0]["value"], "short")
        self.learn("刚才回答太简单。", 88000, value="detailed")
        self.learn("今天回答太短。", 173400, value="detailed")
        self.assertEqual(db.preferences(OWNER)[0]["value"], "detailed")
        # Old short evidence cannot combine with one new complaint to flip back.
        self.learn("你这段讲得太长。", 174000)
        self.assertEqual(db.preferences(OWNER)[0]["value"], "detailed")

    def test_normalized_duplicate_feedback_never_counts_as_independent(self):
        db = self.store()
        for text, now in (("这次你回答太啰嗦。", 100), ("这次你回答太啰嗦！", 90000), ("这次你回答太啰嗦。", 180000)):
            self.learn(text, now)
        row = db.preferences(OWNER)[0]
        self.assertEqual(row["status"], "pending")
        self.assertEqual(len(row["pending"]["short"]), 1)

    def test_feedback_trim_does_not_recount_old_content_with_a_new_message_id(self):
        db = self.store()
        for index in range(9):
            self.learn("这次第"+str(index)+"个回答太啰嗦。", 100+index*10)
        self.assertEqual(db.preferences(OWNER)[0]["status"], "pending")
        self.assertEqual(self.learn("这次第1个回答太啰嗦。", 90000)["ignored"], 1)
        self.assertEqual(db.preferences(OWNER)[0]["status"], "pending")

    def test_clear_invalidates_lease_and_blocks_older_queue_and_command_replay(self):
        db = self.store()
        body = "以后回复请详细解释。"
        db.enqueue(message("old", body), owner_id=OWNER, now=100)
        old = db.claim(OWNER, now=101)
        self.command(action="clear", now=200, mid="clear")
        with self.assertRaisesRegex(ValueError, "lease_conflict"):
            db.complete(old["id"], old["token"], proposal(body, value="detailed", evidence="explicit"),
                        owner_id=OWNER, expected_revision=old["base_revision"], now=201)
        again = db.claim(OWNER, now=202)
        stats = db.complete(again["id"], again["token"], proposal(body, value="detailed", evidence="explicit"),
                            owner_id=OWNER, expected_revision=again["base_revision"], now=203)
        self.assertEqual(stats["ignored"], 1)
        self.assertEqual(db.preferences(OWNER)[0]["status"], "cleared")
        self.command(value="short", now=300)
        self.assertFalse(self.command(action="clear", now=400, mid="clear"))
        self.assertEqual(db.preferences(OWNER)[0]["value"], "short")

    def test_commit_failure_is_atomic_and_does_not_clear_input(self):
        db = self.store()
        body = "以后回复请简短一些。"
        db.enqueue(message("failure", body), owner_id=OWNER, now=100)
        job = db.claim(OWNER, now=101)
        execute = db.con.execute
        def fail(sql, parameters=()):
            if sql.startswith("INSERT INTO habits_audit"):
                raise RuntimeError("synthetic_failure")
            return execute(sql, parameters)
        db.con.execute = fail
        try:
            with self.assertRaisesRegex(RuntimeError, "synthetic_failure"):
                db.complete(job["id"], job["token"], proposal(body, evidence="explicit"), owner_id=OWNER,
                            expected_revision=job["base_revision"], now=102)
        finally:
            db.con.execute = execute
        self.assertEqual(db.preferences(OWNER), [])
        self.assertEqual(db.state(OWNER)["revision"], 0)
        db.complete(job["id"], job["token"], proposal(body, evidence="explicit"), owner_id=OWNER,
                    expected_revision=job["base_revision"], now=103)
        self.assertEqual(db.preferences(OWNER)[0]["value"], "short")


class HabitsStoreTests(HabitLifecycleChecks, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"habits.sqlite3"
        self.db = HabitsStore(self.path)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def reopen(self):
        self.db.close()
        self.db = HabitsStore(self.path)

    def test_empty_constructor_read_does_not_create_rows(self):
        self.assertEqual(self.db.state(OWNER)["revision"], 0)
        self.assertEqual(self.db.preferences(OWNER), [])
        self.assertEqual(self.db.status(OWNER, now=100)["queued_inputs"], 0)
        for suffix in ("control", "inputs", "preferences", "audit"):
            self.assertEqual(self.db.con.execute("SELECT COUNT(*) FROM habits_"+suffix).fetchone()[0], 0)

    def test_command_trusted_text_and_enum_must_match(self):
        text = "/习惯 设置 默认 长度 简短"
        self.assertTrue(self.db.command(message(body=text), owner_id=OWNER, action="set", situation="general", dimension="length", value="short", now=100))
        for event in (message(body=text, owner=OTHER), message(body=text, group=True),
                      {**message(body=text), "is_picture": True}, {**message(body=text), "account_id": OWNER},
                      {**message(body=text), "raw_message": [{"type": "forward", "data": text}]}):
            with self.assertRaises(ValueError):
                self.db.command(event, owner_id=OWNER, action="set", situation="general", dimension="length", value="short", now=101)
        with self.assertRaisesRegex(ValueError, "unverified_command"):
            self.db.command(message("fake", text), owner_id=OWNER, action="set", situation="general", dimension="length", value="detailed", now=101)
        self.assertEqual(self.db.preferences(OTHER), [])

    def test_receipt_restart_idempotency_conflicting_body_and_no_permanent_text(self):
        body = "以后回复请简短一些。"
        ident = self.db.enqueue(message(body=body), owner_id=OWNER, now=100)
        job = self.db.claim(OWNER, now=101)
        self.db.complete(job["id"], job["token"], proposal(body, evidence="explicit"), owner_id=OWNER, expected_revision=0, now=102)
        self.reopen()
        self.assertEqual(self.db.enqueue(message(body=body), owner_id=OWNER, now=200), ident)
        self.assertIsNone(self.db.claim(OWNER, now=201))
        with self.assertRaisesRegex(ValueError, "idempotency_conflict"):
            self.db.enqueue(message(body="以后请详细回答。"), owner_id=OWNER, now=202)
        for suffix in ("inputs", "preferences", "audit"):
            rows = self.db._read("SELECT * FROM habits_"+suffix)
            self.assertNotIn(body, json.dumps(rows, ensure_ascii=False))
        self.assertEqual(self.db.preferences(OWNER)[0]["value"], "short")

    def test_one_message_id_cannot_change_between_natural_input_and_command(self):
        self.db.enqueue(message("same", "这次太啰嗦。"), owner_id=OWNER, now=100)
        with self.assertRaisesRegex(ValueError, "idempotency_conflict"):
            self.command(mid="same", now=101)
        self.command(mid="command-first", now=102)
        with self.assertRaisesRegex(ValueError, "idempotency_conflict"):
            self.db.enqueue(message("command-first", "这次太啰嗦。"), owner_id=OWNER, now=103)

    def test_queue_capacity_ttl_and_plain_text_validation(self):
        for index in range(32):
            self.db.enqueue(message(str(index), "这次太啰嗦。"), owner_id=OWNER, now=100, ttl_seconds=60)
        self.assertIsNone(self.db.enqueue(message("full", "这次太啰嗦。"), owner_id=OWNER, now=101))
        self.assertTrue(self.db.status(OWNER, now=101)["queue_full"])
        self.assertIsNone(self.db.claim(OWNER, now=160))
        self.assertFalse(self.db.status(OWNER, now=160)["queue_full"])
        for event in (message("bad", "短"), message("bad", "字"*601), message("bad", "/命令 请求"), message("bad", "这次太长。", owner=OTHER)):
            with self.assertRaises(ValueError):
                self.db.enqueue(event, owner_id=OWNER, now=161)

    def test_lease_restart_expiry_and_cas_guard(self):
        body = "这次太啰嗦。"
        self.db.enqueue(message(body=body), owner_id=OWNER, now=100)
        old = self.db.claim(OWNER, now=101, lease_seconds=2)
        self.reopen()
        self.assertIsNone(self.db.claim(OWNER, now=102))
        fresh = self.db.claim(OWNER, now=103)
        with self.assertRaisesRegex(ValueError, "lease_conflict"):
            self.db.complete(old["id"], old["token"], proposal(body), owner_id=OWNER, expected_revision=0, now=104)
        with self.assertRaisesRegex(ValueError, "revision_conflict"):
            self.db.complete(fresh["id"], fresh["token"], proposal(body), owner_id=OWNER, expected_revision=99, now=104)
        self.db.complete(fresh["id"], fresh["token"], proposal(body), owner_id=OWNER, expected_revision=0, now=105)

    def test_store_rechecks_quote_enums_and_input_hash(self):
        body = "这次太啰嗦。"
        self.db.enqueue(message(body=body), owner_id=OWNER, now=100)
        job = self.db.claim(OWNER, now=101)
        for value in (proposal("以后请简短。", evidence="explicit"), proposal(body, value="detailed"),
                      proposal(body, situation="technical"), proposal(body, evidence="explicit"), proposal(body, value=True)):
            with self.assertRaisesRegex(ValueError, "invalid_proposal"):
                self.db.complete(job["id"], job["token"], value, owner_id=OWNER, expected_revision=0, now=102)
        with self.db.con:
            self.db.con.execute("UPDATE habits_inputs SET payload=? WHERE id=?", (json.dumps({"text": "以后请详细回答。"}), job["id"]))
        with self.assertRaisesRegex(ValueError, "input_changed"):
            self.db.complete(job["id"], job["token"], proposal(body), owner_id=OWNER, expected_revision=0, now=103)

    def test_failed_attempts_three_clear_body_but_defer_does_not_count(self):
        self.db.enqueue(message(body="这次太啰嗦。"), owner_id=OWNER, now=100)
        job = self.db.claim(OWNER, now=101)
        self.db.defer(job["id"], job["token"], owner_id=OWNER, reason="budget", retry_at=200, now=102)
        now = 200
        for attempt in range(3):
            job = self.db.claim(OWNER, now=now)
            self.db.fail(job["id"], job["token"], owner_id=OWNER, reason="model_failed", now=now+1)
            now += 1000
        self.assertEqual(self.db.status(OWNER, now=now)["inputs"], {"failed": 1})
        row = self.db._read("SELECT attempts,payload FROM habits_inputs")[0]
        self.assertEqual(row["attempts"], 3)
        self.assertEqual(json.loads(row["payload"])["text"], "")

    def test_budget_restart_and_next_day_are_independent_of_revision(self):
        self.assertTrue(self.db.reserve_cycle(OWNER, day_start=0, interval_seconds=10, daily_limit=2, now=100))
        self.reopen()
        self.assertFalse(self.db.reserve_cycle(OWNER, day_start=0, interval_seconds=10, daily_limit=2, now=109))
        self.assertTrue(self.db.reserve_cycle(OWNER, day_start=0, interval_seconds=10, daily_limit=2, now=110))
        self.assertFalse(self.db.reserve_cycle(OWNER, day_start=0, interval_seconds=10, daily_limit=2, now=200))
        self.assertTrue(self.db.reserve_cycle(OWNER, day_start=86400, interval_seconds=10, daily_limit=2, now=86500))
        self.assertEqual(self.db.state(OWNER)["revision"], 0)

    def test_slots_bounded_by_fixed_situations_and_dimensions(self):
        for situation in ("general", "work", "technical", "venting", "interest"):
            for dimension, value in (("length", "short"), ("support", "listen"), ("questions", "fewer")):
                self.command(situation=situation, dimension=dimension, value=value, mid=situation+dimension)
        self.assertEqual(len(self.db.preferences(OWNER)), 15)
        self.assertEqual(self.db.status(OWNER)["active_preferences"], 15)

    def test_competing_connections_one_receipt_and_budget_winner(self):
        barrier = threading.Barrier(3)
        def worker(index):
            db = HabitsStore(self.path)
            try:
                barrier.wait(timeout=10)
                ident = db.enqueue(message(body="这次太啰嗦。"), owner_id=OWNER, now=100)
                wins = []
                for day in (0, 86400):
                    barrier.wait(timeout=10)
                    wins.append(db.reserve_cycle(OWNER, day_start=day, interval_seconds=0, daily_limit=1, now=day+101))
                return ident, wins
            finally:
                db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            rows = list(pool.map(worker, range(3)))
        self.assertEqual(len({row[0] for row in rows}), 1)
        self.assertEqual([sum(row[1][index] for row in rows) for index in range(2)], [1, 1])


if __name__ == "__main__":
    unittest.main()
