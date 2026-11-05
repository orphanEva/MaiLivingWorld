"""Bond catalog, schema and optional-ledger migration integration."""
import importlib
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from MaiLivingWorld.bond_store import BondStore
from MaiLivingWorld.mysql_compat import TABLES
from MaiLivingWorld.scripts import backup_state as backup
from MaiLivingWorld.scripts import migrate_all_sqlite_to_mysql as migration

ROOT = Path(__file__).resolve().parents[1]
BOND_TABLES = ("bond_profiles", "bond_moments", "bond_audit")
OWNER = "12345678"


def seed_legacy(directory):
    for module, name, filename in (
        ("store", "Store", "living_world"), ("brain_core", "BrainBook", "brain_book"),
        ("mind", "MindStore", "mind"), ("cognition", "CognitiveStore", "cognition"),
        ("experience_loop", "ExperienceLoop", "experience_loop"),
        ("proactive_feedback", "ProactiveFeedback", "proactive_feedback"),
        ("self_state", "SelfState", "self_state"), ("growth_store", "GrowthStore", "growth"),
        ("tool_store", "ToolStore", "tool_memory"), ("companion_store", "CompanionStore", "companion"),
    ):
        store = getattr(importlib.import_module("MaiLivingWorld."+module), name)(directory/(filename+".sqlite3"))
        store.close()


class BondMigrationTests(unittest.TestCase):
    def test_fixed_catalog_schema_and_migration_inventories_match(self):
        self.assertEqual(set(migration.LEDGERS), set(backup.LEDGERS))
        self.assertEqual(migration.LEDGERS["bond.sqlite3"], BOND_TABLES)
        self.assertEqual(set(migration.ALL_TABLES), set(backup.TABLES))
        self.assertEqual(set(TABLES) | {"stories", "attempts", "story_attempts"}, set(backup.TABLES))
        self.assertEqual(len(backup.TABLES), 72)
        self.assertIn("bond.sqlite3", migration.OPTIONAL_LEDGERS)

    def test_old_ten_library_migration_does_not_create_bond_or_new_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            seed_legacy(directory)
            rows, missing = migration.snapshot(directory)
            self.assertEqual(missing, [])
            self.assertFalse((directory/"bond.sqlite3").exists())
            self.assertEqual(rows["self_identity"], [])
            for table in BOND_TABLES:
                self.assertEqual(rows[table], [])

    def test_present_bond_rows_and_audit_enter_migration_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            seed_legacy(directory)
            store = BondStore(directory/"bond.sqlite3")
            try:
                self.assertEqual(store.profile(OWNER)["revision"], 0)
                self.assertEqual(store.con.execute("SELECT COUNT(*) FROM bond_profiles").fetchone()[0], 0)
                message = {"platform": "qq", "session_id": "owner-private-stream", "message_id": "bond-migration-1",
                    "message_info": {"user_info": {"user_id": OWNER}, "group_info": None},
                    "processed_plain_text": "相处称呼：小伙伴"}
                profile = store.command(message, owner_id=OWNER, action="set_address", value="小伙伴", now=100)
                moment_message = dict(message, message_id="bond-migration-2", processed_plain_text="记下我们开始一起读书")
                moment = store.confirm_moment(moment_message, owner_id=OWNER, summary="开始一起读书", now=101)
            finally:
                store.close()
            rows, missing = migration.snapshot(directory)
            self.assertEqual(missing, [])
            self.assertEqual(len(rows["bond_profiles"]), 1)
            self.assertEqual(json.loads(rows["bond_profiles"][0]["payload"])["preferred_address"], profile["preferred_address"])
            self.assertEqual(rows["bond_moments"][0]["id"], moment["id"])
            self.assertEqual(len(rows["bond_audit"]), 2)

    def test_safe_schema_initializer_includes_empty_bond_store(self):
        calls = []
        class EmptyStore:
            def __init__(self, *args, **kwargs):
                calls.append((args, kwargs))
            def close(self):
                pass
        names = ("MySQLStore", "BrainBook", "MindStore", "CognitiveStore", "ExperienceLoop",
                 "ProactiveFeedback", "SelfState", "GrowthStore", "ToolStore", "CompanionStore", "BondStore", "ContinuityStore", "AttentionStore", "RecollectionStore", "HabitsStore", "FollowthroughStore", "ProjectsStore", "SharedReadingStore")
        with patch.multiple(migration, **{name: EmptyStore for name in names}), tempfile.TemporaryDirectory() as temp:
            migration.ensure_schema({"table_prefix": "test_"}, Path(temp))
        self.assertEqual(len(calls), 18)
        bond = next(call for call in calls if call[0] and call[0][0].name == "bond.sqlite3")
        self.assertEqual(bond[1]["mysql_settings"], {"table_prefix": "test_"})

    def test_bond_official_ddl_and_comment_upgrade_match_real_columns(self):
        ddl = (ROOT/"sql"/"mysql.sql").read_text(encoding="utf-8")
        upgrade = (ROOT/"sql"/"mysql_comments_existing.sql").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temp:
            store = BondStore(Path(temp)/"bond.sqlite3")
            try:
                for table in BOND_TABLES:
                    cols = {row[1] for row in store.con.execute("PRAGMA table_info("+table+")")}
                    create = re.search(r"CREATE TABLE IF NOT EXISTS `mlw_"+table+r"` \(([\s\S]*?)\) ENGINE=InnoDB[^;]*;", ddl)
                    alter = re.search(r"ALTER TABLE `mlw_"+table+r"`([\s\S]*?);", upgrade)
                    self.assertIsNotNone(create)
                    self.assertIsNotNone(alter)
                    self.assertEqual(set(re.findall(r"^\s+(\w+)\s+(?:VARCHAR|BIGINT|DOUBLE|LONGTEXT)\b", create[1], re.M)), cols)
                    self.assertEqual(set(re.findall(r"MODIFY COLUMN `(\w+)`", alter[1])), cols)
                    self.assertEqual(create[1].count(" COMMENT '"), len(cols))
                    self.assertIn("payload LONGTEXT", create[1])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
