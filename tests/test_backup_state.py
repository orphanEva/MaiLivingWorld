import copy
from decimal import Decimal
from datetime import datetime, date, time, timedelta
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("mlw_backup_tested", ROOT/"scripts"/"backup_state.py")
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


def attention_message():
    return {"platform": "qq", "account_id": "99999", "session_id": "owner-backup-stream", "message_id": "attention-backup-input",
            "message_info": {"user_info": {"user_id": "12345678"}, "group_info": None},
            "processed_plain_text": "这段原始聊天正文不会进入注意账本"}


def seed_attention(store):
    store.capture(attention_message(), owner_id="12345678", now=100, engagement_seconds=1)
    claimed = store.claim("12345678", candidates=[{"kind": "reading", "key": "reading:"+"a"*64, "version": "b"*64}],
                          day_start=0, daily_limit=6, interval_seconds=0, now=101)
    if not claimed or not store.finish("12345678", claimed["token"], outcome="progress", now=102):
        raise AssertionError("expected a confirmed attention batch")
    return store.state("12345678", now=103)


def seed_recollection(store):
    from MaiLivingWorld.growth_store import source_fingerprint
    owner = "12345678"
    raw = {"id": "brain:fact:F1", "source": "brain_fact", "kind": "fact", "scope": "private:"+owner,
           "subject_id": owner, "text": "我住在上海", "timestamp": 1,
           "sensitivity": "personal", "evidence_status": "active"}
    digest = source_fingerprint(raw)
    message = dict(attention_message(), message_id="recollection-backup-input", processed_plain_text="我现在住在北京")
    store.enqueue(message, owner_id=owner, now=100)
    batch = store.claim(owner, now=101)
    if not batch:
        raise AssertionError("expected one real recollection claim")
    store.complete(batch["id"], batch["token"], {"updates": [{"target_id": raw["id"],
        "target_digest": digest, "old_quote": "我住在上海", "quote": "我现在住在北京", "relation": "replace"}]},
        owner_id=owner, expected_revision=batch["base_revision"], targets={raw["id"]: raw}, now=102)
    store.record_delivery(owner_id=owner, scope="private:"+owner, receipt_id="recollection-backup-receipt",
        topic_refs=[{"id": raw["id"], "digest": digest}], text_digest="a"*64, now=103)
    return {table: [dict(row) for row in store.con.execute("SELECT * FROM "+table)]
            for table in backup.LEDGERS["recollection.sqlite3"]}


def assert_recollection_restore(case, store, expected):
    for table, rows in expected.items():
        case.assertEqual([dict(row) for row in store.con.execute("SELECT * FROM "+table)], rows)
        case.assertTrue(rows)
    delivery = expected["recollection_deliveries"][0]
    payload = json.loads(delivery["payload"])
    case.assertFalse(store.record_delivery(owner_id="12345678", scope="private:12345678",
        receipt_id="recollection-backup-receipt", topic_refs=payload["topic_refs"], text_digest="a"*64, now=104))
    case.assertEqual(store.con.execute("SELECT COUNT(*) FROM recollection_deliveries").fetchone()[0], 1)
    case.assertIsNone(store.claim("12345678", now=105))


class SQLiteBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/"source"
        self.source.mkdir()
        self.archive = self.root/"backup.zip"
        self.target = self.root/"restore"

    def seed(self, all_ledgers=False):
        for name, tables in backup.LEDGERS.items():
            if not all_ledgers and name != "self_state.sqlite3":
                continue
            con = sqlite3.connect(self.source/name)
            try:
                for table in tables:
                    con.execute('CREATE TABLE "'+table+'" (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL)')
                    con.execute('INSERT INTO "'+table+'"(payload) VALUES (?)', ("持久身份/主人/记忆/计划："+table,))
                con.commit()
            finally:
                con.close()

    def archive_now(self):
        return backup.backup_sqlite(self.source, self.archive, apply=True, writers_stopped=True)

    def restore_now(self):
        return backup.restore_sqlite(self.archive, self.target, apply=True, writers_stopped=True, confirm=backup.CONFIRM)

    def tamper(self, mutation):
        altered = self.root/"altered.zip"
        with zipfile.ZipFile(self.archive) as source:
            members = {info.filename: source.read(info) for info in source.infolist()}
        mutation(members)
        with zipfile.ZipFile(altered, "w") as target:
            for key, value in members.items():
                target.writestr(key, value)
        return altered

    def test_inventory_fourteen_databases_58_tables_and_frozen_legacy_catalogs(self):
        self.assertEqual(len(backup.LEDGERS), 14)
        self.assertEqual(len(backup.TABLES), 58)
        self.assertEqual(len(backup.CATALOGS["v0.14"]), 10)
        self.assertEqual(sum(map(len, backup.CATALOGS["v0.14"].values())), 44)
        self.assertEqual(len(backup.CATALOGS["v0.15"]), 11)
        self.assertEqual(sum(map(len, backup.CATALOGS["v0.15"].values())), 47)
        self.assertEqual(len(backup.CATALOGS["v0.16"]), 12)
        self.assertEqual(sum(map(len, backup.CATALOGS["v0.16"].values())), 51)
        self.assertEqual(len(backup.CATALOGS["v0.17"]), 13)
        self.assertEqual(sum(map(len, backup.CATALOGS["v0.17"].values())), 54)
        with self.assertRaises(TypeError):
            backup.CATALOGS["v0.14"]["bond.sqlite3"] = ("bond_profiles",)
        with self.assertRaises(TypeError):
            backup.CATALOGS["v0.15"]["continuity.sqlite3"] = ("continuity_control",)
        with self.assertRaises(TypeError):
            backup.CATALOGS["v0.16"]["attention.sqlite3"] = ("attention_control",)
        with self.assertRaises(TypeError):
            backup.CATALOGS["v0.17"]["recollection.sqlite3"] = ("recollection_control",)

    def test_full_offline_restore_drill_preserves_every_table(self):
        self.seed(all_ledgers=True)
        self.archive_now()
        self.assertEqual(backup.verify_archive(self.archive)["files"], 14)
        result = self.restore_now()
        self.assertEqual(result["files"], 14)
        for name, tables in backup.LEDGERS.items():
            original, restored = sqlite3.connect(self.source/name), sqlite3.connect(self.target/name)
            try:
                for table in tables:
                    self.assertEqual(original.execute('SELECT * FROM "'+table+'"').fetchall(), restored.execute('SELECT * FROM "'+table+'"').fetchall())
                self.assertEqual(original.execute("SELECT * FROM sqlite_sequence").fetchall(), restored.execute("SELECT * FROM sqlite_sequence").fetchall())
            finally:
                original.close()
                restored.close()

    def test_dry_run_never_creates_archive_or_restore_directory(self):
        self.seed()
        self.assertTrue(backup.backup_sqlite(self.source, self.archive)["dry_run"])
        self.assertFalse(self.archive.exists())
        self.archive_now()
        self.assertTrue(backup.restore_sqlite(self.archive, self.target)["dry_run"])
        self.assertFalse(self.target.exists())

    def test_requires_explicit_stop_and_restore_acknowledgement(self):
        self.seed()
        with self.assertRaisesRegex(backup.BackupError, "stop_writers"):
            backup.backup_sqlite(self.source, self.archive, apply=True)
        self.archive_now()
        with self.assertRaisesRegex(backup.BackupError, "restore_confirmation"):
            backup.restore_sqlite(self.archive, self.target, apply=True, writers_stopped=True)
        self.assertFalse(self.target.exists())

    def test_wal_committed_state_is_in_snapshot(self):
        self.seed()
        con = sqlite3.connect(self.source/"self_state.sqlite3")
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("UPDATE self_identity SET payload='committed WAL owner'")
            con.commit()
            self.archive_now()
            self.restore_now()
            restored = sqlite3.connect(self.target/"self_state.sqlite3")
            try:
                self.assertEqual(restored.execute("SELECT payload FROM self_identity").fetchone()[0], "committed WAL owner")
            finally:
                restored.close()
        finally:
            con.close()

    def test_existing_target_and_archive_never_overwritten(self):
        self.seed()
        self.archive_now()
        existing = self.archive.read_bytes()
        with self.assertRaisesRegex(backup.BackupError, "exists"):
            self.archive_now()
        self.assertEqual(self.archive.read_bytes(), existing)
        self.target.mkdir()
        marker = self.target/"real-user-data.txt"
        marker.write_text("preserve", encoding="utf-8")
        with self.assertRaisesRegex(backup.BackupError, "not_empty"):
            self.restore_now()
        self.assertEqual(marker.read_text(), "preserve")

    def test_restore_existing_empty_directory(self):
        self.seed()
        self.archive_now()
        self.target.mkdir()
        self.restore_now()
        self.assertTrue((self.target/"self_state.sqlite3").is_file())

    def test_corrupt_hash_refused_before_target_created(self):
        self.seed()
        self.archive_now()
        def mutation(members):
            key = "sqlite/self_state.sqlite3"
            members[key] = b"x"+members[key][1:]
        archive = self.tamper(mutation)
        with self.assertRaisesRegex(backup.BackupError, "hash_mismatch"):
            backup.restore_sqlite(archive, self.target, apply=True, writers_stopped=True, confirm=backup.CONFIRM)
        self.assertFalse(self.target.exists())

    def test_archive_traversal_extra_members_and_duplicate_names_rejected(self):
        self.seed()
        self.archive_now()
        for name in ("../escape", "sqlite/../../escape", "C:/escape", "sqlite\\self_state.sqlite3", "/absolute"):
            with self.subTest(name=name):
                archive = self.tamper(lambda members: members.update({name: b"unsafe"}))
                with self.assertRaises(backup.BackupError):
                    backup.verify_archive(archive)
        self.assertFalse((self.root/"escape").exists())
        with zipfile.ZipFile(self.archive, "a") as package:
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                package.writestr("manifest.json", "{}")
        with self.assertRaisesRegex(backup.BackupError, "inventory"):
            backup.verify_archive(self.archive)

    def test_symlink_archive_entry_rejected(self):
        self.seed()
        self.archive_now()
        archive = self.root/"link.zip"
        with zipfile.ZipFile(self.archive) as origin, zipfile.ZipFile(archive, "w") as destination:
            for item in origin.infolist():
                if item.filename.startswith("sqlite/"):
                    item.create_system = 3
                    item.external_attr = (stat.S_IFLNK | 0o777) << 16
                destination.writestr(item, origin.read(item))
        with self.assertRaisesRegex(backup.BackupError, "unsupported_archive_entry"):
            backup.verify_archive(archive)

    def test_manifest_limits_and_missing_inventory_checked(self):
        self.seed()
        self.archive_now()
        for change in (lambda m: m["files"][0].update(size=backup.MAX_FILE+1), lambda m: m.update(missing=[]), lambda m: m["files"][0].update(path=["bad"])):
            def mutate(members):
                manifest = json.loads(members["manifest.json"])
                change(manifest)
                members["manifest.json"] = json.dumps(manifest).encode()
            archive = self.tamper(mutate)
            with self.assertRaises(backup.BackupError):
                backup.verify_archive(archive)

    def test_readonly_source_rejects_unexpected_schema(self):
        self.seed()
        con = sqlite3.connect(self.source/"self_state.sqlite3")
        con.execute("CREATE VIEW secret_view AS SELECT * FROM self_identity")
        con.close()
        with self.assertRaisesRegex(backup.BackupError, "unexpected_sqlite_schema"):
            self.archive_now()

    def test_source_unchanged_and_missing_optional_libs_recorded(self):
        self.seed()
        path = self.source/"self_state.sqlite3"
        before = path.read_bytes()
        result = self.archive_now()
        self.assertEqual(path.read_bytes(), before)
        self.assertIn("companion.sqlite3", result["missing"])
        self.assertEqual(backup.verify_archive(self.archive)["missing"], result["missing"])

    def legacy_archive(self, *, explicit=False, catalog="v0.14"):
        self.seed(all_ledgers=True)
        self.archive_now()
        def mutation(members):
            manifest = json.loads(members["manifest.json"])
            excluded = set(backup.LEDGERS) - set(backup.CATALOGS[catalog])
            manifest["files"] = [item for item in manifest["files"] if item["path"].removeprefix("sqlite/") not in excluded]
            for filename in excluded:
                members.pop("sqlite/"+filename)
            if explicit:
                manifest["catalog"] = catalog
            else:
                manifest["format"] = backup.LEGACY_FORMAT
                manifest.pop("catalog")
            members["manifest.json"] = json.dumps(manifest).encode()
        return self.tamper(mutation)

    def test_legacy_v14_ten_ledger_archive_restores_and_reports_unprovided_bond(self):
        archive = self.legacy_archive()
        result = backup.verify_archive(archive)
        self.assertEqual(result["catalog"], "v0.14")
        self.assertEqual(result["files"], 10)
        self.assertEqual(result["missing"], ["bond.sqlite3", "continuity.sqlite3", "attention.sqlite3", "recollection.sqlite3"])
        preview = backup.restore_sqlite(archive, self.target)
        self.assertEqual(preview["missing"], ["bond.sqlite3", "continuity.sqlite3", "attention.sqlite3", "recollection.sqlite3"])
        self.assertFalse(self.target.exists())
        result = backup.restore_sqlite(archive, self.target, apply=True, writers_stopped=True, confirm=backup.CONFIRM)
        self.assertEqual(result["missing"], ["bond.sqlite3", "continuity.sqlite3", "attention.sqlite3", "recollection.sqlite3"])
        self.assertFalse((self.target/"bond.sqlite3").exists())
        self.assertEqual({path.name for path in self.target.iterdir()}, set(backup.V14_LEDGERS))
        with zipfile.ZipFile(archive) as package:
            self.assertEqual(json.loads(package.read("manifest.json"))["missing"], [])

    def test_explicit_known_v14_catalog_is_supported(self):
        archive = self.legacy_archive(explicit=True)
        self.assertEqual(backup.verify_archive(archive)["missing"], ["bond.sqlite3", "continuity.sqlite3", "attention.sqlite3", "recollection.sqlite3"])

    def test_v15_eleven_ledger_archive_restores_without_inventing_continuity(self):
        archive = self.legacy_archive(explicit=True, catalog="v0.15")
        result = backup.verify_archive(archive)
        self.assertEqual(result["catalog"], "v0.15")
        self.assertEqual(result["files"], 11)
        self.assertEqual(result["missing"], ["continuity.sqlite3", "attention.sqlite3", "recollection.sqlite3"])
        result = backup.restore_sqlite(archive, self.target, apply=True, writers_stopped=True, confirm=backup.CONFIRM)
        self.assertEqual(result["missing"], ["continuity.sqlite3", "attention.sqlite3", "recollection.sqlite3"])
        self.assertEqual({path.name for path in self.target.iterdir()}, set(backup.V15_LEDGERS))

    def test_v15_catalog_cannot_smuggle_continuity_or_hide_missing_file(self):
        archive = self.legacy_archive(explicit=True, catalog="v0.15")
        with zipfile.ZipFile(archive) as package:
            manifest = json.loads(package.read("manifest.json"))
        for change in (lambda m: m["files"].pop(),
                       lambda m: m.update(missing=["continuity.sqlite3"]),
                       lambda m: m["files"][0].update(path="sqlite/continuity.sqlite3"),
                       lambda m: m.update(catalog="v0.16")):
            candidate = copy.deepcopy(manifest)
            change(candidate)
            with self.subTest(candidate=candidate), self.assertRaises(backup.BackupError):
                backup._validate_manifest(candidate)

    def test_v16_twelve_ledger_archive_restores_without_inventing_attention(self):
        archive = self.legacy_archive(explicit=True, catalog="v0.16")
        result = backup.verify_archive(archive)
        self.assertEqual(result["catalog"], "v0.16")
        self.assertEqual(result["files"], 12)
        self.assertEqual(result["missing"], ["attention.sqlite3", "recollection.sqlite3"])
        preview = backup.restore_sqlite(archive, self.target)
        self.assertEqual(preview["missing"], ["attention.sqlite3", "recollection.sqlite3"])
        self.assertFalse(self.target.exists())
        restored = backup.restore_sqlite(archive, self.target, apply=True, writers_stopped=True, confirm=backup.CONFIRM)
        self.assertEqual(restored["missing"], ["attention.sqlite3", "recollection.sqlite3"])
        self.assertEqual({path.name for path in self.target.iterdir()}, set(backup.V16_LEDGERS))
        self.assertFalse((self.target/"attention.sqlite3").exists())

    def test_v16_catalog_cannot_hide_omissions_or_smuggle_attention(self):
        archive = self.legacy_archive(explicit=True, catalog="v0.16")
        with zipfile.ZipFile(archive) as package:
            manifest = json.loads(package.read("manifest.json"))
        for change in (lambda m: m["files"].pop(),
                       lambda m: m.update(missing=["attention.sqlite3"]),
                       lambda m: m["files"][0].update(path="sqlite/attention.sqlite3"),
                       lambda m: m["files"][0]["tables"].update(attention_events=0),
                       lambda m: m.update(catalog="v0.17")):
            candidate = copy.deepcopy(manifest)
            change(candidate)
            with self.subTest(candidate=candidate), self.assertRaises(backup.BackupError):
                backup._validate_manifest(candidate)

    def test_v17_thirteen_ledger_archive_restores_without_inventing_recollection(self):
        archive = self.legacy_archive(explicit=True, catalog="v0.17")
        result = backup.verify_archive(archive)
        self.assertEqual(result["catalog"], "v0.17")
        self.assertEqual(result["files"], 13)
        self.assertEqual(result["missing"], ["recollection.sqlite3"])
        preview = backup.restore_sqlite(archive, self.target)
        self.assertEqual(preview["missing"], ["recollection.sqlite3"])
        self.assertFalse(self.target.exists())
        restored = backup.restore_sqlite(archive, self.target, apply=True, writers_stopped=True, confirm=backup.CONFIRM)
        self.assertEqual(restored["missing"], ["recollection.sqlite3"])
        self.assertEqual({path.name for path in self.target.iterdir()}, set(backup.V17_LEDGERS))
        self.assertFalse((self.target/"recollection.sqlite3").exists())

    def test_v17_catalog_cannot_hide_omissions_or_smuggle_recollection(self):
        archive = self.legacy_archive(explicit=True, catalog="v0.17")
        with zipfile.ZipFile(archive) as package:
            manifest = json.loads(package.read("manifest.json"))
        for change in (lambda m: m["files"].pop(),
                       lambda m: m.update(missing=["recollection.sqlite3"]),
                       lambda m: m["files"][0].update(path="sqlite/recollection.sqlite3"),
                       lambda m: m["files"][0]["tables"].update(recollection_inputs=0),
                       lambda m: m.update(catalog="v0.18")):
            candidate = copy.deepcopy(manifest)
            change(candidate)
            with self.subTest(candidate=candidate), self.assertRaises(backup.BackupError):
                backup._validate_manifest(candidate)

    def test_catalog_version_cannot_hide_missing_files_or_smuggle_new_ledger(self):
        archive = self.legacy_archive()
        with zipfile.ZipFile(archive) as package:
            legacy = json.loads(package.read("manifest.json"))
        for change in (
            lambda m: m["files"].pop(),
            lambda m: m.update(missing=["bond.sqlite3"]),
            lambda m: m["files"][0].update(path="sqlite/bond.sqlite3"),
            lambda m: m.update(catalog="v0.15"),
            lambda m: m["files"][0]["tables"].update(bond_profiles=0),
        ):
            candidate = copy.deepcopy(legacy)
            change(candidate)
            with self.subTest(candidate=candidate), self.assertRaises(backup.BackupError):
                backup._validate_manifest(candidate)

    def test_new_archive_requires_explicit_known_catalog_and_complete_inventory(self):
        self.seed()
        self.archive_now()
        with zipfile.ZipFile(self.archive) as package:
            current = json.loads(package.read("manifest.json"))
        self.assertEqual(current["format"], backup.FORMAT)
        self.assertEqual(current["catalog"], "v0.18")
        for change in (lambda m: m.pop("catalog"), lambda m: m.update(catalog="future"),
                       lambda m: m.update(catalog=[]), lambda m: m["missing"].remove("bond.sqlite3")):
            candidate = copy.deepcopy(current)
            change(candidate)
            with self.subTest(candidate=candidate), self.assertRaises(backup.BackupError):
                backup._validate_manifest(candidate)

    def test_real_store_schema_identity_budget_growth_and_plan_survive_restore(self):
        classes = (("store", "Store", "living_world"), ("brain_core", "BrainBook", "brain_book"),
            ("mind", "MindStore", "mind"), ("cognition", "CognitiveStore", "cognition"),
            ("experience_loop", "ExperienceLoop", "experience_loop"), ("proactive_feedback", "ProactiveFeedback", "proactive_feedback"),
            ("self_state", "SelfState", "self_state"), ("growth_store", "GrowthStore", "growth"),
            ("tool_store", "ToolStore", "tool_memory"), ("companion_store", "CompanionStore", "companion"),
            ("bond_store", "BondStore", "bond"), ("continuity_store", "ContinuityStore", "continuity"),
            ("attention_store", "AttentionStore", "attention"),
            ("recollection_store", "RecollectionStore", "recollection"))
        for module, name, filename in classes:
            store = getattr(importlib.import_module("MaiLivingWorld."+module), name)(self.source/(filename+".sqlite3"))
            try:
                if module == "self_state":
                    identity = store.configure_owner("12345678", now=100)
                    self.assertTrue(store.reserve_thought(now=101, day_start=0, min_interval_seconds=0, daily_limit=2))
                elif module == "growth_store":
                    growth = store.state(now=100)
                elif module == "companion_store":
                    plan = store.propose_plan(owner_id="12345678", scope="private:12345678", title="一起读书",
                        next_step="第一章", message_id="trusted-real-schema-test", summary_hash="a"*64, now=100)
                    plan = store.command_plan(plan["id"], owner_id="12345678", action="accept", expected_revision=plan["revision"],
                        message_id="trusted-accept", summary_hash="b"*64, now=101)
                elif module == "bond_store":
                    message = {"platform": "qq", "session_id": "owner-test-stream", "message_id": "bond-backup-test",
                        "message_info": {"user_info": {"user_id": "12345678"}, "group_info": None},
                        "processed_plain_text": "记下这次一起读书的约定"}
                    moment = store.confirm_moment(message, owner_id="12345678", summary="一起读书的约定", now=101)
                    store.command(dict(message, message_id="bond-ban", processed_plain_text="不要叫我老板"),
                        owner_id="12345678", action="ban_address", value="老板", now=102)
                    old = store.confirm_moment(dict(message, message_id="bond-old", processed_plain_text="记下旧回忆"),
                        owner_id="12345678", summary="旧回忆", now=103)
                    retired = store.retire_moment(dict(message, message_id="bond-retire", processed_plain_text="撤回旧回忆"),
                        owner_id="12345678", moment_id=old["id"], expected_revision=old["revision"], now=104)
                elif module == "continuity_store":
                    message = {"platform": "qq", "session_id": "owner-test-stream", "message_id": "continuity-backup-input",
                        "message_info": {"user_info": {"user_id": "12345678"}, "group_info": None},
                        "processed_plain_text": "我有点担心考试"}
                    source = store.enqueue(message, owner_id="12345678", now=100)
                    batch = store.claim("12345678", now=101)
                    store.complete(batch["id"], batch["token"], {"updates": [{"action": "open", "thread_id": "",
                        "kind": "concern", "title": "考试", "summary": "担心考试", "evidence": [{"id": source, "quote": "担心考试"}]}]},
                        owner_id="12345678", expected_revision=batch["base_revision"], now=102)
                    old_thread = store.threads("12345678", now=102)[0]
                    source = store.enqueue(dict(message, message_id="continuity-backup-resolved", processed_plain_text="考试已经结束了"),
                                           owner_id="12345678", now=103)
                    batch = store.claim("12345678", now=104)
                    store.complete(batch["id"], batch["token"], {"updates": [{"action": "resolve", "thread_id": old_thread["id"],
                        "kind": "concern", "title": "考试", "summary": "考试已结束", "evidence": [{"id": source, "quote": "考试已经结束了"}]}]},
                        owner_id="12345678", expected_revision=batch["base_revision"], now=105)
                    store.command(dict(message, message_id="continuity-backup-busy", processed_plain_text="接下来我要忙一会儿"),
                        owner_id="12345678", action="contact_pause", value="busy", until=500, now=106)
                    self.assertTrue(store.reserve_cycle("12345678", day_start=0, interval_seconds=0, daily_limit=2, now=107))
                    continuity_state = store.state("12345678", now=108)
                    continuity_threads = store.threads("12345678", status="all", now=108)
                elif module == "attention_store":
                    attention = seed_attention(store)
                    attention_rows = {table: [dict(row) for row in store.con.execute("SELECT * FROM "+table)]
                                      for table in backup.LEDGERS["attention.sqlite3"]}
                elif module == "recollection_store":
                    recollection_rows = seed_recollection(store)
            finally:
                store.close()
        self.archive_now()
        self.restore_now()
        from MaiLivingWorld.self_state import SelfState
        from MaiLivingWorld.growth_store import GrowthStore
        from MaiLivingWorld.companion_store import CompanionStore
        from MaiLivingWorld.bond_store import BondStore
        from MaiLivingWorld.continuity_store import ContinuityStore
        from MaiLivingWorld.attention_store import AttentionStore
        from MaiLivingWorld.recollection_store import RecollectionStore
        stores = [SelfState(self.target/"self_state.sqlite3"), GrowthStore(self.target/"growth.sqlite3"),
                  CompanionStore(self.target/"companion.sqlite3"), BondStore(self.target/"bond.sqlite3"),
                  ContinuityStore(self.target/"continuity.sqlite3"), AttentionStore(self.target/"attention.sqlite3"),
                  RecollectionStore(self.target/"recollection.sqlite3")]
        try:
            current = stores[0].state()
            self.assertEqual(current["agent_id"], identity["agent_id"])
            self.assertEqual(current["owner_id"], identity["owner_id"])
            self.assertEqual(current["thought_count"], 1)
            self.assertEqual(stores[1].state(now=100), growth)
            self.assertEqual(stores[2].plan(plan["id"], owner_id="12345678"), plan)
            self.assertEqual(stores[3].moments("12345678"), [moment])
            self.assertEqual(stores[3].moments("12345678", status="retired"), [retired])
            self.assertEqual(stores[3].profile("12345678")["blocked_addresses"], ["老板"])
            self.assertEqual(len(stores[3].audit("12345678")), 4)
            self.assertEqual(stores[4].state("12345678", now=108), continuity_state)
            self.assertEqual(stores[4].threads("12345678", status="all", now=108), continuity_threads)
            self.assertEqual(stores[4].threads("12345678", now=108), [])
            self.assertEqual(stores[4].state("12345678", now=501)["contact"]["effective_mode"], "normal")
            self.assertEqual(stores[5].state("12345678", now=103), attention)
            for table in backup.LEDGERS["attention.sqlite3"]:
                self.assertEqual([dict(row) for row in stores[5].con.execute("SELECT * FROM "+table)], attention_rows[table])
            stores[5].capture(attention_message(), owner_id="12345678", now=104, engagement_seconds=1)
            self.assertEqual(stores[5].state("12345678", now=104)["revision"], attention["revision"])
            self.assertEqual(stores[5].state("12345678", now=104)["cycle_count"], 1)
            self.assertEqual(stores[5].con.execute("SELECT COUNT(*) FROM attention_receipts").fetchone()[0], 1)
            assert_recollection_restore(self, stores[6], recollection_rows)
        finally:
            for store in stores:
                store.close()


