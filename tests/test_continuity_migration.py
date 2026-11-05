"""Continuity schema, optional-ledger migration, and provenance preservation."""
import importlib
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from MaiLivingWorld.continuity_store import ContinuityStore
from MaiLivingWorld.mysql_compat import TABLES, MySQLCompatConnection, mysql_create_table
from MaiLivingWorld.scripts import backup_state as backup
from MaiLivingWorld.scripts import migrate_all_sqlite_to_mysql as migration

ROOT = Path(__file__).resolve().parents[1]
CONTINUITY_TABLES = ("continuity_control", "continuity_inputs", "continuity_threads", "continuity_audit")
OWNER = "12345678"


def seed_previous(directory):
    for module, name, filename in (
        ("store", "Store", "living_world"), ("brain_core", "BrainBook", "brain_book"),
        ("mind", "MindStore", "mind"), ("cognition", "CognitiveStore", "cognition"),
        ("experience_loop", "ExperienceLoop", "experience_loop"),
        ("proactive_feedback", "ProactiveFeedback", "proactive_feedback"),
        ("self_state", "SelfState", "self_state"), ("growth_store", "GrowthStore", "growth"),
        ("tool_store", "ToolStore", "tool_memory"), ("companion_store", "CompanionStore", "companion"),
        ("bond_store", "BondStore", "bond"),
    ):
        store = getattr(importlib.import_module("MaiLivingWorld."+module), name)(directory/(filename+".sqlite3"))
        store.close()


def message(mid, text):
    return {"platform": "qq", "session_id": "owner-private-stream", "message_id": mid,
            "message_info": {"user_info": {"user_id": OWNER}, "group_info": None},
            "processed_plain_text": text}


