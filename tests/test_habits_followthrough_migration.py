"""v0.19 ledgers preserve owner habits and real task/receipt state during migration."""
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from MaiLivingWorld.habits_store import HabitsStore
from MaiLivingWorld.followthrough_store import FollowthroughStore
from MaiLivingWorld.recollection_store import RecollectionStore
from MaiLivingWorld.mysql_compat import TABLES, MySQLCompatConnection, mysql_create_table
from MaiLivingWorld.scripts import backup_state as backup
from MaiLivingWorld.scripts import migrate_all_sqlite_to_mysql as migration
from test_recollection_migration import seed_previous
from test_backup_state import seed_habits, seed_followthrough


ROOT = Path(__file__).resolve().parents[1]
NEW_LEDGERS = {
    "habits.sqlite3": (HabitsStore, ("habits_control", "habits_inputs", "habits_preferences", "habits_audit"), seed_habits),
    "followthrough.sqlite3": (FollowthroughStore,
        ("followthrough_control", "followthrough_items", "followthrough_receipts", "followthrough_sources"), seed_followthrough),
}


def seed_v18(directory):
    seed_previous(directory)
    store = RecollectionStore(directory/"recollection.sqlite3")
    store.close()


class HabitsFollowthroughMigrationTests(unittest.TestCase):
    def test_current_schema_migration_and_backup_inventories_agree(self):
        self.assertEqual(len(backup.LEDGERS), 16)
        self.assertEqual(len(backup.TABLES), 66)
        self.assertEqual(set(migration.LEDGERS), set(backup.LEDGERS))
        self.assertEqual(set(migration.ALL_TABLES), set(backup.TABLES))
        self.assertEqual(set(TABLES) | {"stories", "attempts", "story_attempts"}, set(backup.TABLES))
        for filename, (_, tables, _) in NEW_LEDGERS.items():
            self.assertEqual(migration.LEDGERS[filename], tables)
            self.assertEqual(backup.LEDGERS[filename], tables)
            self.assertIn(filename, migration.OPTIONAL_LEDGERS)
            self.assertNotIn(filename, backup.V18_LEDGERS)

    def test_old_v18_migration_does_not_invent_new_preferences_or_commitments(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            seed_v18(directory)
            before = {path.name: path.read_bytes() for path in directory.glob("*.sqlite3")}
            rows, missing = migration.snapshot(directory)
            self.assertEqual(missing, [])
            self.assertEqual(rows["self_identity"], [])
            for filename, (_, tables, _) in NEW_LEDGERS.items():
                self.assertFalse((directory/filename).exists())
                self.assertTrue(all(rows[table] == [] for table in tables))
            # SQLite may create empty WAL coordination sidecars on a read-only open.
            self.assertEqual({path.name: path.read_bytes() for path in directory.glob("*.sqlite3")}, before)

    def test_real_new_rows_and_receipts_enter_readonly_migration_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            seed_v18(directory)
            expected = {}
            for filename, (cls, tables, seed) in NEW_LEDGERS.items():
                store = cls(directory/filename)
                try:
                    for table in tables:
                        self.assertEqual(store.con.execute("SELECT COUNT(*) FROM "+table).fetchone()[0], 0)
                    expected.update(seed(store))
                finally:
                    store.close()
            rows, missing = migration.snapshot(directory)
            self.assertEqual(missing, [])
            for table, original in expected.items():
                self.assertTrue(original, table)
                self.assertEqual(migration.signature(rows[table]), migration.signature(original), table)

    def test_incomplete_existing_ledgers_are_not_silently_treated_as_absent(self):
        for filename, (_, tables, _) in NEW_LEDGERS.items():
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                seed_v18(directory)
                con = sqlite3.connect(directory/filename)
                try:
                    con.execute("CREATE TABLE "+tables[0]+" (owner_id TEXT PRIMARY KEY)")
                    con.commit()
                finally:
                    con.close()
                with self.assertRaisesRegex(RuntimeError, re.escape(filename)+".*"+tables[1]):
                    migration.snapshot(directory)

    def test_schema_initializer_builds_all_sixteen_empty_ledgers(self):
        calls = []
        class EmptyStore:
            def __init__(self, *args, **kwargs):
                calls.append((args, kwargs))
            def close(self):
                pass
        names = ("MySQLStore", "BrainBook", "MindStore", "CognitiveStore", "ExperienceLoop", "ProactiveFeedback",
                 "SelfState", "GrowthStore", "ToolStore", "CompanionStore", "BondStore", "ContinuityStore",
                 "AttentionStore", "RecollectionStore", "HabitsStore", "FollowthroughStore")
        with patch.multiple(migration, **{name: EmptyStore for name in names}), tempfile.TemporaryDirectory() as temp:
            migration.ensure_schema({"table_prefix": "test_"}, Path(temp))
        self.assertEqual(len(calls), 16)
        for filename in NEW_LEDGERS:
            call = next(call for call in calls if call[0] and call[0][0].name == filename)
            self.assertEqual(call[1]["mysql_settings"], {"table_prefix": "test_"})

    def test_official_ddl_comments_types_defaults_and_indexes_match_constructors(self):
        ddl = (ROOT/"sql/mysql.sql").read_text(encoding="utf-8")
        upgrade = (ROOT/"sql/mysql_comments_existing.sql").read_text(encoding="utf-8")
        adapter = object.__new__(MySQLCompatConnection)
        adapter.prefix = "test_"
        with tempfile.TemporaryDirectory() as temp:
            for filename, (cls, tables, _) in NEW_LEDGERS.items():
                store = cls(Path(temp)/filename)
                try:
                    for table in tables:
                        columns = {row[1]: row for row in store.con.execute("PRAGMA table_info("+table+")")}
                        create = re.search(r"CREATE TABLE IF NOT EXISTS `mlw_"+table+r"` \(([\s\S]*?)\) ENGINE=InnoDB[^;]*;", ddl)
                        alter = re.search(r"ALTER TABLE `mlw_"+table+r"`([\s\S]*?);", upgrade)
                        self.assertIsNotNone(create, table)
                        self.assertIsNotNone(alter, table)
                        self.assertEqual(set(re.findall(r"^\s+(\w+)\s+(?:VARCHAR|BIGINT|DOUBLE|LONGTEXT)\b", create[1], re.M)), set(columns))
                        self.assertEqual(set(re.findall(r"MODIFY COLUMN `(\w+)`", alter[1])), set(columns))
                        self.assertEqual(create[1].count(" COMMENT '"), len(columns))
                        for name, row in columns.items():
                            expected = {"TEXT": "LONGTEXT" if name == "payload" else "VARCHAR(191)", "REAL": "DOUBLE", "INTEGER": "BIGINT"}[row[2]]
                            self.assertIn(name+" "+expected, create[1])
                            if row[4] is not None:
                                self.assertRegex(create[1], r"\b"+name+r"\s+[^\n]*\bDEFAULT "+re.escape(row[4])+r" COMMENT")
                        sqlite_ddl = store.con.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
                        if "payload" in columns:
                            self.assertIn("payload LONGTEXT", mysql_create_table(sqlite_ddl))
                        self.assertIn("test_"+table, adapter._sql("SELECT * FROM "+table+" WHERE owner_id=?"))
                        for index in store.con.execute("PRAGMA index_list("+table+")"):
                            if not index[1].startswith("idx_"):
                                continue
                            names = [row[2] for row in store.con.execute("PRAGMA index_info("+index[1]+")")]
                            kind = "UNIQUE KEY" if index[2] else "KEY"
                            self.assertIn(kind+" `"+index[1]+"` ("+",".join(names)+")", create[1])
                finally:
                    store.close()


if __name__ == "__main__":
    unittest.main()
