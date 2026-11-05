"""Recollection ledger migration, canonical DDL and archive catalog integration."""
import importlib
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from MaiLivingWorld.recollection_store import RecollectionStore
from MaiLivingWorld.growth_store import source_fingerprint
from MaiLivingWorld.mysql_compat import TABLES, MySQLCompatConnection, mysql_create_table
from MaiLivingWorld.scripts import backup_state as backup
from MaiLivingWorld.scripts import migrate_all_sqlite_to_mysql as migration

ROOT = Path(__file__).resolve().parents[1]
RECOLLECTION_TABLES = ("recollection_control", "recollection_inputs", "recollection_revisions", "recollection_deliveries")
OWNER = "12345678"


def seed_previous(directory):
    for module, name, filename in (
        ("store", "Store", "living_world"), ("brain_core", "BrainBook", "brain_book"),
        ("mind", "MindStore", "mind"), ("cognition", "CognitiveStore", "cognition"),
        ("experience_loop", "ExperienceLoop", "experience_loop"),
        ("proactive_feedback", "ProactiveFeedback", "proactive_feedback"),
        ("self_state", "SelfState", "self_state"), ("growth_store", "GrowthStore", "growth"),
        ("tool_store", "ToolStore", "tool_memory"), ("companion_store", "CompanionStore", "companion"),
        ("bond_store", "BondStore", "bond"), ("continuity_store", "ContinuityStore", "continuity"),
        ("attention_store", "AttentionStore", "attention"),
    ):
        store = getattr(importlib.import_module("MaiLivingWorld."+module), name)(directory/(filename+".sqlite3"))
        store.close()


def message(mid="recollection-migration-message"):
    return {"platform": "qq", "account_id": "99999", "session_id": "owner-private-stream", "message_id": mid,
            "message_info": {"user_info": {"user_id": OWNER}, "group_info": None},
            "processed_plain_text": "我现在住在北京"}


def seed_recollection(store):
    raw = {"id": "brain:fact:F1", "source": "brain_fact", "kind": "fact", "scope": "private:"+OWNER,
           "subject_id": OWNER, "text": "我住在上海", "timestamp": 1,
           "sensitivity": "personal", "evidence_status": "active"}
    digest = source_fingerprint(raw)
    store.enqueue(message(), owner_id=OWNER, now=100)
    batch = store.claim(OWNER, now=101)
    if not batch:
        raise AssertionError("expected one real recollection claim")
    store.complete(batch["id"], batch["token"], {"updates": [{"target_id": raw["id"],
        "target_digest": digest, "old_quote": "我住在上海", "quote": "我现在住在北京", "relation": "replace"}]},
        owner_id=OWNER, expected_revision=batch["base_revision"], targets={raw["id"]: raw}, now=102)
    store.record_delivery(owner_id=OWNER, scope="private:"+OWNER, receipt_id="recollection-migration-receipt",
        topic_refs=[{"id": raw["id"], "digest": digest}], text_digest="a"*64, now=103)
    return {table: [dict(row) for row in store.con.execute("SELECT * FROM "+table)]
            for table in RECOLLECTION_TABLES}


