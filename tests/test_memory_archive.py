"""Growing beyond an active window preserves exact evidence and owner control."""
from __future__ import annotations

import concurrent.futures
import hashlib
from pathlib import Path
import tempfile
import threading
import unittest

from MaiLivingWorld.advice_store import AdviceStore
from MaiLivingWorld.repair_store import RepairStore
from MaiLivingWorld.memory_archive import tier


OWNER, OTHER = "12345678", "87654321"
REF = {"id": "tool:observation:"+"a"*64, "digest": "b"*64, "scope": "private:"+OWNER, "sensitivity": "personal"}


def message(text, mid):
    return {"platform": "qq", "message_id": mid, "session_id": "private-one", "account_id": "99999999",
            "processed_plain_text": text, "message_info": {"user_info": {"user_id": OWNER}, "group_info": None}}


class ArchiveChecks:
    def advice(self, db, index, *, delivered=True, enabled=False, now=None):
        now = 100+index*2 if now is None else now
        row = db.prepare(OWNER, "private-one", "q-"+str(index), "事项%03d" % index,
                         "安排%03d先核对资料再决定" % index, [REF], archive_enabled=enabled, now=now)
        if delivered:
            self.assertTrue(db.record_delivery(OWNER, row["id"], 1, "private-one", row["delivery_text"],
                                              hashlib.sha256(str(index).encode()).hexdigest(), now=now+1))
        return db.get(OWNER, row["id"])

    def repair(self, db, index, *, enabled=False):
        now = 100+index*90000
        old = "事项%03d的安排在周五" % index
        original = "我记得"+old+"，可以提前准备。"
        correction = "你刚才说“"+old+"”，其实这件事安排在周六。"
        receipt = {"owner": OWNER, "sid": "private-one", "mid": "s-"+str(index), "text": original,
                   "receipt_id": hashlib.sha256(str(index).encode()).hexdigest(), "text_digest": hashlib.sha256(original.encode()).hexdigest()}
        self.assertTrue(db.record_delivery(OWNER, receipt, now=now))
        source = db.recent(OWNER, now=now)[0]
        event = message(correction, "c-"+str(index))
        self.assertTrue(db.enqueue(event, OWNER, source["id"], source["digest"], now=now+1))
        claim = db.claim(OWNER, day_start=now//86400*86400, daily_limit=24, interval_seconds=0, now=now+2)
        self.assertTrue(db.finish(OWNER, claim, {"old_quote": old, "correction_quote": correction},
                                 archive_enabled=enabled, now=now+3))
        item = db.search(OWNER, old, limit=1)[0]
        return item, receipt, event

    def test_advice_archives_oldest_without_rewriting_evidence_and_pages_history(self):
        db = self.store("advice")
        original = self.advice(db, 0)
        for index in range(1, 64):
            self.advice(db, index)
        with self.assertRaisesRegex(ValueError, "advice_capacity"):
            self.advice(db, 64)
        self.advice(db, 64, enabled=True)
        counts = db.archive_status(OWNER)
        self.assertEqual((counts["active"], counts["archived"], counts["total"]), (64, 1, 65))
        archived = db.get(OWNER, original["id"])
        self.assertEqual(tier(archived), "archive")
        self.assertEqual({k: archived[k] for k in original}, original)
        self.assertEqual(len(db.items(OWNER)), 64)
        pages = [db.search(OWNER, offset=offset) for offset in (0, 20, 40, 60)]
        self.assertEqual(len({r["id"] for page in pages for r in page}), 65)
        self.assertEqual(len(pages[-1]), 5)
        self.assertEqual(db.search(OTHER), [])
        self.assertEqual(db.search(OWNER, "事项000", limit=1)[0]["id"], original["id"])
        self.assertNotIn(original["id"], {r["id"] for r in db.recall(OWNER, "事项000")})
        self.assertEqual(db.recall(OWNER, "事项000", archive_enabled=True, limit=1)[0]["id"], original["id"])
        self.assertNotIn(original["id"], {r["id"] for r in db.recall(OWNER, archive_enabled=True)})
        self.assertNotIn(original["id"], {r["id"] for r in db.recall(OWNER, "完全无关", archive_enabled=True)})
        self.assertEqual(db.feedback_items(OWNER, "你说的“先核对资料再决定”我尝试了，没有帮助", archive_enabled=True), [])
        peer = self.store("advice")
        self.assertEqual(peer.get(OWNER, original["id"]), archived)
        with self.assertRaisesRegex(ValueError, "advice_already_known"):
            db.prepare(OWNER, "new-session", "new-q", original["topic"], original["advice"], [REF], archive_enabled=True, now=300)

    def test_forgotten_tombstones_free_capacity_only_when_enabled_and_never_revive(self):
        db = self.store("advice")
        original = self.advice(db, 0)
        for index in range(1, 64):
            self.advice(db, index)
        command = message("/建议 忘记 "+original["id"]+" "+str(original["revision"]), "forget")
        self.assertTrue(db.command(command, OWNER, now=250))
        with self.assertRaisesRegex(ValueError, "advice_capacity"):
            self.advice(db, 64)
        self.advice(db, 64, enabled=True)
        counts = db.archive_status(OWNER)
        self.assertEqual((counts["active"], counts["archived"], counts["tombstones"]), (64, 0, 1))
        self.assertEqual(tier(db.get(OWNER, original["id"])), "tombstone")
        self.assertEqual(db.search(OWNER, original["id"])[0]["advice"], "")
        with self.assertRaises(ValueError):
            self.advice(db, 0, enabled=True, now=260)
        self.assertFalse(db.command(command, OWNER, now=261))

    def test_archive_does_not_hide_still_deliverable_drafts(self):
        db = self.store("advice")
        for index in range(64):
            self.advice(db, index, delivered=False, now=100)
        with self.assertRaisesRegex(ValueError, "advice_capacity"):
            self.advice(db, 64, delivered=False, enabled=True, now=101)
        self.assertEqual(len(db.drafts(OWNER, now=101)), 64)
        self.assertEqual(db.archive_status(OWNER)["archived"], 0)
        self.advice(db, 64, delivered=False, enabled=True, now=701)
        self.assertEqual(len(db.drafts(OWNER, now=701)), 1)
        self.assertEqual(db.archive_status(OWNER)["archived"], 1)

    def test_archived_advice_can_receive_feedback_and_forget_revokes_inflight_result(self):
        db = self.store("advice")
        original = self.advice(db, 0)
        for index in range(1, 65):
            self.advice(db, index, enabled=True)
        text = "你说的“"+original["advice"]+"”我尝试了，没有帮助"
        self.assertEqual(db.feedback_items(OWNER, text, archive_enabled=True)[0]["id"], original["id"])
        self.assertEqual(db.feedback_items(OWNER, text), [])
        event = message(text, "feedback")
        self.assertTrue(db.enqueue(event, OWNER, original["id"], original["revision"], now=300))
        claim = db.claim(OWNER, day_start=0, interval_seconds=0, daily_limit=4, now=301)
        self.assertTrue(db.command(message("/建议 忘记 "+original["id"]+" "+str(original["revision"]), "forget-archive"), OWNER, now=302))
        self.assertFalse(db.finish(OWNER, claim, "unhelpful", "我尝试了，没有帮助", now=303))
        self.assertIsNone(db.enqueue(event, OWNER, original["id"], original["revision"], now=304))
        self.assertEqual(db.search(OWNER, original["id"])[0]["advice"], "")
        self.assertEqual(db.feedback_items(OWNER, text, archive_enabled=True), [])

    def test_repair_archive_survives_restart_and_forget_clears_old_quotations(self):
        db = self.store("repair")
        original, sent, event = self.repair(db, 0)
        for index in range(1, 65):
            self.repair(db, index, enabled=True)
        archived = db.get(OWNER, original["id"])
        self.assertEqual(tier(archived), "archive")
        self.assertEqual({k: archived[k] for k in original}, original)
        self.assertEqual(db.archive_status(OWNER)["active"], 64)
        self.assertEqual(db.recall(OWNER, original["old_quote"], archive_enabled=True, limit=1)[0]["id"], original["id"])
        self.assertNotIn(original["id"], {r["id"] for r in db.recall(OWNER, "完全无关", archive_enabled=True)})
        peer = self.store("repair")
        self.assertEqual(peer.get(OWNER, original["id"]), archived)
        now = 100+65*90000
        self.assertTrue(peer.command(message("/修复 忘记 "+original["id"]+" 1", "forget"), OWNER, now=now))
        forgotten = db.get(OWNER, original["id"])
        self.assertEqual((forgotten["old_quote"], forgotten["correction_quote"], tier(forgotten)), ("", "", "tombstone"))
        self.assertFalse(db.record_delivery(OWNER, sent, now=now+1))
        self.assertIsNone(db.enqueue(event, OWNER, original["delivery_id"], original["delivery_digest"], now=now+2))
        self.repair(db, 65, enabled=True)
        self.assertEqual(db.archive_status(OWNER)["active"], 64)
        self.assertEqual(db.archive_status(OWNER)["tombstones"], 1)

    def test_two_concurrent_writers_keep_one_bounded_active_window(self):
        db = self.store("advice")
        for index in range(64):
            self.advice(db, index)
        barrier = threading.Barrier(2)
        def worker(index):
            peer = self.store("advice")
            try:
                barrier.wait(timeout=10)
                return self.advice(peer, index, enabled=True)["id"]
            finally:
                peer.close()
                self.stores.remove(peer)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, (64, 65)))
        self.assertEqual(len(set(results)), 2)
        counts = db.archive_status(OWNER)
        self.assertEqual((counts["active"], counts["archived"], counts["total"]), (64, 2, 66))

    def test_search_inputs_are_bounded_and_literal_sql_patterns_cannot_broaden(self):
        db = self.store("advice")
        self.advice(db, 0)
        for options in ({"limit": 0}, {"limit": 66}, {"offset": -1}, {"offset": True}, {"query": "x"*1201}):
            with self.assertRaises(ValueError):
                db.search(OWNER, **options)
        self.assertEqual(db.search(OWNER, "unknown_%~' OR 1=1"), [])


class MemoryArchiveTests(ArchiveChecks, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.stores = []

    def store(self, kind):
        value = (AdviceStore if kind == "advice" else RepairStore)(Path(self.temp.name)/(kind+".sqlite3"))
        self.stores.append(value)
        return value

    def tearDown(self):
        for store in self.stores:
            store.close()
        self.temp.cleanup()