def schema():
    return [{"Field": "id", "Type": "bigint", "Null": "NO", "Key": "PRI", "Default": None, "Extra": "auto_increment", "Collation": None},
            {"Field": "payload", "Type": "longtext", "Null": "NO", "Key": "", "Default": None, "Extra": "", "Collation": "utf8mb4_0900_ai_ci"}]


class FakeMySQL:
    def __init__(self, rows=None, fail=False, prefix="mlw_"):
        self.prefix = prefix
        self.rows = copy.deepcopy(rows if rows is not None else {"self_identity": [], "companion_plans": []})
        self.schemas = {table: schema() for table in self.rows}
        self.increments = {table: 900 for table in self.rows}
        self.cached_increments = dict(self.increments)
        self.stats_expiry = 86400
        self.unopened = set()
        self.operations, self.result = [], []
        self.closed = self.committed = False
        self.before = copy.deepcopy(self.rows)
        self.fail = fail
        self.definition_suffix = ""

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        self.operations.append((sql, params))
        self.result = []
        if sql.startswith("START TRANSACTION"):
            self.before = copy.deepcopy(self.rows)
        elif sql == "SET SESSION information_schema_stats_expiry = 0":
            self.stats_expiry = 0
        elif sql.startswith("SHOW TABLE STATUS"):
            counters = self.increments if self.stats_expiry == 0 else self.cached_increments
            self.result = [{"Name": self.prefix+table, "Engine": "InnoDB", "Auto_increment": None if table in self.unopened else counters[table]}
                           for table in self.rows if params is None or self.prefix+table == params[0]]
        elif sql.startswith("SHOW FULL COLUMNS"):
            table = re.findall(r"`([^`]+)`", sql)[0][len(self.prefix):]
            self.result = copy.deepcopy(self.schemas[table])
        elif sql.startswith("SHOW CREATE TABLE"):
            table = re.findall(r"`([^`]+)`", sql)[0]
            self.unopened.discard(table[len(self.prefix):])
            self.result = [{"Create Table": "CREATE TABLE `"+table+"` (id BIGINT PRIMARY KEY AUTO_INCREMENT, payload LONGTEXT) ENGINE=InnoDB AUTO_INCREMENT=900 "+self.definition_suffix}]
        elif sql.startswith("SELECT"):
            physical = re.search(r" FROM `([^`]+)`", sql).group(1)
            table = physical[len(self.prefix):]
            self.result = sorted(copy.deepcopy(self.rows[table]), key=lambda row: row["id"])
            if " LIMIT 1" in sql:
                self.result = self.result[:1]
        elif sql.startswith("ALTER TABLE"):
            physical = re.findall(r"`([^`]+)`", sql)[0]
            self.increments[physical[len(self.prefix):]] = int(sql.rsplit(" ", 1)[-1])
        elif sql.startswith("INSERT INTO"):
            if self.fail:
                raise RuntimeError("super-private-password and database URL")
            physical, *columns = re.findall(r"`([^`]+)`", sql)
            self.rows[physical[len(self.prefix):]].append(dict(zip(columns, params)))

    def fetchone(self):
        return self.result.pop(0) if self.result else None

    def fetchall(self):
        rows, self.result = self.result, []
        return rows

    def fetchmany(self, size):
        rows, self.result = self.result[:size], self.result[size:]
        return rows

    def rollback(self):
        self.rows = copy.deepcopy(self.before)

    def commit(self):
        self.committed = True
        self.before = copy.deepcopy(self.rows)

    def close(self):
        self.closed = True


class MySQLBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.archive = Path(self.temp.name)/"mysql.zip"
        self.config = {"prefix": "mlw_", "password": "secret-not-archived"}
        self.data = {"self_identity": [{"id": 2, "payload": "persistent-owner"}], "companion_plans": [{"id": 8, "payload": "共同计划"}]}
        self.source = FakeMySQL(self.data)
        backup.backup_mysql(self.config, self.archive, apply=True, writers_stopped=True, connect=lambda _: self.source)

    def restore(self, target, **kwargs):
        options = dict(apply=True, writers_stopped=True, confirm=backup.CONFIRM, connect=lambda _: target)
        options.update(kwargs)
        return backup.restore_mysql(self.archive, self.config, **options)

    def test_consistent_snapshot_and_roundtrip_single_commit(self):
        self.assertIn(("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY", None), self.source.operations)
        target = FakeMySQL()
        target.increments = {key: 1 for key in target.rows}
        result = self.restore(target)
        self.assertEqual(result["rows"], 2)
        self.assertTrue(target.committed)
        self.assertTrue(target.closed)
        self.assertEqual(target.rows, self.data)
        self.assertEqual(set(target.increments.values()), {900})
        self.assertTrue(any("FOR UPDATE" in sql for sql, _ in target.operations))

    def test_dry_run_reads_but_never_inserts_or_alters(self):
        target = FakeMySQL()
        self.assertTrue(self.restore(target, apply=False)["dry_run"])
        self.assertFalse(target.committed)
        self.assertFalse(any(sql.startswith(("INSERT", "ALTER")) for sql, _ in target.operations))

    def test_nonempty_target_refused_before_any_mutation(self):
        target = FakeMySQL({"self_identity": [{"id": 99, "payload": "real-owner"}], "companion_plans": []})
        before = copy.deepcopy(target.rows)
        with self.assertRaisesRegex(backup.BackupError, "not_empty"):
            self.restore(target)
        self.assertEqual(target.rows, before)
        self.assertFalse(any(sql.startswith(("INSERT", "ALTER")) for sql, _ in target.operations))

    def test_stale_null_or_low_counter_cache_never_changes_archive_or_restore(self):
        for stale in (None, 1):
            with self.subTest(stale=stale):
                archive = Path(self.temp.name)/("stale-"+str(stale)+".zip")
                source = FakeMySQL(self.data)
                source.cached_increments = {key: stale for key in source.rows}
                backup.backup_mysql(self.config, archive, apply=True, writers_stopped=True, connect=lambda _: source)
                with zipfile.ZipFile(archive) as package:
                    manifest = json.loads(package.read("manifest.json"))
                self.assertEqual({item["auto_increment"] for item in manifest["files"]}, {900})
                statements = [sql for sql, _ in source.operations]
                self.assertLess(statements.index("SET SESSION information_schema_stats_expiry = 0"), statements.index("SHOW TABLE STATUS"))
                # The same nonempty source must be refused for its rows, without
                # reporting a fake schema change caused by cached NULL -> 1.
                with self.assertRaisesRegex(backup.BackupError, "^target_not_empty$"):
                    backup.restore_mysql(archive, self.config, apply=True, writers_stopped=True,
                        confirm=backup.CONFIRM, connect=lambda _: source)
                target = FakeMySQL()
                target.increments = {key: 1 for key in target.rows}
                target.cached_increments = {key: None for key in target.rows}
                backup.restore_mysql(archive, self.config, apply=True, writers_stopped=True,
                    confirm=backup.CONFIRM, connect=lambda _: target)
                self.assertEqual(target.rows, self.data)
                self.assertEqual(set(target.increments.values()), {900})

    def test_missing_live_counter_is_rejected_not_coerced_to_one(self):
        source = FakeMySQL(self.data)
        source.increments["self_identity"] = None
        archive = Path(self.temp.name)/"unreliable.zip"
        with self.assertRaisesRegex(backup.BackupError, "^mysql_auto_increment_mismatch$"):
            backup.backup_mysql(self.config, archive, apply=True, writers_stopped=True, connect=lambda _: source)
        self.assertFalse(archive.exists())

    def test_unopened_tables_are_opened_before_reading_native_counter(self):
        archive = Path(self.temp.name)/"unopened.zip"
        source = FakeMySQL(self.data)
        source.unopened = set(source.rows)
        source.increments = {key: 1900 for key in source.rows}
        backup.backup_mysql(self.config, archive, apply=True, writers_stopped=True, connect=lambda _: source)
        with zipfile.ZipFile(archive) as package:
            manifest = json.loads(package.read("manifest.json"))
        self.assertEqual({item["auto_increment"] for item in manifest["files"]}, {1900})
        target = FakeMySQL()
        target.unopened = set(target.rows)
        target.increments = {key: 1 for key in target.rows}
        backup.restore_mysql(archive, self.config, apply=True, writers_stopped=True,
            confirm=backup.CONFIRM, connect=lambda _: target)
        self.assertEqual(target.rows, self.data)
        self.assertEqual(set(target.increments.values()), {1900})
        for con in (source, target):
            for table in con.rows:
                physical = con.prefix+table
                opened = next(i for i, (sql, _) in enumerate(con.operations) if sql == "SHOW CREATE TABLE `"+physical+"`")
                read = next(i for i, (sql, args) in enumerate(con.operations) if sql == "SHOW TABLE STATUS WHERE Name = %s" and args == (physical,))
                self.assertLess(opened, read)

    def test_non_auto_increment_tables_keep_null_counter(self):
        source = FakeMySQL(self.data)
        for name in source.rows:
            source.increments[name] = None
            source.schemas[name][0]["Extra"] = ""
        archive = Path(self.temp.name)/"no-increments.zip"
        backup.backup_mysql(self.config, archive, apply=True, writers_stopped=True, connect=lambda _: source)
        self.assertTrue(backup.verify_archive(archive)["verified"])
        target = FakeMySQL()
        for name in target.rows:
            target.increments[name] = None
            target.schemas[name][0]["Extra"] = ""
        backup.restore_mysql(archive, self.config, apply=True, writers_stopped=True,
            confirm=backup.CONFIRM, connect=lambda _: target)
        self.assertEqual(target.rows, self.data)
        self.assertFalse(any(sql.startswith("ALTER TABLE") for sql, _ in target.operations))

    def test_schema_and_indexes_must_match(self):
        for changed in ("column", "index"):
            target = FakeMySQL()
            if changed == "column":
                target.schemas["self_identity"][1]["Type"] = "text"
            else:
                target.definition_suffix = "EXTRA UNIQUE INDEX"
            with self.subTest(changed=changed), self.assertRaisesRegex(backup.BackupError, "mismatch"):
                self.restore(target)
            self.assertFalse(any(sql.startswith(("INSERT", "ALTER")) for sql, _ in target.operations))

    def test_restore_failure_rolls_back_every_insert_and_redacts_errors(self):
        target = FakeMySQL(fail=True)
        with self.assertRaisesRegex(backup.BackupError, "^mysql_restore_failed$"):
            self.restore(target)
        self.assertFalse(target.committed)
        self.assertEqual(target.rows, {"self_identity": [], "companion_plans": []})

    def test_credential_and_raw_schema_not_archived(self):
        with zipfile.ZipFile(self.archive) as package:
            manifest = package.read("manifest.json")
        self.assertNotIn(b"secret-not-archived", manifest)
        self.assertNotIn(b"CREATE TABLE", manifest)
        self.assertNotIn(b"persistent-owner", manifest)

    def test_bare_or_foreign_schema_rejected(self):
        for rows in ({}, {"business_data": []}):
            with self.subTest(rows=rows), self.assertRaises(backup.BackupError):
                self.restore(FakeMySQL(rows))

    def test_legacy_mysql_44_tables_require_matching_schema_before_upgrade(self):
        old_tables = {table: [] for group in backup.V14_LEDGERS.values() for table in group}
        old_tables.update(copy.deepcopy(self.data))
        source = FakeMySQL(old_tables)
        current = Path(self.temp.name)/"old-schema.zip"
        legacy = Path(self.temp.name)/"legacy-v14.zip"
        backup.backup_mysql(self.config, current, apply=True, writers_stopped=True, connect=lambda _: source)
        with zipfile.ZipFile(current) as origin, zipfile.ZipFile(legacy, "w") as destination:
            for info in origin.infolist():
                data = origin.read(info)
                if info.filename == "manifest.json":
                    manifest = json.loads(data)
                    manifest["format"] = backup.LEGACY_FORMAT
                    manifest.pop("catalog")
                    data = json.dumps(manifest).encode()
                destination.writestr(info, data)
        self.assertEqual(backup.verify_archive(legacy)["files"], 44)
        new_target = FakeMySQL({table: [] for table in backup.TABLES})
        with self.assertRaisesRegex(backup.BackupError, "^mysql_catalog_requires_matching_schema_then_upgrade$"):
            backup.restore_mysql(legacy, self.config, apply=True, writers_stopped=True,
                confirm=backup.CONFIRM, connect=lambda _: new_target)
        self.assertFalse(any(sql.startswith(("INSERT", "ALTER")) for sql, _ in new_target.operations))
        old_target = FakeMySQL({table: [] for table in old_tables})
        backup.restore_mysql(legacy, self.config, apply=True, writers_stopped=True,
            confirm=backup.CONFIRM, connect=lambda _: old_target)
        self.assertEqual(old_target.rows, old_tables)

    def test_mysql_value_codec_is_exact_and_rejects_nonfinite(self):
        values = [None, True, 7, "中文", 1.25, b"\x00\xff", Decimal("1.2300"), datetime(2026, 1, 2, 3, 4), date(2026, 1, 2), time(3, 4), timedelta(days=-1, microseconds=17)]
        for value in values:
            self.assertEqual(backup._decode(backup._loads(backup._json(backup._encode(value)))), value)
        for value in (float("inf"), float("nan"), Decimal("NaN")):
            with self.assertRaises(backup.BackupError):
                backup._encode(value)

    def test_v15_v16_v17_mysql_catalogs_require_matching_schema_before_upgrade(self):
        for catalog, expected_count in (("v0.15", 47), ("v0.16", 51), ("v0.17", 54)):
            with self.subTest(catalog=catalog):
                old_tables = {table: [] for group in backup.CATALOGS[catalog].values() for table in group}
                old_tables.update(copy.deepcopy(self.data))
                old_tables["bond_moments"] = [{"id": 19, "payload": "保留旧主人纪念与撤回状态"}]
                if catalog in {"v0.16", "v0.17"}:
                    old_tables["continuity_threads"] = [{"id": 20, "payload": "保留已解决话题及原话来源"}]
                current = Path(self.temp.name)/(catalog+"-source.zip")
                archive = Path(self.temp.name)/(catalog+"-schema.zip")
                backup.backup_mysql(self.config, current, apply=True, writers_stopped=True,
                                    connect=lambda _: FakeMySQL(old_tables))
                with zipfile.ZipFile(current) as origin, zipfile.ZipFile(archive, "w") as destination:
                    for info in origin.infolist():
                        data = origin.read(info)
                        if info.filename == "manifest.json":
                            manifest = json.loads(data)
                            manifest["catalog"] = catalog
                            data = json.dumps(manifest).encode()
                        destination.writestr(info, data)
                self.assertEqual(backup.verify_archive(archive)["catalog"], catalog)
                self.assertEqual(backup.verify_archive(archive)["files"], expected_count)
                new_target = FakeMySQL({table: [] for table in backup.TABLES})
                with self.assertRaisesRegex(backup.BackupError, "^mysql_catalog_requires_matching_schema_then_upgrade$"):
                    backup.restore_mysql(archive, self.config, apply=True, writers_stopped=True,
                        confirm=backup.CONFIRM, connect=lambda _: new_target)
                self.assertFalse(any(sql.startswith(("INSERT", "ALTER")) for sql, _ in new_target.operations))
                old_target = FakeMySQL({table: [] for table in old_tables})
                backup.restore_mysql(archive, self.config, apply=True, writers_stopped=True,
                    confirm=backup.CONFIRM, connect=lambda _: old_target)
                self.assertEqual(old_target.rows, old_tables)

    def test_config_only_uses_supplied_environment_and_never_reveals_credentials(self):
        env = {"MLW_MYSQL_USER": "operator", "MLW_MYSQL_PASSWORD": "private", "MLW_MYSQL_DATABASE": "restore_only", "MLW_MYSQL_PREFIX": "mlw_"}
        config = backup.mysql_config(env)
        self.assertEqual(config["password"], "private")
        env["MLW_MYSQL_PORT"] = "private"
        with self.assertRaisesRegex(backup.BackupError, "^invalid_mysql_configuration$"):
            backup.mysql_config(env)