class RecollectionMigrationTests(unittest.TestCase):
    def test_fixed_current_catalog_and_migration_inventories_match(self):
        self.assertEqual(set(migration.LEDGERS), set(backup.LEDGERS))
        self.assertEqual(migration.LEDGERS["recollection.sqlite3"], RECOLLECTION_TABLES)
        self.assertEqual(set(migration.ALL_TABLES), set(backup.TABLES))
        self.assertEqual(set(TABLES) | {"stories", "attempts", "story_attempts"}, set(backup.TABLES))
        self.assertEqual(len(backup.TABLES), 72)
        self.assertEqual(len(backup.LEDGERS), 18)
        self.assertIn("recollection.sqlite3", migration.OPTIONAL_LEDGERS)
        self.assertNotIn("recollection.sqlite3", backup.V17_LEDGERS)

    def test_old_thirteen_library_migration_does_not_invent_recollection_or_owner(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            seed_previous(directory)
            rows, missing = migration.snapshot(directory)
            self.assertEqual(missing, [])
            self.assertFalse((directory/"recollection.sqlite3").exists())
            self.assertEqual(rows["self_identity"], [])
            for table in RECOLLECTION_TABLES:
                self.assertEqual(rows[table], [])

    def test_existing_recollection_retains_revisions_deliveries_and_source_digests(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            seed_previous(directory)
            store = RecollectionStore(directory/"recollection.sqlite3")
            try:
                for table in RECOLLECTION_TABLES:
                    self.assertEqual(store.con.execute("SELECT COUNT(*) FROM "+table).fetchone()[0], 0)
                seed_recollection(store)
                expected = {table: [dict(row) for row in store.con.execute("SELECT * FROM "+table)]
                            for table in RECOLLECTION_TABLES}
            finally:
                store.close()
            rows, missing = migration.snapshot(directory)
            self.assertEqual(missing, [])
            for table in RECOLLECTION_TABLES:
                self.assertEqual(rows[table], expected[table])
                self.assertTrue(rows[table])
            self.assertEqual(rows["recollection_inputs"][0]["status"], "processed")
            self.assertEqual(rows["recollection_revisions"][0]["relation"], "replace")
            self.assertEqual(rows["recollection_revisions"][0]["target_id"], "brain:fact:F1")
            self.assertEqual(len(rows["recollection_deliveries"]), 1)

    def test_present_but_incomplete_recollection_database_is_not_silently_optional(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            seed_previous(directory)
            con = sqlite3.connect(directory/"recollection.sqlite3")
            try:
                con.execute("CREATE TABLE recollection_control(owner_id TEXT PRIMARY KEY)")
                con.commit()
            finally:
                con.close()
            with self.assertRaisesRegex(RuntimeError, "recollection.sqlite3.*recollection_inputs"):
                migration.snapshot(directory)

    def test_empty_schema_initializer_includes_recollection_without_runtime_capture(self):
        calls = []
        class EmptyStore:
            def __init__(self, *args, **kwargs):
                calls.append((args, kwargs))
            def close(self):
                pass
        names = ("MySQLStore", "BrainBook", "MindStore", "CognitiveStore", "ExperienceLoop", "ProactiveFeedback",
                 "SelfState", "GrowthStore", "ToolStore", "CompanionStore", "BondStore", "ContinuityStore", "AttentionStore", "RecollectionStore", "HabitsStore", "FollowthroughStore", "ProjectsStore", "SharedReadingStore")
        with patch.multiple(migration, **{name: EmptyStore for name in names}), tempfile.TemporaryDirectory() as temp:
            migration.ensure_schema({"table_prefix": "test_"}, Path(temp))
        self.assertEqual(len(calls), 18)
        recollection = next(call for call in calls if call[0] and call[0][0].name == "recollection.sqlite3")
        self.assertEqual(recollection[1]["mysql_settings"], {"table_prefix": "test_"})

    def test_official_ddl_comments_defaults_indexes_and_runtime_prefix_match(self):
        ddl = (ROOT/"sql"/"mysql.sql").read_text(encoding="utf-8")
        upgrade = (ROOT/"sql"/"mysql_comments_existing.sql").read_text(encoding="utf-8")
        adapter = object.__new__(MySQLCompatConnection)
        adapter.prefix = "test_"
        with tempfile.TemporaryDirectory() as temp:
            store = RecollectionStore(Path(temp)/"recollection.sqlite3")
            try:
                for table in RECOLLECTION_TABLES:
                    columns = {row[1]: row for row in store.con.execute("PRAGMA table_info("+table+")")}
                    create = re.search(r"CREATE TABLE IF NOT EXISTS `mlw_"+table+r"` \(([\s\S]*?)\) ENGINE=InnoDB[^;]*;", ddl)
                    alter = re.search(r"ALTER TABLE `mlw_"+table+r"`([\s\S]*?);", upgrade)
                    self.assertIsNotNone(create)
                    self.assertIsNotNone(alter)
                    self.assertEqual(set(re.findall(r"^\s+(\w+)\s+(?:VARCHAR|BIGINT|DOUBLE|LONGTEXT)\b", create[1], re.M)), set(columns))
                    self.assertEqual(set(re.findall(r"MODIFY COLUMN `(\w+)`", alter[1])), set(columns))
                    self.assertEqual(create[1].count(" COMMENT '"), len(columns))
                    if "payload" in columns:
                        self.assertIn("payload LONGTEXT", create[1])
                    for name, row in columns.items():
                        if row[4] is not None:
                            self.assertRegex(create[1], r"\b"+name+r"\s+[^\n]*\bDEFAULT "+re.escape(row[4])+r" COMMENT")
                    sqlite_ddl = store.con.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
                    if "payload" in columns:
                        self.assertIn("payload LONGTEXT", mysql_create_table(sqlite_ddl))
                    self.assertIn("test_"+table, adapter._sql("SELECT * FROM "+table+" WHERE owner_id=?"))
                indexes = {row[0] for row in store.con.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_recollection_%'")}
                expected_indexes = {"idx_recollection_inputs_owner": "owner_id,status,created",
                    "idx_recollection_revisions_owner": "owner_id,status,created",
                    "idx_recollection_revisions_target": "owner_id,target_id",
                    "idx_recollection_deliveries_owner": "owner_id,created"}
                self.assertEqual(indexes, set(expected_indexes))
                for index, columns in expected_indexes.items():
                    self.assertIn("KEY `"+index+"` ("+columns+")", ddl)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
