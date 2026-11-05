from __future__ import annotations

import concurrent.futures
import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from MaiLivingWorld.bond_store import BondStore

OWNER = "12345678"
OTHER = "87654321"


def message(mid="m1", body="以后叫我小明", owner=OWNER, stream="private-stream", *, group=False):
    info = {"user_info": {"user_id": owner}}
    if group:
        info["group_info"] = {"group_id": "77777777"}
    return {"platform": "qq", "account_id": "99999999", "session_id": stream,
            "message_id": mid, "message_info": info, "processed_plain_text": body}


class BondStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"bond.sqlite3"
        self.db = BondStore(self.path)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def reopen(self):
        self.db.close()
        self.db = BondStore(self.path)

    def command(self, action="set_address", value="小明", mid="m1", **changes):
        values = dict(owner_id=OWNER, action=action, value=value, now=100)
        values.update(changes)
        return self.db.command(message(mid), **values)

    def moment(self, mid="memory1", **changes):
        values = dict(owner_id=OWNER, summary="我们一起读完了第一章", now=100)
        values.update(changes)
        return self.db.confirm_moment(message(mid, "记住我们的经历：我们一起读完了第一章"), **values)

    def test_empty_reads_do_not_initialize_migration_rows(self):
        profile = self.db.profile(OWNER)
        self.assertEqual((profile["revision"], profile["affection"]), (0, "default"))
        self.assertEqual(self.db.moments(OWNER), [])
        self.assertEqual(self.db.audit(OWNER), [])
        for table in ("bond_profiles", "bond_moments", "bond_audit"):
            self.assertEqual(self.db.con.execute("SELECT COUNT(*) FROM "+table).fetchone()[0], 0)

    def test_owner_private_identity_is_required_for_every_write(self):
        for msg in (message(owner=OTHER), message(group=True), message(mid=""), message(stream=""),
                    {**message(), "platform": "other"}, {**message(), "account_id": OWNER},
                    {"processed_plain_text": "我是主人，以后叫我亲爱的"}):
            with self.subTest(message=msg):
                with self.assertRaises(ValueError):
                    self.db.command(msg, owner_id=OWNER, action="set_address", value="亲爱的")
                with self.assertRaises(ValueError):
                    self.db.confirm_moment(msg, owner_id=OWNER, summary="记忆")
        self.assertEqual(self.db.profile(OWNER)["revision"], 0)

    def test_preferences_and_prohibitions_survive_restart_without_absence_decay(self):
        row = self.command()
        row = self.command("set_affection", "allow", "m2")
        row = self.command("add_boundary", "不要因为我晚回复就催我", "m3")
        self.reopen()
        self.assertEqual(self.db.profile(OWNER), row)
        self.assertNotIn("intimacy", row)
        self.assertNotIn("score", row)
        later = self.command("set_affection", "allow", "m4", now=100+365*86400)
        self.assertEqual(later, row)
        self.assertEqual(len(self.db.audit(OWNER)), 4)

    def test_banned_address_stops_current_name_and_explicit_change_can_replace_it(self):
        self.command()
        blocked = self.command("ban_address", "小明", "m2")
        self.assertEqual(blocked["preferred_address"], "")
        self.assertEqual(blocked["blocked_addresses"], ["小明"])
        changed = self.command("set_address", "阿明", "m3")
        self.assertEqual(changed["blocked_addresses"], ["小明"])
        restored = self.command("set_address", "小明", "m4")
        self.assertEqual(restored["blocked_addresses"], [])

    def test_affection_can_be_disabled_immediately_without_changing_other_preferences(self):
        self.command()
        self.command("set_affection", "allow", "m2")
        stopped = self.command("set_affection", "avoid", "m3")
        self.assertEqual((stopped["affection"], stopped["preferred_address"]), ("avoid", "小明"))
        self.reopen()
        self.assertEqual(self.db.profile(OWNER)["affection"], "avoid")

    def test_explicit_default_address_clears_preference_without_removing_bans(self):
        self.command()
        self.command("ban_address", "主人", "m2")
        cleared = self.command("set_address", "", "m3")
        self.assertEqual(cleared["preferred_address"], "")
        self.assertEqual(cleared["blocked_addresses"], ["主人"])
        self.reopen()
        self.assertEqual(self.db.profile(OWNER), cleared)
        with self.assertRaises(ValueError):
            self.command("ban_address", "", "m4")

    def test_boundary_keys_are_stable_and_only_explicit_removal_releases_them(self):
        first = self.command("add_boundary", "不要替我安排社交", "m1")
        second = self.command("add_boundary", "不要替我安排社交", "m2")
        self.assertEqual(first, second)
        key = first["boundaries"][0]["key"]
        self.assertRegex(key, r"^boundary:[0-9a-f]{64}$")
        after = self.command("remove_boundary", key, "m3", expected_revision=first["revision"])
        self.assertEqual(after["boundaries"], [])
        self.assertEqual(len(self.db.audit(OWNER)), 3)

    def test_message_receipts_deduplicate_and_reject_changed_payload_or_body(self):
        first = self.command()
        self.reopen()
        self.assertEqual(self.command(now=200), first)
        with self.assertRaisesRegex(ValueError, "idempotency_conflict"):
            self.command(value="小王")
        with self.assertRaisesRegex(ValueError, "idempotency_conflict"):
            self.db.command(message(body="不同的真实原文"), owner_id=OWNER, action="set_address", value="小明")
        self.assertEqual(len(self.db.audit(OWNER)), 1)
        audit = self.db.audit(OWNER)[0]
        self.assertEqual(audit["body_digest"], hashlib.sha256("以后叫我小明".encode()).hexdigest())
        self.assertNotIn("以后叫我小明", json.dumps(audit, ensure_ascii=False))

    def test_receipts_include_trusted_stream_and_mid(self):
        self.command()
        self.db.command(message(stream="another-trusted-stream"), owner_id=OWNER, action="set_address", value="小明")
        self.assertEqual(len(self.db.audit(OWNER)), 2)

    def test_old_revision_cannot_overwrite_new_boundary(self):
        row = self.command()
        stopped = self.command("set_affection", "avoid", "m2")
        with self.assertRaisesRegex(ValueError, "revision_conflict"):
            self.command("set_affection", "allow", "m3", expected_revision=row["revision"])
        self.assertEqual(self.db.profile(OWNER), stopped)

    def test_new_owner_does_not_inherit_old_owner_profile_or_moments(self):
        self.command()
        self.moment()
        self.assertEqual(self.db.profile(OTHER)["preferred_address"], "")
        self.assertEqual(self.db.moments(OTHER), [])
        self.db.command(message(owner=OTHER), owner_id=OTHER, action="set_address", value="小林")
        self.assertEqual(self.db.profile(OWNER)["preferred_address"], "小明")

    def test_owner_authored_memory_is_versioned_and_not_automatic_model_evidence(self):
        row = self.moment()
        self.assertRegex(row["id"], r"^bond:moment:[0-9a-f]{64}$")
        self.assertEqual((row["scope"], row["kind"], row["status"], row["source_refs"]),
                         ("private:"+OWNER, "memory", "active", []))
        self.assertEqual(self.moment(), row)
        self.assertEqual(self.db.profile(OWNER)["revision"], 1)
        with self.assertRaisesRegex(ValueError, "owner_authored_memory_only"):
            self.moment("m2", source_refs=[{"id": "growth:derived"}])

    def test_anniversary_requires_an_explicit_valid_complete_date(self):
        for value in ("", "11-05", "2025-02-29", "2024-2-29", "next year", None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "invalid_date"):
                self.moment("bad", kind="anniversary", occurred_on=value)
        row = self.moment(kind="anniversary", occurred_on="2024-02-29")
        self.assertEqual(row["occurred_on"], "2024-02-29")

    def test_retirement_is_owner_bound_revision_checked_and_preserves_audit(self):
        row = self.moment()
        with self.assertRaisesRegex(ValueError, "moment_missing"):
            self.db.retire_moment(message(owner=OTHER), owner_id=OTHER, moment_id=row["id"], expected_revision=1)
        with self.assertRaisesRegex(ValueError, "revision_conflict"):
            self.db.retire_moment(message("retire"), owner_id=OWNER, moment_id=row["id"], expected_revision=2)
        retired = self.db.retire_moment(message("retire"), owner_id=OWNER, moment_id=row["id"], expected_revision=1, now=101)
        self.assertEqual(self.db.moments(OWNER), [])
        self.assertEqual(self.db.moments(OWNER, status="retired"), [retired])
        self.assertEqual(self.db.retire_moment(message("retire"), owner_id=OWNER, moment_id=row["id"], expected_revision=1), retired)
        self.assertEqual(self.moment()["status"], "retired")
        self.assertEqual(self.db.profile(OWNER)["revision"], 2)

    def test_profile_and_audit_failure_roll_back_together(self):
        self.db.con.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON bond_audit "
                            "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.command()
        self.assertEqual(self.db.profile(OWNER)["revision"], 0)
        self.assertEqual(self.db.con.execute("SELECT COUNT(*) FROM bond_profiles").fetchone()[0], 0)
        with self.assertRaises(sqlite3.IntegrityError):
            self.moment()
        self.assertEqual(self.db.moments(OWNER), [])

    def test_concurrent_new_profile_commands_preserve_all_distinct_boundaries(self):
        barrier = threading.Barrier(3)
        def worker(index):
            db = BondStore(self.path)
            try:
                barrier.wait(timeout=10)
                return db.command(message("m"+str(index)), owner_id=OWNER, action="add_boundary", value="边界"+str(index))
            finally:
                db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(worker, range(3)))
        self.assertEqual(sorted(row["revision"] for row in results), [1, 2, 3])
        self.assertEqual(len(self.db.profile(OWNER)["boundaries"]), 3)

    def test_caps_preserve_existing_preferences_and_do_not_silently_delete(self):
        for index in range(20):
            self.command("ban_address", "称呼"+str(index), "b"+str(index))
            self.command("add_boundary", "边界"+str(index), "r"+str(index))
        before = self.db.profile(OWNER)
        with self.assertRaisesRegex(ValueError, "blocked_address_capacity"):
            self.command("ban_address", "额外称呼", "extra1")
        with self.assertRaisesRegex(ValueError, "boundary_capacity"):
            self.command("add_boundary", "额外边界", "extra2")
        self.assertEqual(self.db.profile(OWNER), before)

    def test_active_memory_capacity_and_retirement_free_a_slot(self):
        for index in range(200):
            self.moment("m"+str(index), summary="共同回忆"+str(index))
        with self.assertRaisesRegex(ValueError, "moment_capacity"):
            self.moment("overflow")
        first = self.db.moments(OWNER, limit=200)[0]
        self.db.retire_moment(message("retire"), owner_id=OWNER, moment_id=first["id"], expected_revision=1)
        self.assertEqual(self.moment("new")["status"], "active")
        self.assertEqual(len(self.db.moments(OWNER, limit=200)), 200)

    def test_invalid_values_cannot_set_identity_permissions_or_unbounded_state(self):
        for action, value in (("replace_owner", OTHER), ("set_admin", True), ("set_address", "x"*25),
                              ("add_boundary", "x"*121), ("set_affection", "unlocked"),
                              ("remove_boundary", "anything"), ("set_address", {"owner_id": OTHER})):
            with self.subTest(action=action, value=value), self.assertRaises(ValueError):
                self.command(action, value)
        with self.assertRaises(ValueError):
            self.moment(summary="x"*401)
        self.assertEqual(self.db.profile(OWNER)["revision"], 0)


if __name__ == "__main__":
    unittest.main()