@unittest.skipUnless(os.getenv("MLW_LIVE_MYSQL") == "1" and os.getenv("MLW_BACKUP_MYSQL_DATABASE", "").startswith("mlw_backup_test"),
                     "Only with an explicitly configured empty dedicated MySQL CI database")
class BackupMySQLLiveTests(unittest.TestCase):
    def test_real_58_table_archive_empty_restore_and_counter_high_water(self):
        import pymysql
        from MaiLivingWorld.scripts.migrate_all_sqlite_to_mysql import ensure_schema
        from MaiLivingWorld.self_state import SelfState
        from MaiLivingWorld.attention_store import AttentionStore
        from MaiLivingWorld.recollection_store import RecollectionStore
        env = dict(os.environ, MLW_MYSQL_DATABASE=os.environ["MLW_BACKUP_MYSQL_DATABASE"], MLW_MYSQL_PREFIX="backup_")
        config = backup.mysql_config(env)
        settings = {key: value for key, value in config.items() if key != "prefix"}
        settings["table_prefix"] = config["prefix"]
        connection = pymysql.connect(**{key: value for key, value in config.items() if key != "prefix"}, autocommit=True)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SHOW TABLES")
                self.assertEqual(cursor.fetchall(), (), "backup CI database must be empty before this test")
            with tempfile.TemporaryDirectory() as temp:
                directory, archive = Path(temp), Path(temp)/"mysql.zip"
                created = False
                try:
                    created = True
                    ensure_schema(settings, directory)
                    state = SelfState(directory/"unused.sqlite3", mysql_settings=settings)
                    try:
                        identity = state.configure_owner("12345678", now=100)
                        state.reserve_thought(now=101, day_start=0, min_interval_seconds=0, daily_limit=2)
                    finally:
                        state.close()
                    attention = AttentionStore(directory/"unused-attention.sqlite3", mysql_settings=settings)
                    try:
                        attention_before = seed_attention(attention)
                    finally:
                        attention.close()
                    recollection = RecollectionStore(directory/"unused-recollection.sqlite3", mysql_settings=settings)
                    try:
                        recollection_before = seed_recollection(recollection)
                    finally:
                        recollection.close()
                    with connection.cursor() as cursor:
                        cursor.execute("ALTER TABLE backup_attempts AUTO_INCREMENT = 900")
                        cursor.execute("INSERT INTO backup_attempts(target,created_at,kind) VALUES ('private:test',100,'test')")
                        deleted_id = cursor.lastrowid
                        cursor.execute("DELETE FROM backup_attempts WHERE id=%s", (deleted_id,))
                    result = backup.backup_mysql(config, archive, apply=True, writers_stopped=True)
                    self.assertEqual(result["tables"], 58)
                    self.assertTrue(backup.verify_archive(archive)["verified"])
                    with self.assertRaisesRegex(backup.BackupError, "target_not_empty"):
                        backup.restore_mysql(archive, config, apply=True, writers_stopped=True, confirm=backup.CONFIRM)
                    # Every table here was created by this test in its dedicated empty DB.
                    with connection.cursor() as cursor:
                        for table in reversed(backup.TABLES):
                            cursor.execute("DROP TABLE `backup_"+table+"`")
                    ensure_schema(settings, directory)
                    restored = backup.restore_mysql(archive, config, apply=True, writers_stopped=True, confirm=backup.CONFIRM)
                    self.assertEqual(restored["tables"], 58)
                    state = SelfState(directory/"unused.sqlite3", mysql_settings=settings)
                    try:
                        self.assertEqual(state.state()["agent_id"], identity["agent_id"])
                        self.assertEqual(state.state()["thought_count"], 1)
                    finally:
                        state.close()
                    attention = AttentionStore(directory/"unused-attention.sqlite3", mysql_settings=settings)
                    try:
                        self.assertEqual(attention.state("12345678", now=103), attention_before)
                        attention.capture(attention_message(), owner_id="12345678", now=104, engagement_seconds=1)
                        self.assertEqual(attention.state("12345678", now=104)["revision"], attention_before["revision"])
                        self.assertEqual(attention.state("12345678", now=104)["cycle_count"], 1)
                        self.assertEqual(attention.con.execute("SELECT COUNT(*) FROM attention_receipts").fetchone()[0], 1)
                    finally:
                        attention.close()
                    recollection = RecollectionStore(directory/"unused-recollection.sqlite3", mysql_settings=settings)
                    try:
                        assert_recollection_restore(self, recollection, recollection_before)
                    finally:
                        recollection.close()
                    with connection.cursor() as cursor:
                        cursor.execute("INSERT INTO backup_attempts(target,created_at,kind) VALUES ('private:test',101,'test')")
                        self.assertGreater(cursor.lastrowid, deleted_id)
                finally:
                    if created:
                        with connection.cursor() as cursor:
                            for table in reversed(backup.TABLES):
                                cursor.execute("DROP TABLE IF EXISTS `backup_"+table+"`")
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
