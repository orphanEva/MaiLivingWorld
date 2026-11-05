"""Attention ledger migration, canonical DDL and archive catalog integration."""
import importlib
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from MaiLivingWorld.attention_store import AttentionStore
from MaiLivingWorld.mysql_compat import TABLES, MySQLCompatConnection, mysql_create_table
from MaiLivingWorld.scripts import backup_state as backup
from MaiLivingWorld.scripts import migrate_all_sqlite_to_mysql as migration

ROOT = Path(__file__).resolve().parents[1]
ATTENTION_TABLES = ("attention_control", "attention_receipts", "attention_events")
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
    ):
        store = getattr(importlib.import_module("MaiLivingWorld."+module), name)(directory/(filename+".sqlite3"))
        store.close()


def message(mid="attention-migration-message"):
    return {"platform": "qq", "account_id": "99999", "session_id": "owner-private-stream", "message_id": mid,
            "message_info": {"user_info": {"user_id": OWNER}, "group_info": None},
            "processed_plain_text": "这是不会写进注意账本的主人聊天原文"}


def seed_attention(store):
    store.capture(message(), owner_id=OWNER, now=100, engagement_seconds=1)
    batch = store.claim(OWNER, candidates=[{"kind": "reading", "key": "reading:"+"a"*64, "version": "b"*64}],
                        day_start=0, daily_limit=6, interval_seconds=0, now=101)
    if not batch:
        raise AssertionError("expected one real attention claim")
    store.finish(OWNER, batch["token"], outcome="progress", now=102)


class AttentionMigrationTests(unittest.TestCase):
    def test_fixed_current_catalog_and_migration_inventories_match(self):
        self.assertEqual(set(migration.LEDGERS), set(backup.LEDGERS))
        self.assertEqual(migration.LEDGERS["attention.sqlite3"], ATTENTION_TABLES)
        self.assertEqual(set(migration.ALL_TABLES), set(backup.TABLES))
        self.assertEqual(set(TABLES) | {"stories", "attempts", "story_attempts"}, set(backup.TABLES))
        self.assertEqual(len(backup.TABLES), 66)
        self.assertEqual(len(backup.LEDGERS), 16)
        self.assertIn("attention.sqlite3", migration.OPTIONAL_LEDGERS)
        self.assertNotIn("attention.sqlite3", backup.V16_LEDGERS)

    def test_old_twelve_library_migration_does_not_invent_attention_or_owner(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            seed_previous(directory)
            rows, missing = migration.snapshot(directory)
            self.assertEqual(missing, [])
            self.assertFalse((directory/"attention.sqlite3").exists())
            self.assertEqual(rows["self_identity"], [])
            for table in ATTENTION_TABLES:
                self.assertEqual(rows[table], [])

    def test_existing_attention_retains_receipt_budget_and_events_without_raw_text(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            seed_previous(directory)
            store = AttentionStore(directory/"attention.sqlite3")
            try:
                for table in ATTENTION_TABLES:
                    self.assertEqual(store.con.execute("SELECT COUNT(*) FROM "+table).fetchone()[0], 0)
                seed_attention(store)
                expected = {table: [dict(row) for row in store.con.execute("SELECT * FROM "+table)]
                            for table in ATTENTION_TABLES}
            finally:
                store.close()
            rows, missing = migration.snapshot(directory)
            self.assertEqual(missing, [])
            for table in ATTENTION_TABLES:
                self.assertEqual(rows[table], expected[table])
                self.assertTrue(rows[table])
            self.assertEqual(rows["attention_control"][0]["cycle_count"], 1)
            self.assertEqual(len(rows["attention_receipts"]), 1)
            self.assertNotIn(message()["processed_plain_text"], json.dumps(expected, ensure_ascii=False))

    def test_present_but_incomplete_attention_database_is_not_silently_optional(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            seed_previous(directory)
            con = sqlite3.connect(directory/"attention.sqlite3")
            try:
                con.execute("CREATE TABLE attention_control(owner_id TEXT PRIMARY KEY)")
                con.commit()
            finally:
                con.close()
            with self.assertRaisesRegex(RuntimeError, "attention.sqlite3.*attention_receipts"):
                migration.snapshot(directory)

    def test_empty_schema_initializer_includes_attention_without_runtime_capture(self):
        calls = []
        class EmptyStore:
            def __init__(self, *args, **kwargs):
                calls.append((args, kwargs))
            def close(self):
                pass
        names = ("MySQLStore", "BrainBook", "MindStore", "CognitiveStore", "ExperienceLoop", "ProactiveFeedback",
                 "SelfState", "GrowthStore", "ToolStore", "CompanionStore", "BondStore", "ContinuityStore", "AttentionStore", "RecollectionStore", "HabitsStore", "FollowthroughStore")
        with patch.multiple(migration, **{name: EmptyStore for name in names}), tempfile.TemporaryDirectory() as temp:
            migration.ensure_schema({"table_prefix": "test_"}, Path(temp))
        self.assertEqual(len(calls), 16)
        attention = next(call for call in calls if call[0] and call[0][0].name == "attention.sqlite3")
        self.assertEqual(attention[1]["mysql_settings"], {"table_prefix": "test_"})

    def test_official_ddl_comments_defaults_indexes_and_runtime_prefix_match(self):
        ddl = (ROOT/"sql"/"mysql.sql").read_text(encoding="utf-8")
        upgrade = (ROOT/"sql"/"mysql_comments_existing.sql").read_text(encoding="utf-8")
        adapter = object.__new__(MySQLCompatConnection)
        adapter.prefix = "test_"
        with tempfile.TemporaryDirectory() as temp:
            store = AttentionStore(Path(temp)/"attention.sqlite3")
            try:
                for table in ATTENTION_TABLES:
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
                indexes = {row[0] for row in store.con.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_attention_%'")}
                self.assertEqual(indexes, {"idx_attention_receipts_owner", "idx_attention_events_owner"})
                for index in indexes:
                    self.assertIn("KEY `"+index+"` (owner_id,created)", ddl)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
