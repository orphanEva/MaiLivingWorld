"""v0.21 migration preserves advice outcomes and cleared topic references."""
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from MaiLivingWorld.advice_store import AdviceStore
from MaiLivingWorld.reference_store import ReferenceStore
from MaiLivingWorld.projects_store import ProjectsStore
from MaiLivingWorld.shared_reading_store import SharedReadingStore
from MaiLivingWorld.mysql_compat import TABLES, MySQLCompatConnection, mysql_create_table
from MaiLivingWorld.scripts import backup_state as backup
from MaiLivingWorld.scripts import migrate_all_sqlite_to_mysql as migration
from test_projects_shared_reading_migration import seed_v19
from test_backup_state import seed_advice, seed_reference


ROOT = Path(__file__).resolve().parents[1]
NEW_LEDGERS = {
    "advice.sqlite3": (AdviceStore, ("advice_control", "advice_items", "advice_inputs", "advice_receipts"), seed_advice),
    "reference.sqlite3": (ReferenceStore, ("reference_control", "reference_receipts"), seed_reference),
}


def seed_v20(directory):
    seed_v19(directory)
    for name, cls in (("projects", ProjectsStore), ("shared_reading", SharedReadingStore)):
        store = cls(directory/(name+".sqlite3"))
        store.close()


class AdviceReferenceMigrationTests(unittest.TestCase):
    def test_current_schema_migration_and_archive_inventories_agree(self):
        self.assertEqual(len(backup.LEDGERS), 21)
        self.assertEqual(len(backup.TABLES), 82)
        self.assertEqual(set(migration.LEDGERS), set(backup.LEDGERS))
        self.assertEqual(set(migration.ALL_TABLES), set(backup.TABLES))
        self.assertEqual(set(TABLES) | {"stories", "attempts", "story_attempts"}, set(backup.TABLES))
        for filename, (_, tables, _) in NEW_LEDGERS.items():
            self.assertEqual(migration.LEDGERS[filename], tables)
            self.assertEqual(backup.LEDGERS[filename], tables)
            self.assertIn(filename, migration.OPTIONAL_LEDGERS)
            self.assertNotIn(filename, backup.V20_LEDGERS)

    def test_old_v20_snapshot_does_not_invent_advice_or_current_discussion(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            seed_v20(directory)
            before = {path.name: path.read_bytes() for path in directory.glob("*.sqlite3")}
            rows, missing = migration.snapshot(directory)
            self.assertEqual(missing, [])
            self.assertEqual(rows["self_identity"], [])
            for filename, (_, tables, _) in NEW_LEDGERS.items():
                self.assertFalse((directory/filename).exists())
                self.assertTrue(all(rows[table] == [] for table in tables))
            self.assertEqual({path.name: path.read_bytes() for path in directory.glob("*.sqlite3")}, before)

    def test_sent_outcome_forgotten_and_cleared_reference_rows_survive_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            seed_v20(directory)
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

    def test_incomplete_existing_new_ledgers_are_rejected(self):
        for filename, (_, tables, _) in NEW_LEDGERS.items():
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                seed_v20(directory)
                con = sqlite3.connect(directory/filename)
                try:
                    con.execute("CREATE TABLE "+tables[0]+" (owner_id TEXT PRIMARY KEY)")
                    con.commit()
                finally:
                    con.close()
                with self.assertRaisesRegex(RuntimeError, re.escape(filename)+".*"+tables[1]):
                    migration.snapshot(directory)

    def test_schema_initializer_uses_all_twenty_one_official_store_constructors(self):
        calls = []
        class EmptyStore:
            def __init__(self, *args, **kwargs):
                calls.append((args, kwargs))
            def close(self):
                pass
        names = ("MySQLStore", "BrainBook", "MindStore", "CognitiveStore", "ExperienceLoop", "ProactiveFeedback",
                 "SelfState", "GrowthStore", "ToolStore", "CompanionStore", "BondStore", "ContinuityStore",
                 "AttentionStore", "RecollectionStore", "HabitsStore", "FollowthroughStore", "ProjectsStore", "SharedReadingStore",
                 "AdviceStore", "ReferenceStore", "RepairStore")
        with patch.multiple(migration, **{name: EmptyStore for name in names}), tempfile.TemporaryDirectory() as temp:
            migration.ensure_schema({"table_prefix": "test_"}, Path(temp))
        self.assertEqual(len(calls), 21)
        for filename in NEW_LEDGERS:
            call = next(call for call in calls if call[0] and call[0][0].name == filename)
            self.assertEqual(call[1]["mysql_settings"], {"table_prefix": "test_"})

    def test_ddl_comments_columns_defaults_and_indexes_match_constructors(self):
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
                        primary = {name for name, column in columns.items() if column[5]}
                        for index in store.con.execute("PRAGMA index_list("+table+")"):
                            names = [row[2] for row in store.con.execute("PRAGMA index_info("+index[1]+")")]
                            if index[1].startswith("idx_"):
                                self.assertIn(("UNIQUE KEY" if index[2] else "KEY")+" `"+index[1]+"` ("+",".join(names)+")", create[1])
                            elif index[2] and set(names) != primary:
                                self.assertIn("UNIQUE ("+",".join(names)+")", create[1])
                finally:
                    store.close()


if __name__ == "__main__":
    unittest.main()
