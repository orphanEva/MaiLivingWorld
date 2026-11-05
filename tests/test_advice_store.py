"""Durable actual delivery, owner reports, revocation and bounded feedback leases."""
from pathlib import Path
import tempfile
import unittest

from MaiLivingWorld.advice_store import AdviceStore

OWNER, OTHER = "12345678", "87654321"
ADVICE = "先列一张小清单，再开始一个任务"


def message(text, mid="in-1", uid=OWNER):
    return {"platform": "qq", "session_id": "private-one", "message_id": mid, "account_id": "99999999",
            "processed_plain_text": text, "message_info": {"user_info": {"user_id": uid}, "group_info": None}}


def reference():
    return {"id": "tool:observation:"+"a"*64, "digest": "b"*64, "scope": "private:"+OWNER, "sensitivity": "personal"}


class AdviceChecks:
    def prepared(self, db, now=100):
        return db.prepare(OWNER, "private-one", "question", "学习安排", ADVICE, [reference()], now=now)

    def delivered(self, db):
        row = self.prepared(db)
        self.assertTrue(db.record_delivery(OWNER, row["id"], row["revision"], "private-one", row["delivery_text"], "c"*64, now=101))
        return db.get(OWNER, row["id"])

    def command(self, row, label="有用", mid="command"):
        return message("/建议 "+label+" "+row["id"]+" "+str(row["revision"]), mid)

    def test_delivery_is_exact_once_and_reports_survive_restart(self):
        db = self.store()
        row = self.prepared(db)
        self.assertEqual(row["outcome"], "unknown")
        self.assertFalse(db.record_delivery(OWNER, row["id"], 1, "other", row["delivery_text"], "c"*64, now=101))
        self.assertFalse(db.record_delivery(OWNER, row["id"], 1, "private-one", "引用："+row["delivery_text"], "c"*64, now=101))
        self.assertTrue(db.record_delivery(OWNER, row["id"], 1, "private-one", row["delivery_text"], "c"*64, now=101))
        self.assertFalse(db.record_delivery(OWNER, row["id"], 1, "private-one", row["delivery_text"], "c"*64, now=102))
        reader = self.store()
        sent = reader.get(OWNER, row["id"])
        self.assertTrue(reader.command(self.command(sent), OWNER, now=103))
        self.assertEqual(db.get(OWNER, row["id"])["outcome"], "helpful")
        self.assertFalse(reader.command(self.command(sent), OWNER, now=104))
        self.assertIsNone(db.get(OTHER, row["id"]))

    def test_expired_draft_or_owner_and_media_cannot_confirm(self):
        db = self.store()
        draft = self.prepared(db)
        self.assertFalse(db.record_delivery(OWNER, draft["id"], 1, "private-one", draft["delivery_text"], "c"*64, now=701))
        with self.assertRaises(ValueError):
            db.command(message(self.command(draft)["processed_plain_text"], uid=OTHER), OWNER, now=102)
        media = self.command(draft)
        media["raw_message"] = [{"type": "image"}]
        with self.assertRaises(ValueError):
            db.command(media, OWNER, now=102)
        self.assertFalse(db.command(self.command(draft), OWNER, now=103))
        self.assertEqual(db.get(OWNER, draft["id"])["status"], "draft")

    def test_forget_wipes_evidence_and_blocks_late_results_and_replay(self):
        db = self.store()
        row = self.delivered(db)
        event = message("你说的“先列一张小清单”我试了，没有帮助", "feedback")
        db.enqueue(event, OWNER, row["id"], row["revision"], now=102)
        claim = db.claim(OWNER, day_start=0, interval_seconds=60, daily_limit=4, now=103)
        self.assertTrue(db.command(self.command(row, "忘记"), OWNER, now=104))
        self.assertFalse(db.finish(OWNER, claim, "unhelpful", "我试了，没有帮助", now=105))
        value = db.get(OWNER, row["id"])
        self.assertEqual((value["status"], value["advice"], value["source_refs"], value["feedback"]), ("forgotten", "", [], ""))
        self.assertIsNone(db.enqueue(event, OWNER, row["id"], row["revision"], now=106))
        with self.assertRaises(ValueError):
            self.prepared(db, 107)
        with self.assertRaises(ValueError):
            db.prepare(OWNER, "private-one", "new-question", "学习安排", ADVICE, [reference()], now=107)
        self.assertTrue(all(not r["text"] for r in [db._decode(x) for x in db._read("SELECT * FROM advice_inputs")]))

    def test_feedback_requires_unique_literal_and_budget_is_persistent(self):
        db = self.store()
        row = self.delivered(db)
        db.enqueue(message("你说的“先列一张小清单”我试了，没有帮助"), OWNER, row["id"], row["revision"], now=102)
        claim = db.claim(OWNER, day_start=0, interval_seconds=60, daily_limit=1, now=103)
        with self.assertRaises(ValueError):
            db.finish(OWNER, claim, "helpful", "我试了，非常有帮助", now=104)
        self.assertTrue(db.finish(OWNER, claim, "unhelpful", "我试了，没有帮助", now=104))
        self.assertFalse(db.finish(OWNER, claim, "helpful", "我试了，没有帮助", now=105))
        reader = self.store()
        row = reader.get(OWNER, row["id"])
        reader.enqueue(message("你说的“先列一张小清单”我后来试了，有帮助", "new-feedback"), OWNER, row["id"], row["revision"], now=106)
        self.assertIsNone(reader.claim(OWNER, day_start=0, interval_seconds=60, daily_limit=1, now=1000))
        self.assertEqual(reader.state(OWNER)["cycle_count"], 1)

    def test_newer_report_invalidates_old_worker_and_preserves_new_input(self):
        db = self.store()
        row = self.delivered(db)
        first = message("你说的“先列一张小清单”我试了，没有帮助", "first")
        second = message("你说的“先列一张小清单”我后来又试了，有帮助", "second")
        db.enqueue(first, OWNER, row["id"], row["revision"], now=102)
        old = db.claim(OWNER, day_start=0, interval_seconds=60, daily_limit=4, now=103)
        db.enqueue(second, OWNER, row["id"], row["revision"], now=104)
        self.assertFalse(db.valid(OWNER, old, now=105))
        self.assertFalse(db.finish(OWNER, old, "unhelpful", "我试了，没有帮助", now=105))
        new = db.claim(OWNER, day_start=0, interval_seconds=60, daily_limit=4, now=164)
        self.assertEqual(new["text"], second["processed_plain_text"])
        self.assertTrue(db.finish(OWNER, new, "helpful", "我后来又试了，有帮助", now=165))

    def test_stop_and_old_command_cannot_change_feedback(self):
        db = self.store()
        row = self.delivered(db)
        self.assertTrue(db.command(self.command(row, "停止"), OWNER, now=102))
        self.assertFalse(db.command(self.command(row, "有用", "late"), OWNER, now=103))
        stopped = db.get(OWNER, row["id"])
        self.assertFalse(db.command(self.command(stopped, "有用", "another"), OWNER, now=104))
        self.assertEqual(db.get(OWNER, row["id"])["outcome"], "unknown")

    def test_two_queued_reports_select_the_newer_specific_report(self):
        db = self.store()
        row = self.delivered(db)
        db.enqueue(message("你说的“先列一张小清单”我试了，有帮助", "old"), OWNER, row["id"], row["revision"], now=102)
        db.enqueue(message("你说的“先列一张小清单”后来又试了，其实没有帮助", "new"), OWNER, row["id"], row["revision"], now=103)
        claim = db.claim(OWNER, day_start=0, interval_seconds=60, daily_limit=4, now=104)
        self.assertIn("其实没有帮助", claim["text"])
        self.assertTrue(db.finish(OWNER, claim, "unhelpful", "后来又试了，其实没有帮助", now=105))
        self.assertEqual(db.get(OWNER, row["id"])["outcome"], "unhelpful")

    def test_expired_lease_and_finite_retries_cannot_commit(self):
        db = self.store()
        row = self.delivered(db)
        db.enqueue(message("你说的“先列一张小清单”我没尝试"), OWNER, row["id"], row["revision"], now=102)
        for moment in (103, 170, 240):
            claim = db.claim(OWNER, day_start=0, interval_seconds=60, daily_limit=4, lease_seconds=1, now=moment)
            self.assertIsNotNone(claim)
            self.assertFalse(db.valid(OWNER, claim, now=moment+2))
        self.assertIsNone(db.claim(OWNER, day_start=0, interval_seconds=60, daily_limit=4, now=310))
        self.assertEqual(db.state(OWNER)["cycle_count"], 3)

    def test_unrelated_suggestion_does_not_discard_current_feedback(self):
        db = self.store()
        row = self.delivered(db)
        db.enqueue(message("你说的“先列一张小清单”我试了，没有帮助"), OWNER, row["id"], row["revision"], now=102)
        claim = db.claim(OWNER, day_start=0, interval_seconds=60, daily_limit=4, now=103)
        db.prepare(OWNER, "private-one", "another-question", "阅读安排", "先挑一小段阅读", [reference()], now=104)
        self.assertTrue(db.valid(OWNER, claim, now=105))
        self.assertTrue(db.finish(OWNER, claim, "unhelpful", "我试了，没有帮助", now=105))
        self.assertEqual(db.get(OWNER, row["id"])["outcome"], "unhelpful")


class AdviceStoreTests(AdviceChecks, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"advice.sqlite3"
        self.stores = []

    def store(self):
        db = AdviceStore(self.path)
        self.stores.append(db)
        return db

    def tearDown(self):
        for db in self.stores:
            db.close()
        self.temp.cleanup()
