from __future__ import annotations

import concurrent.futures
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path

from MaiLivingWorld.growth_store import source_fingerprint
from MaiLivingWorld.recollection_store import RecollectionStore

OWNER, OTHER = "12345678", "87654321"


def message(mid="m1", body="我现在住在北京", *, owner=OWNER, stream="private-stream", group=False):
    info = {"user_info": {"user_id": owner}}
    if group:
        info["group_info"] = {"group_id": "77777777"}
    return {"platform": "qq", "account_id": "99999999", "session_id": stream, "message_id": mid,
            "message_info": info, "processed_plain_text": body}


def target(**changes):
    return {"id": "brain:fact:F1", "source": "brain_fact", "kind": "fact", "scope": "private:"+OWNER,
            "subject_id": OWNER, "text": "我住在上海", "timestamp": 10,
            "sensitivity": "personal", "evidence_status": "active", **changes}


def proposal(record=None, *, quote="我现在住在北京", old_quote="我住在上海", relation="replace"):
    record = target() if record is None else record
    return {"updates": [{"target_id": record["id"], "target_digest": source_fingerprint(record),
                         "quote": quote, "old_quote": old_quote, "relation": relation}]}


class FragmentCorrectionChecks:
    """The same fragment lifecycle checks run against SQLite and live MySQL."""

    def _fragment_store(self):
        return self.db

    def _correct_fragment(self, raw, old_quote, quote, now):
        db = self._fragment_store()
        db.enqueue(message("fragment-"+str(now), quote), owner_id=OWNER, now=now)
        job = db.claim(OWNER, now=now+1)
        result = db.complete(job["id"], job["token"], proposal(raw, old_quote=old_quote, quote=quote),
                             owner_id=OWNER, expected_revision=job["base_revision"],
                             targets={raw["id"]: raw}, now=now+2)
        return next(row for row in db.active(OWNER) if row["id"] == result["ids"][0])

    def test_independent_fragments_survive_replacement_and_individual_retraction(self):
        db = self._fragment_store()
        raw = target(text="面试在周三。居住地是杭州。")
        first = self._correct_fragment(raw, "面试在周三。", "面试改到周五。", 100)
        second = self._correct_fragment(raw, "居住地是杭州。", "现在居住地是苏州。", 200)
        self.assertEqual({row["id"] for row in db.active(OWNER)}, {first["id"], second["id"]})
        replacement = self._correct_fragment(raw, "面试在周三。", "面试又改到周六。", 300)
        self.assertEqual({row["id"] for row in db.active(OWNER)}, {replacement["id"], second["id"]})
        command = message("retract-first", "/记忆 撤回更正")
        db.retract(command, owner_id=OWNER, revision_id=replacement["id"], expected_revision=1, now=400)
        self.assertEqual(db.active(OWNER), [second])
        db.retract(command, owner_id=OWNER, revision_id=replacement["id"], expected_revision=1, now=450)
        self.assertEqual(db.active(OWNER), [second])
        db.retract(message("retract-second", "/记忆 撤回更正"), owner_id=OWNER,
                   revision_id=second["id"], expected_revision=1, now=500)
        self.assertEqual(db.active(OWNER), [])
        statuses = {row["id"]: row["status"] for row in db._read("SELECT id,status FROM recollection_revisions")}
        self.assertEqual(statuses[first["id"]], "superseded")
        self.assertEqual(statuses[replacement["id"]], "retracted")
        self.assertEqual(statuses[second["id"]], "retracted")

    def test_overlapping_different_fragment_is_rejected_atomically(self):
        db = self._fragment_store()
        raw = target(text="面试在周三。居住地是杭州。")
        first = self._correct_fragment(raw, "面试在周三。", "面试改到周五。", 100)
        db.enqueue(message("overlap", "现在居住地是苏州。"), owner_id=OWNER, now=200)
        job = db.claim(OWNER, now=201)
        before = db.state(OWNER)
        for old_quote in ("周三。", "面试在周三。居住地", raw["text"]):
            with self.subTest(old_quote=old_quote), self.assertRaisesRegex(ValueError, "overlapping_quote"):
                db.complete(job["id"], job["token"], proposal(raw, old_quote=old_quote, quote="现在居住地是苏州。"),
                            owner_id=OWNER, expected_revision=job["base_revision"], targets={raw["id"]: raw}, now=202)
        self.assertEqual(db.active(OWNER), [first])
        self.assertEqual(db.state(OWNER), before)
        # The original lease remains usable after every rejected overlap.
        db.complete(job["id"], job["token"], proposal(raw, old_quote="居住地是杭州。", quote="现在居住地是苏州。"),
                    owner_id=OWNER, expected_revision=job["base_revision"], targets={raw["id"]: raw}, now=203)
        self.assertEqual(len(db.active(OWNER)), 2)

    def test_four_adjacent_fragments_limit_allows_same_fragment_replacement(self):
        db = self._fragment_store()
        parts = ["面试在周三。", "居住地是杭州。", "喜欢茉莉。", "晚饭吃米饭。", "周末去公园。"]
        raw = target(text="".join(parts))
        first = None
        for index, old_quote in enumerate(parts[:4]):
            row = self._correct_fragment(raw, old_quote, "这是第"+str(index)+"项更正。", 100+index*10)
            first = row if first is None else first
        db.enqueue(message("fifth", "周末去海边，面试改到周一。"), owner_id=OWNER, now=200)
        job = db.claim(OWNER, now=201)
        with self.assertRaisesRegex(ValueError, "fragment_capacity"):
            db.complete(job["id"], job["token"], proposal(raw, old_quote=parts[4], quote="周末去海边"),
                        owner_id=OWNER, expected_revision=job["base_revision"], targets={raw["id"]: raw}, now=202)
        self.assertEqual(len(db.active(OWNER)), 4)
        db.complete(job["id"], job["token"], proposal(raw, old_quote=parts[0], quote="面试改到周一。"),
                    owner_id=OWNER, expected_revision=job["base_revision"], targets={raw["id"]: raw}, now=203)
        self.assertEqual(len(db.active(OWNER)), 4)
        self.assertNotIn(first["id"], {row["id"] for row in db.active(OWNER)})

    def test_full_global_capacity_counts_new_fragment_but_not_replacement(self):
        db = self._fragment_store()
        raw = target(text="面试在周三。居住地是杭州。")
        first = self._correct_fragment(raw, "面试在周三。", "面试改到周五。", 100)
        with db.con:
            for index in range(127):
                db.con.execute("INSERT INTO recollection_revisions(id,owner_id,target_id,target_digest,input_id,relation,status,revision,payload,created,updated) "
                    "SELECT ?,owner_id,?,target_digest,input_id,relation,status,revision,payload,created,updated FROM recollection_revisions WHERE id=?",
                    ("fragment-fixture:"+str(index), "brain:fact:fixture"+str(index), first["id"]))
        db.enqueue(message("full", "现在住在苏州，面试改到周一。"), owner_id=OWNER, now=200)
        job = db.claim(OWNER, now=201)
        with self.assertRaisesRegex(ValueError, "revision_capacity"):
            db.complete(job["id"], job["token"], proposal(raw, old_quote="居住地是杭州。", quote="现在住在苏州"),
                        owner_id=OWNER, expected_revision=job["base_revision"], targets={raw["id"]: raw}, now=202)
        self.assertEqual(len(db.active(OWNER)), 128)
        db.complete(job["id"], job["token"], proposal(raw, old_quote="面试在周三。", quote="面试改到周一。"),
                    owner_id=OWNER, expected_revision=job["base_revision"], targets={raw["id"]: raw}, now=203)
        self.assertEqual(len(db.active(OWNER)), 128)

    def test_new_raw_version_supersedes_all_old_version_fragments(self):
        db = self._fragment_store()
        raw = target(text="面试在周三。居住地是杭州。")
        first = self._correct_fragment(raw, "面试在周三。", "面试改到周五。", 100)
        second = self._correct_fragment(raw, "居住地是杭州。", "现在居住地是苏州。", 200)
        changed = {**raw, "timestamp": 250}
        latest = self._correct_fragment(changed, "面试在周三。", "面试改到周一。", 300)
        self.assertEqual(db.active(OWNER), [latest])
        self.assertEqual(latest["target_digest"], source_fingerprint(changed))
        statuses = {row["id"]: row["status"] for row in db._read("SELECT id,status FROM recollection_revisions")}
        self.assertEqual(statuses[first["id"]], "superseded")
        self.assertEqual(statuses[second["id"]], "superseded")