class ContinuityMigrationTests(unittest.TestCase):
    def test_fixed_catalog_schema_and_migration_inventories_match(self):
        self.assertEqual(set(migration.LEDGERS), set(backup.LEDGERS))
        self.assertEqual(migration.LEDGERS["continuity.sqlite3"], CONTINUITY_TABLES)
        self.assertEqual(set(migration.ALL_TABLES), set(backup.TABLES))
        self.assertEqual(set(TABLES) | {"stories", "attempts", "story_attempts"}, set(backup.TABLES))
        self.assertEqual(len(backup.TABLES), 82)
        self.assertIn("continuity.sqlite3", migration.OPTIONAL_LEDGERS)

    def test_old_eleven_library_migration_does_not_invent_continuity_or_owner(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            seed_previous(directory)
            rows, missing = migration.snapshot(directory)
            self.assertEqual(missing, [])
            self.assertFalse((directory/"continuity.sqlite3").exists())
            self.assertEqual(rows["self_identity"], [])
            for table in CONTINUITY_TABLES:
                self.assertEqual(rows[table], [])

    def test_present_continuity_state_sources_and_audit_enter_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            seed_previous(directory)
            store = ContinuityStore(directory/"continuity.sqlite3")
            try:
                self.assertEqual(store.state(OWNER, now=100)["revision"], 0)
                self.assertEqual(store.con.execute("SELECT COUNT(*) FROM continuity_control").fetchone()[0], 0)
                ident = store.enqueue(message("input", "我有点担心考试"), owner_id=OWNER, now=100)
                batch = store.claim(OWNER, now=101)
                store.complete(batch["id"], batch["token"], {"updates": [{"action": "open", "thread_id": "",
                    "kind": "concern", "title": "考试", "summary": "担心考试",
                    "evidence": [{"id": ident, "quote": "担心考试"}]}]}, owner_id=OWNER,
                    expected_revision=batch["base_revision"], now=102)
                store.command(message("pause", "我现在忙一会儿"), owner_id=OWNER,
                              action="contact_pause", value="busy", until=500, now=103)
                thread = store.threads(OWNER, now=104)[0]
            finally:
                store.close()
            rows, missing = migration.snapshot(directory)
            self.assertEqual(missing, [])
            self.assertEqual(len(rows["continuity_control"]), 1)
            self.assertEqual(json.loads(rows["continuity_control"][0]["payload"])["contact"], {"mode": "busy", "until": 500})
            self.assertEqual(rows["continuity_threads"][0]["id"], thread["id"])
            self.assertEqual(json.loads(rows["continuity_threads"][0]["payload"])["source_refs"][0]["id"], ident)
            self.assertEqual(rows["continuity_inputs"][0]["status"], "processed")
            self.assertEqual(json.loads(rows["continuity_inputs"][0]["payload"])["text"], "")
            self.assertEqual(len(rows["continuity_audit"]), 2)

    def test_safe_schema_initializer_includes_empty_continuity_store(self):
        calls = []
        class EmptyStore:
            def __init__(self, *args, **kwargs):
                calls.append((args, kwargs))
            def close(self):
                pass
        names = ("MySQLStore", "BrainBook", "MindStore", "CognitiveStore", "ExperienceLoop", "ProactiveFeedback",
                 "SelfState", "GrowthStore", "ToolStore", "CompanionStore", "BondStore", "ContinuityStore", "AttentionStore", "RecollectionStore", "HabitsStore", "FollowthroughStore", "ProjectsStore", "SharedReadingStore", "AdviceStore", "ReferenceStore", "RepairStore")
        with patch.multiple(migration, **{name: EmptyStore for name in names}), tempfile.TemporaryDirectory() as temp:
            migration.ensure_schema({"table_prefix": "test_"}, Path(temp))
        self.assertEqual(len(calls), 21)
        continuity = next(call for call in calls if call[0] and call[0][0].name == "continuity.sqlite3")
        self.assertEqual(continuity[1]["mysql_settings"], {"table_prefix": "test_"})

    def test_official_ddl_comments_and_runtime_sql_match_real_schema(self):
        ddl = (ROOT/"sql"/"mysql.sql").read_text(encoding="utf-8")
        upgrade = (ROOT/"sql"/"mysql_comments_existing.sql").read_text(encoding="utf-8")
        adapter = object.__new__(MySQLCompatConnection)
        adapter.prefix = "test_"
        with tempfile.TemporaryDirectory() as temp:
            store = ContinuityStore(Path(temp)/"continuity.sqlite3")
            try:
                for table in CONTINUITY_TABLES:
                    columns = {row[1] for row in store.con.execute("PRAGMA table_info("+table+")")}
                    create = re.search(r"CREATE TABLE IF NOT EXISTS `mlw_"+table+r"` \(([\s\S]*?)\) ENGINE=InnoDB[^;]*;", ddl)
                    alter = re.search(r"ALTER TABLE `mlw_"+table+r"`([\s\S]*?);", upgrade)
                    self.assertIsNotNone(create)
                    self.assertIsNotNone(alter)
                    self.assertEqual(set(re.findall(r"^\s+(\w+)\s+(?:VARCHAR|BIGINT|DOUBLE|LONGTEXT)\b", create[1], re.M)), columns)
                    self.assertEqual(set(re.findall(r"MODIFY COLUMN `(\w+)`", alter[1])), columns)
                    self.assertEqual(create[1].count(" COMMENT '"), len(columns))
                    self.assertIn("payload LONGTEXT", create[1])
                    sqlite_ddl = store.con.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
                    self.assertIn("payload LONGTEXT", mysql_create_table(sqlite_ddl))
                    self.assertIn("test_"+table, adapter._sql("SELECT * FROM "+table+" WHERE owner_id=?"))
                indexes = {row[0] for row in store.con.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_continuity_%'")}
                for index in indexes:
                    self.assertIn("KEY `"+index+"`", ddl)
                query = adapter._sql("INSERT INTO continuity_control(owner_id,payload,created,updated) VALUES (?,?,?,?) ON CONFLICT(owner_id) DO UPDATE SET revision=revision")
                self.assertIn("ON DUPLICATE KEY UPDATE revision=revision", query)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
