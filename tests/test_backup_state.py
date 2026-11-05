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

    def test_inventory_ten_databases_44_tables(self):
        self.assertEqual(len(backup.LEDGERS), 10)
        self.assertEqual(len(backup.TABLES), 44)

    def test_full_offline_restore_drill_preserves_every_table(self):
        self.seed(all_ledgers=True)
        self.archive_now()
        self.assertEqual(backup.verify_archive(self.archive)["files"], 10)
        result = self.restore_now()
        self.assertEqual(result["files"], 10)
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

    def test_real_store_schema_identity_budget_growth_and_plan_survive_restore(self):
        classes = (("store", "Store", "living_world"), ("brain_core", "BrainBook", "brain_book"),
            ("mind", "MindStore", "mind"), ("cognition", "CognitiveStore", "cognition"),
            ("experience_loop", "ExperienceLoop", "experience_loop"), ("proactive_feedback", "ProactiveFeedback", "proactive_feedback"),
            ("self_state", "SelfState", "self_state"), ("growth_store", "GrowthStore", "growth"),
            ("tool_store", "ToolStore", "tool_memory"), ("companion_store", "CompanionStore", "companion"))
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
            finally:
                store.close()
        self.archive_now()
        self.restore_now()
        from MaiLivingWorld.self_state import SelfState
        from MaiLivingWorld.growth_store import GrowthStore
        from MaiLivingWorld.companion_store import CompanionStore
        stores = [SelfState(self.target/"self_state.sqlite3"), GrowthStore(self.target/"growth.sqlite3"), CompanionStore(self.target/"companion.sqlite3")]
        try:
            current = stores[0].state()
            self.assertEqual(current["agent_id"], identity["agent_id"])
            self.assertEqual(current["owner_id"], identity["owner_id"])
            self.assertEqual(current["thought_count"], 1)
            self.assertEqual(stores[1].state(now=100), growth)
            self.assertEqual(stores[2].plan(plan["id"], owner_id="12345678"), plan)
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
        elif sql == "SHOW TABLE STATUS":
            self.result = [{"Name": self.prefix+table, "Engine": "InnoDB", "Auto_increment": self.increments[table]} for table in self.rows]
        elif sql.startswith("SHOW FULL COLUMNS"):
            table = re.findall(r"`([^`]+)`", sql)[0][len(self.prefix):]
            self.result = copy.deepcopy(self.schemas[table])
        elif sql.startswith("SHOW CREATE TABLE"):
            table = re.findall(r"`([^`]+)`", sql)[0]
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

    def test_mysql_value_codec_is_exact_and_rejects_nonfinite(self):
        values = [None, True, 7, "中文", 1.25, b"\x00\xff", Decimal("1.2300"), datetime(2026, 1, 2, 3, 4), date(2026, 1, 2), time(3, 4), timedelta(days=-1, microseconds=17)]
        for value in values:
            self.assertEqual(backup._decode(backup._loads(backup._json(backup._encode(value)))), value)
        for value in (float("inf"), float("nan"), Decimal("NaN")):
            with self.assertRaises(backup.BackupError):
                backup._encode(value)

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
    def test_real_44_table_archive_empty_restore_and_counter_high_water(self):
        import pymysql
        from MaiLivingWorld.scripts.migrate_all_sqlite_to_mysql import ensure_schema
        from MaiLivingWorld.self_state import SelfState
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
                    with connection.cursor() as cursor:
                        cursor.execute("ALTER TABLE backup_attempts AUTO_INCREMENT = 900")
                    result = backup.backup_mysql(config, archive, apply=True, writers_stopped=True)
                    self.assertEqual(result["tables"], 44)
                    self.assertTrue(backup.verify_archive(archive)["verified"])
                    with self.assertRaisesRegex(backup.BackupError, "target_not_empty"):
                        backup.restore_mysql(archive, config, apply=True, writers_stopped=True, confirm=backup.CONFIRM)
                    # Every table here was created by this test in its dedicated empty DB.
                    with connection.cursor() as cursor:
                        for table in reversed(backup.TABLES):
                            cursor.execute("DROP TABLE `backup_"+table+"`")
                    ensure_schema(settings, directory)
                    restored = backup.restore_mysql(archive, config, apply=True, writers_stopped=True, confirm=backup.CONFIRM)
                    self.assertEqual(restored["tables"], 44)
                    state = SelfState(directory/"unused.sqlite3", mysql_settings=settings)
                    try:
                        self.assertEqual(state.state()["agent_id"], identity["agent_id"])
                        self.assertEqual(state.state()["thought_count"], 1)
                    finally:
                        state.close()
                    with connection.cursor() as cursor:
                        cursor.execute("INSERT INTO backup_attempts(target,created_at,kind) VALUES ('private:test',101,'test')")
                        self.assertGreaterEqual(cursor.lastrowid, 900)
                finally:
                    if created:
                        with connection.cursor() as cursor:
                            for table in reversed(backup.TABLES):
                                cursor.execute("DROP TABLE IF EXISTS `backup_"+table+"`")
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
