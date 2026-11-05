"""Event reactions share growth transactions, history and durable source receipts."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from MaiLivingWorld.emotion_episodes import HALF_LIFE, TTL, MAX_ITEMS
from MaiLivingWorld.growth_model import empty_growth
from MaiLivingWorld.growth_store import GrowthStore


TEXT = "雪原故事结局留下了未解的问题，人物之间的互相理解让我印象深刻。"


def record(ident="A", text=TEXT, *, now=100, origin=None, scope="private:12345678", **changes):
    return {"id": "brain:event:"+ident, "source": "brain_event", "kind": "experience", "text": text,
            "scope": scope, "subject_id": "12345678" if scope != "public" else "",
            "sensitivity": "personal" if scope != "public" else "public", "evidence_status": "recorded_event",
            "timestamp": now, "origin_key": origin or "event:"+ident, **changes}


def proposal(rows, feelings=("appreciative", "uncertain")):
    result = empty_growth(emotional_episodes=True)
    for feeling in feelings:
        result["emotions"].append({"topic": "雪原结局", "feeling": feeling,
            "appraisal": "对故事里人物互相理解的描写仍有印象" if feeling == "appreciative" else "对故事留下的问题还拿不准",
            "intensity": .4, "evidence_ids": [r["id"] for r in rows],
            "quotes": [{"source_id": r["id"], "quote": r["text"]} for r in rows]})
    return result


class EmotionChecks:
    def submit(self, rows, data=None, *, now=100, mode="active", enabled=True):
        jid = self.db.enqueue(rows, mode=mode, now=now)
        job = self.db.claim(now=now, mode=mode)
        self.assertEqual(job["id"], jid)
        return self.db.complete(jid, job["token"], data or proposal(rows), rows, now=now+1,
            expected_revision=self.db.state(now=now)["revision"], emotional_episodes=enabled)

    def test_mixed_reactions_survive_restart_and_decay_without_rewriting_history(self):
        rows = [record()]
        result = self.submit(rows)
        self.assertEqual(result["emotions"], 2)
        state = self.db.state(now=101)
        self.assertEqual({r["feeling"] for r in state["emotions"]}, {"appreciative", "uncertain"})
        original = self.db.history()[0]
        self.db.close()
        self.db = self.open_store()
        later = self.db.state(now=101+HALF_LIFE)
        self.assertTrue(all(r["intensity"] == .2 for r in later["emotions"]))
        self.assertEqual(self.db.history()[0], original)
        self.assertEqual(later["revision"], state["revision"])
        self.assertEqual(self.db.state(now=101+TTL)["emotions"], [])
        self.assertEqual(original["after"]["emotions"][0]["intensity"], .4)

    def test_origin_and_content_aliases_do_not_refresh_reaction(self):
        self.submit([record()])
        before = self.db.state(now=102)
        for n, row in enumerate((record("B", "另一条同事件的记录", now=110, origin="event:A"),
                                 record("C", TEXT, now=110)), 1):
            result = self.submit([row], now=110+n)
            self.assertFalse(result["changed"])
            self.assertEqual(result["emotions"], 0)
        self.assertEqual(self.db.state(now=102), before)

    def test_rollback_cannot_restore_consumable_emotional_stimulus(self):
        self.submit([record()])
        self.db.rollback(1, now=110)
        self.assertEqual(self.db.state(now=111).get("emotions", []), [])
        result = self.submit([record("alias", now=112)], now=112)
        self.assertFalse(result["changed"])
        self.assertEqual(self.db.state(now=113).get("emotions", []), [])
        self.assertGreater(self.db.status()["consumed"], 0)

    def test_observe_does_not_create_reactions_or_consume_future_active_evidence(self):
        self.submit([record()], mode="observe")
        self.assertEqual(self.db.state(now=101).get("emotions", []), [])
        self.assertEqual(self.db.status()["consumed"], 0)
        result = self.submit([record()], now=110)
        self.assertEqual(result["emotions"], 2)

    def test_expired_reactions_make_room_without_reusing_old_event(self):
        for n in range(MAX_ITEMS+1):
            row = record(str(n), f"第{n}次实际阅读的新段落提供了不同材料。", now=100+n*10)
            self.submit([row], proposal([row], feelings=("curious",)), now=100+n*10)
        state = self.db.state(now=100+MAX_ITEMS*10+1)
        self.assertEqual(len(state["emotions"]), MAX_ITEMS)
        self.assertFalse(any(r["sources"][0]["id"] == "brain:event:0" for r in state["emotions"]))
        self.assertEqual(len(self.db.history(limit=100)), MAX_ITEMS+1)
        now = TTL+1000
        fresh = record("fresh", "多年冻土的介绍带来了另一个新问题。", now=now)
        self.submit([fresh], proposal([fresh], feelings=("curious",)), now=now)
        self.assertEqual(len(self.db.state(now=now+1)["emotions"]), 1)
        repeated = record("first-alias", "第0次实际阅读的新段落提供了不同材料。", now=now+10)
        self.assertFalse(self.submit([repeated], now=now+10)["changed"])

    def test_old_or_future_sources_cannot_create_a_current_emotion(self):
        for ident, timestamp, now in (("old", 100, TTL+200), ("future", 2*TTL, TTL+210)):
            row = record(ident, ident+"独立事件明确记录了阅读体验", now=timestamp)
            self.assertFalse(self.submit([row], now=now)["changed"])
        self.assertEqual(self.db.state(now=TTL+220).get("emotions", []), [])

    def test_transaction_rechecks_quotes_and_denies_owner_mood_absence_and_extra_fields(self):
        cases = [record("owner", evidence_status="owner_reported"),
                 record("absence", "主人没有回复，我感到被冷落。"),
                 record("preference", kind="preference"),
                 record("report", source="continuity")]
        for n, row in enumerate(cases):
            with self.assertRaisesRegex(ValueError, "invalid_emotion_evidence"):
                self.submit([row], now=110+n)
        self.assertEqual(self.db.state(now=200).get("emotions", []), [])
        self.assertEqual(self.db.status()["consumed"], 0)

    def test_disabled_feature_rejects_new_schema_instead_of_silently_learning(self):
        with self.assertRaisesRegex(ValueError, "invalid_proposal"):
            self.submit([record()], enabled=False)
        self.assertEqual(self.db.state(now=101).get("emotions", []), [])

    def test_extra_resolved_source_and_changed_evidence_never_commit(self):
        original, extra = record(), record("extra", "这是一项不同的真实阅读体验")
        jid = self.db.enqueue([original], now=100)
        job = self.db.claim(now=100)
        with self.assertRaisesRegex(ValueError, "invalid_emotion_evidence"):
            self.db.complete(jid, job["token"], proposal([extra]), [original, extra], now=101, emotional_episodes=True)
        with self.assertRaisesRegex(ValueError, "source_changed"):
            self.db.complete(jid, job["token"], proposal([original]), [{**original, "text": "已经被修改的原始材料"}], now=101, emotional_episodes=True)
        self.assertEqual(self.db.state(now=102).get("emotions", []), [])

    def test_revision_conflict_then_alias_retry_cannot_apply_twice(self):
        original, alias = record(), record("alias")
        first = self.db.enqueue([original], now=100)
        second = self.db.enqueue([alias], now=100)
        jobs = {j["id"]: j for j in (self.db.claim(now=100), self.db.claim(now=100))}
        self.db.complete(first, jobs[first]["token"], proposal([original]), [original], now=101,
                         expected_revision=0, emotional_episodes=True)
        with self.assertRaisesRegex(ValueError, "revision_conflict"):
            self.db.complete(second, jobs[second]["token"], proposal([alias]), [alias], now=101,
                             expected_revision=0, emotional_episodes=True)
        result = self.db.complete(second, jobs[second]["token"], proposal([alias]), [alias], now=102,
                                 expected_revision=1, emotional_episodes=True)
        self.assertFalse(result["changed"])
        self.assertEqual(len(self.db.state(now=102)["emotions"]), 2)

    def test_retiring_episode_preserves_receipts_and_legacy_state_works(self):
        self.assertNotIn("emotions", self.db.state(now=100))
        self.submit([record()])
        item = self.db.state(now=101)["emotions"][0]
        after = self.db.retire(item["key"], 1, now=102)
        self.assertEqual(len(after["emotions"]), 1)
        self.assertFalse(self.submit([record("alias")], now=110)["changed"])
        self.assertEqual(len(self.db.state(now=111)["emotions"]), 1)


class EmotionStoreTests(EmotionChecks, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"growth.sqlite3"
        self.db = self.open_store()

    def open_store(self):
        return GrowthStore(self.path)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()