class RecollectionStoreTests(FragmentCorrectionChecks, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"recollection.sqlite3"
        self.db = RecollectionStore(self.path)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def reopen(self):
        self.db.close()
        self.db = RecollectionStore(self.path)

    def enqueue(self, mid="m1", body="我现在住在北京", now=100):
        return self.db.enqueue(message(mid, body), owner_id=OWNER, now=now)

    def complete(self, job, value=None, *, records=None, now=102):
        return self.db.complete(job["id"], job["token"], proposal() if value is None else value,
                                owner_id=OWNER, expected_revision=job["base_revision"],
                                targets={target()["id"]: target()} if records is None else records, now=now)

    def corrected(self, mid="m1", quote="我现在住在北京", now=100):
        self.enqueue(mid, quote, now)
        self.complete(self.db.claim(OWNER, now=now+1), proposal(quote=quote), now=now+2)
        return self.db.active(OWNER)[0]

    def delivery(self, receipt="send1", now=100, **changes):
        values = dict(owner_id=OWNER, scope="private:"+OWNER, receipt_id=receipt,
                      topic_refs=[{"id": target()["id"], "digest": source_fingerprint(target())}],
                      text_digest="a"*64, now=now)
        values.update(changes)
        return self.db.record_delivery(**values)

    def test_empty_constructor_and_reads_preserve_migration_empty_database(self):
        self.assertEqual(self.db.status(OWNER, now=100)["revision"], 0)
        self.assertEqual(self.db.active(OWNER), [])
        self.assertEqual(self.db.recent_deliveries(OWNER, now=100), [])
        for name in ("control", "inputs", "revisions", "deliveries"):
            self.assertEqual(self.db.con.execute("SELECT COUNT(*) FROM recollection_"+name).fetchone()[0], 0)

    def test_only_trusted_owner_private_plain_text_can_be_enqueued(self):
        invalid = [message(owner=OTHER), message(group=True), message(mid=""), message(stream=""),
                   {**message(), "account_id": OWNER}, {**message(), "platform": "other"},
                   {**message(), "raw_message": [{"type": "forward", "data": "纠错"}]},
                   {**message(), "is_picture": True}, {**message(), "is_command": True},
                   message(body="/纠正 记忆"), message(body="长"*601), message(body="好")]
        for msg in invalid:
            with self.subTest(msg=msg), self.assertRaises(ValueError):
                self.db.enqueue(msg, owner_id=OWNER, now=100)
        self.assertEqual(self.db.status(OWNER)["inputs"], {})

    def test_message_dedup_and_changed_body_survive_restart(self):
        ident = self.enqueue()
        self.reopen()
        self.assertEqual(self.enqueue(now=200), ident)
        with self.assertRaisesRegex(ValueError, "idempotency_conflict"):
            self.enqueue(body="我现在住在南京")
        another = self.db.enqueue(message(stream="another-stream"), owner_id=OWNER, now=101)
        self.assertNotEqual(ident, another)
        self.assertEqual(self.db.status(OWNER, now=102)["queued_inputs"], 2)

    def test_queue_capacity_is_visible_and_expired_originals_are_cleared(self):
        for index in range(32):
            self.db.enqueue(message(str(index)), owner_id=OWNER, now=100, ttl_seconds=60)
        self.assertIsNone(self.enqueue("full", now=101))
        self.assertTrue(self.db.status(OWNER, now=101)["queue_full"])
        self.assertIsNone(self.db.claim(OWNER, now=160))
        self.assertFalse(self.db.status(OWNER, now=160)["queue_full"])
        self.assertTrue(all(json.loads(row[0])["text"] == "" for row in self.db.con.execute("SELECT payload FROM recollection_inputs")))

    def test_claim_is_single_input_exclusive_and_old_lease_cannot_commit(self):
        self.enqueue()
        self.enqueue("m2", now=101)
        first = self.db.claim(OWNER, now=102, lease_seconds=2)
        self.assertEqual(first["text"], "我现在住在北京")
        self.assertIsNone(self.db.claim(OWNER, now=103))
        self.reopen()
        second = self.db.claim(OWNER, now=104)
        self.assertNotEqual(first["token"], second["token"])
        with self.assertRaisesRegex(ValueError, "lease_conflict"):
            self.complete(first, now=105)
        self.complete(second, now=105)

    def test_budget_defer_does_not_count_as_one_of_three_model_failures(self):
        self.enqueue()
        job = self.db.claim(OWNER, now=101)
        self.db.defer(job["id"], job["token"], owner_id=OWNER, reason="budget", retry_at=150, now=102)
        self.assertIsNone(self.db.claim(OWNER, now=149))
        for now in (150, 300, 600):
            job = self.db.claim(OWNER, now=now)
            self.db.fail(job["id"], job["token"], owner_id=OWNER, reason="invalid_model", now=now)
        self.assertIsNone(self.db.claim(OWNER, now=2000))
        self.assertEqual(self.db.status(OWNER)["inputs"], {"failed": 1})

    def test_cycle_budget_persists_without_changing_revision(self):
        args = dict(day_start=0, daily_limit=2, interval_seconds=10)
        self.assertTrue(self.db.reserve_cycle(OWNER, now=100, **args))
        self.assertFalse(self.db.reserve_cycle(OWNER, now=105, **args))
        self.assertTrue(self.db.reserve_cycle(OWNER, now=110, **args))
        self.reopen()
        self.assertFalse(self.db.reserve_cycle(OWNER, now=120, **args))
        self.assertEqual(self.db.state(OWNER)["revision"], 0)
        self.assertTrue(self.db.reserve_cycle(OWNER, day_start=86400, daily_limit=2, interval_seconds=10, now=86410))

    def test_complete_pins_original_version_and_does_not_mutate_source(self):
        raw = target()
        snapshot = copy.deepcopy(raw)
        self.enqueue()
        result = self.complete(self.db.claim(OWNER, now=101), records={raw["id"]: raw})
        self.assertEqual(result["applied"], 1)
        row = self.db.active(OWNER)[0]
        self.assertTrue(row["id"].startswith("recollection:revision:"))
        self.assertEqual(row["target_digest"], source_fingerprint(snapshot))
        self.assertEqual((row["quote"], row["old_quote"], row["evidence_status"]), ("我现在住在北京", "我住在上海", "owner_reported"))
        self.assertEqual(raw, snapshot)
        self.assertEqual(json.loads(self.db.con.execute("SELECT payload FROM recollection_inputs WHERE kind='input'").fetchone()[0])["text"], "")
        self.reopen()
        self.assertEqual(self.db.active(OWNER), [row])
        self.enqueue(now=200)
        self.assertIsNone(self.db.claim(OWNER, now=201))

    def test_empty_proposal_consumes_and_clears_input_without_creating_revision(self):
        self.enqueue()
        result = self.complete(self.db.claim(OWNER, now=101), {"updates": []})
        self.assertEqual((result["applied"], result["revision"]), (0, 0))
        self.assertEqual(self.db.active(OWNER), [])
        self.assertEqual(json.loads(self.db.con.execute("SELECT payload FROM recollection_inputs").fetchone()[0])["text"], "")

    def test_forged_changed_or_non_contiguous_quotes_are_rejected(self):
        self.enqueue()
        job = self.db.claim(OWNER, now=101)
        for value in (proposal(old_quote="住在北京"), proposal(quote="我住在北京"),
                      proposal(relation="invent"), {"updates": [{"target_id": "fake"}]}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.complete(job, value)
        changed = target(text="我现在搬到了苏州")
        with self.assertRaisesRegex(ValueError, "target_changed"):
            self.complete(job, records={changed["id"]: changed})
        self.assertEqual(self.db.active(OWNER), [])

    def test_derived_tool_other_owner_and_non_private_targets_are_rejected(self):
        self.enqueue()
        job = self.db.claim(OWNER, now=101)
        candidates = [target(scope="group:77777777"), target(subject_id=OTHER), target(derived=True),
                      target(source="tool", id="tool:observation:"+"a"*64),
                      target(source="mind", id="mind:note:"+OTHER+":note"), target(evidence_status="owner_reported"),
                      target(timestamp=100), target(timestamp=200), target(id="bond:moment:"+"a"*64)]
        for raw in candidates:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.complete(job, proposal(raw), records={raw["id"]: raw})
        self.assertEqual(self.db.active(OWNER), [])

    def test_repeated_old_quote_cannot_identify_the_exact_fact_to_replace(self):
        raw = target(text="我的面试在周三；朋友的面试也在周三")
        self.enqueue(body="纠正一下，我的面试改到周五了")
        job = self.db.claim(OWNER, now=101)
        with self.assertRaisesRegex(ValueError, "ambiguous_old_quote"):
            self.complete(job, proposal(raw, quote="纠正一下，我的面试改到周五了", old_quote="周三"),
                          records={raw["id"]: raw})
        self.assertEqual(self.db.active(OWNER), [])
        overlap = target(text="哈哈哈")
        with self.assertRaisesRegex(ValueError, "ambiguous_old_quote"):
            self.complete(job, proposal(overlap, quote="纠正一下，我的面试改到周五了", old_quote="哈哈"),
                          records={overlap["id"]: overlap})

    def test_mind_bond_and_continuity_require_correct_original_identity(self):
        for index, (source, ident, status) in enumerate([
            ("mind", "mind:note:"+OWNER+":N1", "active"),
            ("bond", "bond:moment:"+"a"*64, "owner_reported"),
            ("continuity", "continuity:thread:"+"b"*64, "owner_reported")]):
            raw = target(source=source, id=ident, evidence_status=status)
            self.enqueue(str(index), now=100+index*10)
            job = self.db.claim(OWNER, now=101+index*10)
            self.complete(job, proposal(raw), records={ident: raw}, now=102+index*10)
        self.assertEqual(len(self.db.active(OWNER)), 3)

    def test_full_input_and_original_source_privacy_are_conservatively_combined(self):
        raw = target(sensitivity="unknown")
        self.enqueue(body="这件事请保密，我现在住在北京")
        self.complete(self.db.claim(OWNER, now=101), proposal(raw), records={raw["id"]: raw})
        self.assertEqual(self.db.active(OWNER)[0]["sensitivity"], "confidential")

    def test_second_correction_supersedes_old_entry_and_latest_retraction_restores_original(self):
        first = self.corrected()
        second = self.corrected("m2", "我现在住在南京", now=200)
        self.assertEqual(self.db.active(OWNER), [second])
        self.assertEqual(second["target_digest"], first["target_digest"])
        old = self.db.con.execute("SELECT status FROM recollection_revisions WHERE id=?", (first["id"],)).fetchone()
        self.assertEqual(old[0], "superseded")
        retired = self.db.retract(message("retract", "/记忆 撤回更正"), owner_id=OWNER, revision_id=second["id"], expected_revision=1, now=300)
        self.assertEqual(retired["status"], "retracted")
        self.assertEqual(self.db.active(OWNER), [])
        self.reopen()
        self.assertEqual(self.db.active(OWNER), [])
        self.enqueue("m1", now=400)
        self.assertIsNone(self.db.claim(OWNER, now=401))

    def test_replayed_retract_does_not_remove_a_newer_explicit_correction(self):
        first = self.corrected()
        command = message("retract", "/记忆 撤回更正")
        self.db.retract(command, owner_id=OWNER, revision_id=first["id"], expected_revision=1, now=200)
        second = self.corrected("later", "我现在住在南京", now=300)
        replay = self.db.retract(command, owner_id=OWNER, revision_id=first["id"], expected_revision=1, now=400)
        self.assertEqual(replay["status"], "retracted")
        self.assertEqual(self.db.active(OWNER), [second])

    def test_old_delayed_input_cannot_override_newer_correction(self):
        self.enqueue("old", "我现在住在苏州", now=100)
        old = self.db.claim(OWNER, now=101)
        self.db.defer(old["id"], old["token"], owner_id=OWNER, reason="budget", retry_at=300, now=102)
        newer = self.corrected("new", "我现在住在南京", now=200)
        job = self.db.claim(OWNER, now=300)
        result = self.complete(job, proposal(quote="我现在住在苏州"), now=301)
        self.assertEqual(result["ignored"], 1)
        self.assertEqual(self.db.active(OWNER), [newer])

    def test_pre_retraction_queued_input_cannot_revive_a_withdrawn_correction(self):
        first = self.corrected()
        self.enqueue("queued-before", "我现在住在南京", now=200)
        stale = self.db.claim(OWNER, now=201)
        self.db.retract(message("retract", "/记忆 撤回更正"), owner_id=OWNER, revision_id=first["id"], expected_revision=1, now=202)
        with self.assertRaisesRegex(ValueError, "lease_conflict"):
            self.complete(stale, proposal(quote="我现在住在南京"), now=203)
        result = self.complete(self.db.claim(OWNER, now=204), proposal(quote="我现在住在南京"), now=205)
        self.assertEqual(result["ignored"], 1)
        self.assertEqual(self.db.active(OWNER), [])

    def test_raw_target_new_version_never_inherits_old_digest(self):
        first = self.corrected()
        raw = target(text="我曾住在上海", timestamp=150)
        self.enqueue("new-version", "我现在住在南京", now=200)
        self.complete(self.db.claim(OWNER, now=201), proposal(raw, quote="我现在住在南京", old_quote="我曾住在上海"),
                      records={raw["id"]: raw}, now=202)
        row = self.db.active(OWNER)[0]
        self.assertEqual(row["target_digest"], source_fingerprint(raw))
        self.assertNotEqual(row["target_digest"], first["target_digest"])

    def test_input_hash_and_control_revision_are_checked_inside_commit(self):
        self.enqueue()
        job = self.db.claim(OWNER, now=101)
        with self.db.con:
            self.db.con.execute("UPDATE recollection_control SET revision=revision+1 WHERE owner_id=?", (OWNER,))
        with self.assertRaisesRegex(ValueError, "revision_conflict"):
            self.complete(job)
        with self.db.con:
            self.db.con.execute("UPDATE recollection_inputs SET payload=? WHERE id=?", (json.dumps({"text": "我现在住在别处"}), job["id"]))
        with self.assertRaisesRegex(ValueError, "input_changed"):
            self.complete(job)
        self.assertEqual(self.db.active(OWNER), [])

    def test_second_invalid_update_rolls_back_first_and_leaves_input_retryable(self):
        self.enqueue()
        job = self.db.claim(OWNER, now=101)
        raw2 = target(id="brain:fact:F2", text="其他原始内容")
        value = proposal()
        value["updates"].extend(proposal(raw2, old_quote="并不存在")["updates"])
        with self.assertRaisesRegex(ValueError, "unverified_quote"):
            self.complete(job, value, records={target()["id"]: target(), raw2["id"]: raw2})
        self.assertEqual(self.db.active(OWNER), [])
        self.assertEqual(self.db.state(OWNER)["revision"], 0)
        self.complete(job)

    def test_owner_and_row_revision_guard_retraction(self):
        row = self.corrected()
        self.assertEqual(self.db.active(OTHER), [])
        with self.assertRaises(ValueError):
            self.db.retract(message("bad", owner=OTHER), owner_id=OWNER, revision_id=row["id"], expected_revision=1)
        with self.assertRaisesRegex(ValueError, "revision_conflict"):
            self.db.retract(message("bad"), owner_id=OWNER, revision_id=row["id"], expected_revision=2)
        self.assertEqual(self.db.active(OWNER), [row])

    def test_delivery_is_idempotent_and_stores_only_hashes_and_refs(self):
        self.assertTrue(self.delivery("host-original-receipt"))
        self.reopen()
        self.assertFalse(self.delivery("host-original-receipt", now=200))
        row = self.db.recent_deliveries(OWNER, now=201)[0]
        self.assertEqual(row["created"], 100)
        self.assertEqual(row["topic_refs"][0]["digest"], source_fingerprint(target()))
        self.assertNotIn("host-original-receipt", json.dumps(row))
        self.assertNotIn("text", row)
        with self.assertRaisesRegex(ValueError, "idempotency_conflict"):
            self.delivery("host-original-receipt", text_digest="b"*64, now=202)
        self.assertEqual(self.db.state(OWNER)["revision"], 0)

    def test_delivery_old_receipt_cannot_reappear_after_bounded_detail_retention(self):
        for index in range(260):
            self.delivery("receipt"+str(index), now=100+index)
        self.assertEqual(len(self.db.recent_deliveries(OWNER, now=400)), 256)
        self.assertEqual(self.db.status(OWNER, now=400)["deliveries"], 256)
        self.reopen()
        self.assertFalse(self.delivery("receipt0", now=500))
        self.assertEqual(len(self.db.recent_deliveries(OWNER, now=500)), 256)
        self.assertIsNone(self.db.claim(OWNER, now=500))

    def test_delivery_scope_window_and_topic_versions_are_checked(self):
        self.delivery(now=100)
        self.delivery("future", now=300)
        self.assertEqual(len(self.db.recent_deliveries(OWNER, now=150, within_seconds=50)), 1)
        self.assertEqual(self.db.recent_deliveries(OWNER, now=151, within_seconds=50), [])
        self.assertEqual(self.db.recent_deliveries(OTHER, now=150), [])
        with self.assertRaises(ValueError):
            self.delivery("wrong-scope", scope="private:"+OTHER)
        with self.assertRaises(ValueError):
            self.delivery("mixed", topic_refs=[{"id": "x", "digest": "a"*64}, {"id": "x", "digest": "b"*64}])

    def test_delivery_insert_failure_rolls_back_permanent_receipt(self):
        execute = self.db.con.execute
        def fail(sql, parameters=()):
            if sql.startswith("INSERT INTO recollection_deliveries"):
                raise RuntimeError("synthetic_failure")
            return execute(sql, parameters)
        self.db.con.execute = fail
        try:
            with self.assertRaisesRegex(RuntimeError, "synthetic_failure"):
                self.delivery()
        finally:
            self.db.con.execute = execute
        self.assertTrue(self.delivery())

    def test_revision_capacity_rejects_new_target_but_allows_replacement(self):
        first = self.corrected()
        with self.db.con:
            for index in range(127):
                self.db.con.execute("INSERT INTO recollection_revisions(id,owner_id,target_id,target_digest,input_id,relation,status,revision,payload,created,updated) "
                    "SELECT ?,owner_id,?,target_digest,input_id,relation,status,revision,payload,created,updated FROM recollection_revisions WHERE id=?",
                    ("fixture:"+str(index), "brain:fact:fixture"+str(index), first["id"]))
        self.assertEqual(len(self.db.active(OWNER)), 128)
        raw = target(id="brain:fact:brand-new")
        self.enqueue("new-target", now=200)
        job = self.db.claim(OWNER, now=201)
        with self.assertRaisesRegex(ValueError, "revision_capacity"):
            self.complete(job, proposal(raw), records={raw["id"]: raw}, now=202)
        self.complete(job, proposal(), now=203)
        self.assertEqual(len(self.db.active(OWNER)), 128)

    def test_concurrent_same_message_receipt_is_unique(self):
        barrier = threading.Barrier(3)
        def worker(index):
            db = RecollectionStore(self.path)
            try:
                barrier.wait(timeout=10)
                return db.enqueue(message(), owner_id=OWNER, now=100)
            finally:
                db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            ids = list(pool.map(worker, range(3)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(self.db.status(OWNER, now=101)["queued_inputs"], 1)

    def test_concurrent_first_and_existing_owner_budget_has_one_winner(self):
        barrier = threading.Barrier(3)
        def worker(index):
            db = RecollectionStore(self.path)
            try:
                result = []
                for day in (0, 86400):
                    barrier.wait(timeout=10)
                    result.append(db.reserve_cycle(OWNER, day_start=day, daily_limit=1, interval_seconds=0, now=day+100))
                return result
            finally:
                db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(worker, range(3)))
        self.assertEqual([sum(row[i] for row in results) for i in range(2)], [1, 1])


if __name__ == "__main__":
    unittest.main()
