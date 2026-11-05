#!/usr/bin/env python3
"""Verified state archives. Dry-run by default; no credentials or SQL in archives.

All actual operations require writers to be stopped. MySQL restore additionally
requires an empty, already initialized, matching plugin schema. This program
never executes DDL taken from a backup or overwrites existing destination data.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from types import MappingProxyType
import zipfile

LEGACY_FORMAT = "mailivingworld.state-backup.v1"
FORMAT = "mailivingworld.state-backup.v2"
V14_LEDGERS = MappingProxyType({
    "living_world.sqlite3": ("stories", "attempts", "story_attempts"),
    "brain_book.sqlite3": ("brain_runs", "brain_events", "brain_event_evidence", "brain_facts", "brain_fact_evidence", "brain_goals", "brain_goal_events", "brain_decisions", "brain_cycles", "brain_fact_revisions"),
    "mind.sqlite3": ("mind_runs", "mind_notes", "mind_evidence", "mind_reviews", "mind_snapshots"),
    "cognition.sqlite3": ("cognitive_cycles", "cognitive_sources", "cognitive_goals", "cognitive_evidence", "cognitive_events"),
    "experience_loop.sqlite3": ("experience_actions", "experience_evidence", "experience_policy", "experience_opt_out"),
    "proactive_feedback.sqlite3": ("proactive_targets", "proactive_events"),
    "self_state.sqlite3": ("self_identity", "self_owner_history"),
    "growth.sqlite3": ("growth_state", "growth_jobs", "growth_history", "growth_consumed"),
    "tool_memory.sqlite3": ("tool_observations", "tool_calls", "tool_tasks", "tool_research"),
    "companion.sqlite3": ("companion_control", "companion_readings", "companion_units", "companion_plans", "companion_audit"),
})
V15_LEDGERS = MappingProxyType({**V14_LEDGERS,
    "bond.sqlite3": ("bond_profiles", "bond_moments", "bond_audit"),
})
V16_LEDGERS = MappingProxyType({**V15_LEDGERS,
    "continuity.sqlite3": ("continuity_control", "continuity_inputs", "continuity_threads", "continuity_audit"),
})
V17_LEDGERS = MappingProxyType({**V16_LEDGERS,
    "attention.sqlite3": ("attention_control", "attention_receipts", "attention_events"),
})
V18_LEDGERS = MappingProxyType({**V17_LEDGERS,
    "recollection.sqlite3": ("recollection_control", "recollection_inputs", "recollection_revisions", "recollection_deliveries"),
})
V19_LEDGERS = MappingProxyType({**V18_LEDGERS,
    "habits.sqlite3": ("habits_control", "habits_inputs", "habits_preferences", "habits_audit"),
    "followthrough.sqlite3": ("followthrough_control", "followthrough_items", "followthrough_receipts", "followthrough_sources"),
})
V20_LEDGERS = MappingProxyType({**V19_LEDGERS,
    "projects.sqlite3": ("projects_control", "projects_items", "projects_audit"),
    "shared_reading.sqlite3": ("shared_reading_control", "shared_reading_sessions", "shared_reading_receipts"),
})
V21_LEDGERS = MappingProxyType({**V20_LEDGERS,
    "advice.sqlite3": ("advice_control", "advice_items", "advice_inputs", "advice_receipts"),
    "reference.sqlite3": ("reference_control", "reference_receipts"),
})
# v0.22 stores opinion revisions in existing growth JSON and history rows.
V22_LEDGERS = MappingProxyType(dict(V21_LEDGERS))
# v0.23 keeps activity context in the existing attention JSON payload.
V23_LEDGERS = MappingProxyType(dict(V22_LEDGERS))
CATALOGS = MappingProxyType({"v0.14": V14_LEDGERS, "v0.15": V15_LEDGERS, "v0.16": V16_LEDGERS,
                           "v0.17": V17_LEDGERS, "v0.18": V18_LEDGERS, "v0.19": V19_LEDGERS,
                           "v0.20": V20_LEDGERS, "v0.21": V21_LEDGERS, "v0.22": V22_LEDGERS,
                           "v0.23": V23_LEDGERS})
CURRENT_CATALOG = "v0.23"
LEDGERS = CATALOGS[CURRENT_CATALOG]
TABLES = tuple(table for tables in LEDGERS.values() for table in tables)
MAX_MANIFEST = 1024 * 1024
MAX_FILE = 2 * 1024**3
MAX_TOTAL = 4 * 1024**3
MAX_ROW = 16 * 1024**2
CONFIRM = "RESTORE_EMPTY_TARGET"
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")


class BackupError(ValueError):
    """A fixed, non-sensitive error code, safe to display at the CLI."""


def _fail(code):
    raise BackupError(code)


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, OverflowError):
        _fail("invalid_backup_value")


def _loads(data):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                _fail("duplicate_json_key")
            value[key] = item
        return value
    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=lambda _: _fail("non_finite_value"))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _fail("invalid_backup_json")


def _ident(value):
    if not isinstance(value, str) or not _IDENT.fullmatch(value):
        _fail("invalid_identifier")
    return "`" + value + "`"


def _is_link(path):
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _directory(path, *, empty=False):
    path = Path(path).absolute()
    if any(_is_link(item) for item in (path, *path.parents)):
        _fail("linked_directory_rejected")
    if empty:
        if path.exists() and (not path.is_dir() or next(path.iterdir(), None) is not None):
            _fail("target_not_empty")
        if not path.parent.is_dir():
            _fail("target_parent_missing")
    elif not path.is_dir():
        _fail("source_directory_missing")
    return path


def _write_guard(apply, writers_stopped, *, restore=False, confirm=None):
    if apply and not writers_stopped:
        _fail("stop_writers_required")
    if apply and restore and confirm != CONFIRM:
        _fail("restore_confirmation_required")


def _hash(path):
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(block)
            if size > MAX_FILE:
                _fail("backup_file_too_large")
            digest.update(block)
    return size, digest.hexdigest()


def _manifest(backend, files, missing=()):
    manifest = {"format": FORMAT, "catalog": CURRENT_CATALOG, "backend": backend,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "consistency": "writers_stopped", "files": files, "missing": list(missing)}
    return _validate_manifest(manifest)


def _publish(archive, directory, manifest):
    archive = Path(archive).absolute()
    if any(_is_link(item) for item in (archive, *archive.parents)) or archive.exists():
        _fail("archive_target_exists_or_linked")
    # Exclusive creation prevents accidentally replacing an existing private backup.
    created = False
    try:
        with archive.open("xb") as target:
            created = True
            if os.name != "nt":
                os.chmod(archive, 0o600)
            with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as package:
                package.writestr("manifest.json", _json(manifest))
                for entry in manifest["files"]:
                    package.write(Path(directory)/entry["path"], entry["path"])
            target.flush()
            os.fsync(target.fileno())
    except Exception:
        if created:
            archive.unlink(missing_ok=True)
        raise


@contextmanager
def _sqlite(path):
    con = sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro", uri=True)
    try:
        con.execute("PRAGMA query_only=ON")
        con.execute("PRAGMA trusted_schema=OFF")
        yield con
    finally:
        con.close()


def _sqlite_info(path, ledger):
    try:
        with _sqlite(path) as con:
            if con.execute("PRAGMA quick_check").fetchone() != ("ok",):
                _fail("sqlite_integrity_failed")
            tables = {}
            for kind, name in con.execute("SELECT type,name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"):
                if kind == "index":
                    continue
                if kind != "table" or name not in LEDGERS[ledger]:
                    _fail("unexpected_sqlite_schema")
                tables[name] = con.execute("SELECT COUNT(*) FROM "+_ident(name)).fetchone()[0]
            return tables
    except sqlite3.Error:
        _fail("sqlite_read_failed")


def backup_sqlite(source, archive, *, apply=False, writers_stopped=False):
    _write_guard(apply, writers_stopped)
    source = _directory(source)
    found, missing, summary = [], [], {}
    for name in LEDGERS:
        path = source/name
        if _is_link(path):
            _fail("linked_database_rejected")
        if path.exists():
            if not path.is_file():
                _fail("invalid_database_file")
            found.append(name)
            summary[name] = _sqlite_info(path, name)
        else:
            missing.append(name)
    if not found:
        _fail("no_state_databases")
    result = {"backend": "sqlite", "dry_run": not apply, "ledgers": summary, "missing": missing}
    if not apply:
        return result
    with tempfile.TemporaryDirectory(prefix="mlw-backup-") as temp:
        directory = Path(temp)
        (directory/"sqlite").mkdir()
        files = []
        for name in found:
            path = directory/"sqlite"/name
            try:
                with _sqlite(source/name) as origin:
                    destination = sqlite3.connect(path)
                    try:
                        origin.backup(destination)
                    finally:
                        destination.close()
            except sqlite3.Error:
                _fail("sqlite_snapshot_failed")
            size, digest = _hash(path)
            files.append({"path": "sqlite/"+name, "size": size, "sha256": digest,
                          "tables": _sqlite_info(path, name)})
        if sum(item["size"] for item in files) > MAX_TOTAL:
            _fail("backup_too_large")
        _publish(archive, directory, _manifest("sqlite", files, missing))
    return result


def _catalog_name(manifest):
    # The old format predates explicit catalogs and always meant these ten
    # ledgers. Never reinterpret its omissions against an evolving current list.
    if manifest.get("format") == LEGACY_FORMAT:
        return "v0.14"
    name = manifest.get("catalog")
    if manifest.get("format") != FORMAT or not isinstance(name, str) or name not in CATALOGS:
        _fail("unsupported_backup_catalog")
    return name


def _current_missing(manifest):
    missing = list(manifest["missing"])
    if manifest["backend"] == "sqlite":
        catalog = CATALOGS[_catalog_name(manifest)]
        missing.extend(name for name in LEDGERS if name not in catalog)
    return missing


def _validate_manifest(manifest):
    if not isinstance(manifest, dict):
        _fail("invalid_manifest")
    fields = {"format", "backend", "created_at", "consistency", "files", "missing"}
    if manifest.get("format") == FORMAT:
        fields.add("catalog")
    if set(manifest) != fields:
        _fail("invalid_manifest")
    if manifest["format"] not in (LEGACY_FORMAT, FORMAT) or manifest["backend"] not in ("sqlite", "mysql") or manifest["consistency"] != "writers_stopped":
        _fail("unsupported_backup_format")
    catalog = CATALOGS[_catalog_name(manifest)]
    catalog_tables = tuple(table for group in catalog.values() for table in group)
    if not isinstance(manifest["created_at"], str) or len(manifest["created_at"]) > 64:
        _fail("invalid_manifest")
    files, missing = manifest["files"], manifest["missing"]
    if not isinstance(files, list) or not 1 <= len(files) <= len(catalog_tables) or not isinstance(missing, list):
        _fail("invalid_manifest")
    allowed = {"sqlite/"+name for name in catalog} if manifest["backend"] == "sqlite" else {"mysql/"+name+".jsonl" for name in catalog_tables}
    seen = set()
    total = 0
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or item["path"] not in allowed or item["path"] in seen:
            _fail("invalid_archive_path")
        seen.add(item["path"])
        if type(item.get("size")) is not int or not 0 <= item["size"] <= MAX_FILE or not isinstance(item.get("sha256"), str) or not _DIGEST.fullmatch(item["sha256"]):
            _fail("invalid_file_metadata")
        total += item["size"]
        if manifest["backend"] == "sqlite":
            tables = item.get("tables")
            if set(item) != {"path", "size", "sha256", "tables"} or not isinstance(tables, dict):
                _fail("invalid_sqlite_metadata")
            for table, count in tables.items():
                if table not in catalog[Path(item["path"]).name] or type(count) is not int or count < 0:
                    _fail("invalid_sqlite_metadata")
        else:
            if set(item) != {"path", "size", "sha256", "schema", "rows", "definition", "auto_increment"} or type(item.get("rows")) is not int or item["rows"] < 0:
                _fail("invalid_mysql_metadata")
            _validate_schema(item.get("schema"))
            if not isinstance(item["definition"], str) or not _DIGEST.fullmatch(item["definition"]):
                _fail("invalid_mysql_metadata")
            if item["auto_increment"] is not None and (type(item["auto_increment"]) is not int or not 1 <= item["auto_increment"] <= 2**64-1):
                _fail("invalid_mysql_metadata")
            _check_increment(item["schema"], item["auto_increment"])
    if total > MAX_TOTAL:
        _fail("backup_too_large")
    if manifest["backend"] == "sqlite":
        if any(not isinstance(name, str) or name not in catalog for name in missing) or len(set(missing)) != len(missing):
            _fail("invalid_missing_ledgers")
        if {Path(name).name for name in seen} & set(missing) or {Path(name).name for name in seen} | set(missing) != set(catalog):
            _fail("incomplete_ledger_inventory")
    elif missing:
        _fail("invalid_missing_tables")
    return manifest


@contextmanager
def _verified(archive):
    """Extract fixed filenames only, after checking every archive entry and hash."""
    try:
        with zipfile.ZipFile(archive) as package, tempfile.TemporaryDirectory(prefix="mlw-verify-") as temp:
            entries = package.infolist()
            names = [item.filename for item in entries]
            if not 2 <= len(entries) <= len(TABLES)+1 or len(set(names)) != len(names) or "manifest.json" not in names:
                _fail("invalid_archive_inventory")
            for info in entries:
                mode = info.external_attr >> 16
                if info.flag_bits & 1 or info.is_dir() or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
                    _fail("unsupported_archive_entry")
            info = package.getinfo("manifest.json")
            if info.file_size > MAX_MANIFEST:
                _fail("manifest_too_large")
            manifest = _validate_manifest(_loads(package.read(info)))
            if set(names) != {"manifest.json", *(item["path"] for item in manifest["files"])}:
                _fail("unexpected_archive_entry")
            directory = Path(temp)
            for item in manifest["files"]:
                info = package.getinfo(item["path"])
                if info.file_size != item["size"]:
                    _fail("archive_size_mismatch")
                target = directory/item["path"]
                target.parent.mkdir(exist_ok=True)
                digest, size = hashlib.sha256(), 0
                with package.open(info) as origin, target.open("xb") as destination:
                    for block in iter(lambda: origin.read(1024*1024), b""):
                        size += len(block)
                        if size > item["size"]:
                            _fail("archive_size_mismatch")
                        digest.update(block)
                        destination.write(block)
                if size != item["size"] or digest.hexdigest() != item["sha256"]:
                    _fail("archive_hash_mismatch")
                if manifest["backend"] == "sqlite":
                    if _sqlite_info(target, target.name) != item["tables"]:
                        _fail("sqlite_metadata_mismatch")
                else:
                    count = sum(1 for _ in _read_rows(target, item["schema"]))
                    if count != item["rows"]:
                        _fail("mysql_row_count_mismatch")
            yield manifest, directory
    except BackupError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError, EOFError):
        _fail("archive_read_failed")


def verify_archive(archive):
    with _verified(archive) as (manifest, _):
        return {"backend": manifest["backend"], "catalog": _catalog_name(manifest), "verified": True,
                "files": len(manifest["files"]), "missing": _current_missing(manifest)}


def restore_sqlite(archive, target, *, apply=False, writers_stopped=False, confirm=None):
    _write_guard(apply, writers_stopped, restore=True, confirm=confirm)
    target = _directory(target, empty=True)
    with _verified(archive) as (manifest, directory):
        if manifest["backend"] != "sqlite":
            _fail("backend_mismatch")
        result = {"backend": "sqlite", "catalog": _catalog_name(manifest), "dry_run": not apply,
                  "files": len(manifest["files"]), "missing": _current_missing(manifest)}
        if not apply:
            return result
        with tempfile.TemporaryDirectory(prefix="mlw-restore-", dir=target.parent) as temp:
            staged = Path(temp)/"state"
            staged.mkdir(mode=0o700)
            for item in manifest["files"]:
                destination = staged/Path(item["path"]).name
                with (directory/item["path"]).open("rb") as origin, destination.open("xb") as output:
                    for block in iter(lambda: origin.read(1024*1024), b""):
                        output.write(block)
                if os.name != "nt":
                    os.chmod(destination, 0o600)
            # Nonempty concurrent destinations make rmdir/rename fail safely.
            _directory(target, empty=True)
            if target.exists():
                target.rmdir()
            staged.rename(target)
        return result


def _encode(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            _fail("non_finite_value")
        return value
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, Decimal):
        if not value.is_finite():
            _fail("non_finite_value")
        return {"$decimal": str(value)}
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, time):
        return {"$time": value.isoformat()}
    if isinstance(value, timedelta):
        return {"$timedelta": [value.days, value.seconds, value.microseconds]}
    _fail("unsupported_mysql_value")


def _decode(value):
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if not isinstance(value, dict) or len(value) != 1:
        _fail("invalid_mysql_value")
    kind, data = next(iter(value.items()))
    try:
        if kind == "$timedelta" and isinstance(data, list) and len(data) == 3 and all(type(n) is int for n in data):
            return timedelta(days=data[0], seconds=data[1], microseconds=data[2])
        if not isinstance(data, str):
            _fail("invalid_mysql_value")
        if kind == "$bytes":
            return base64.b64decode(data, validate=True)
        if kind == "$decimal":
            number = Decimal(data)
            if number.is_finite():
                return number
        if kind == "$datetime":
            return datetime.fromisoformat(data)
        if kind == "$date":
            return date.fromisoformat(data)
        if kind == "$time":
            return time.fromisoformat(data)
    except (ValueError, OverflowError, TypeError, ArithmeticError):
        _fail("invalid_mysql_value")
    _fail("invalid_mysql_value")


SCHEMA_KEYS = ("Field", "Type", "Null", "Key", "Default", "Extra", "Collation")


def _validate_schema(schema):
    if not isinstance(schema, list) or not 1 <= len(schema) <= 256:
        _fail("invalid_mysql_schema")
    names = set()
    for item in schema:
        if not isinstance(item, dict) or set(item) != set(SCHEMA_KEYS):
            _fail("invalid_mysql_schema")
        _ident(item["Field"])
        if item["Field"] in names or any(value is not None and (not isinstance(value, str) or len(value) > 8192) for value in item.values()):
            _fail("invalid_mysql_schema")
        names.add(item["Field"])
    if not any(item["Key"] == "PRI" for item in schema):
        _fail("mysql_primary_key_required")
    return schema


def _read_rows(path, schema):
    with Path(path).open("rb") as stream:
        while True:
            line = stream.readline(MAX_ROW+1)
            if not line:
                break
            if len(line) > MAX_ROW or not line.endswith(b"\n"):
                _fail("invalid_mysql_row_size")
            row = _loads(line)
            if not isinstance(row, list) or len(row) != len(schema):
                _fail("invalid_mysql_row")
            decoded = [_decode(value) for value in row]
            if _json([_encode(value) for value in decoded])+b"\n" != line:
                _fail("noncanonical_mysql_row")
            yield decoded


def mysql_config(env=None):
    env = os.environ if env is None else env
    try:
        port = int(env.get("MLW_MYSQL_PORT", "3306"))
        if not 1 <= port <= 65535:
            _fail("invalid_mysql_configuration")
        config = {"host": env.get("MLW_MYSQL_HOST", "127.0.0.1"), "port": port,
                  "user": env.get("MLW_MYSQL_USER", ""), "password": env.get("MLW_MYSQL_PASSWORD", ""),
                  "database": env.get("MLW_MYSQL_DATABASE", ""), "prefix": env.get("MLW_MYSQL_PREFIX", "mlw_")}
        if not config["user"] or not config["database"]:
            _fail("invalid_mysql_configuration")
        _ident(config["database"])
        for name in TABLES:
            _ident(config["prefix"]+name)
        return config
    except (ValueError, TypeError):
        _fail("invalid_mysql_configuration")


def _connect(config):
    try:
        import pymysql
        return pymysql.connect(**{key: config[key] for key in ("host", "port", "user", "password", "database")},
            charset="utf8mb4", autocommit=False, cursorclass=pymysql.cursors.SSDictCursor,
            connect_timeout=10, read_timeout=60, write_timeout=60)
    except Exception:
        _fail("mysql_connection_failed")


def _inventory(cursor, prefix):
    # MySQL 8 caches SHOW TABLE STATUS statistics (including Auto_increment).
    # A newly opened table or ALTER AUTO_INCREMENT can otherwise produce stale
    # NULL/old counters. Read the engine on every inventory, without ANALYZE or
    # any persistent source mutation; deleted-row high-water marks must survive.
    cursor.execute("SET SESSION information_schema_stats_expiry = 0")
    cursor.execute("SHOW TRIGGERS")
    if cursor.fetchone() is not None:
        _fail("mysql_triggers_not_supported")
    cursor.execute("SHOW TABLE STATUS")
    tables = {}
    allowed = {prefix+name: name for name in TABLES}
    for row in cursor.fetchall():
        name = row.get("Name")
        if name not in allowed or row.get("Engine", "").lower() != "innodb":
            _fail("mysql_dedicated_innodb_schema_required")
        tables[allowed[name]] = name
    if not tables:
        _fail("mysql_initialized_schema_required")
    return tables


def _schema(cursor, name):
    cursor.execute("SHOW FULL COLUMNS FROM "+_ident(name))
    return _validate_schema([{key: row.get(key) if row.get(key) is None else str(row[key]) for key in SCHEMA_KEYS} for row in cursor.fetchall()])


def _check_increment(schema, value):
    auto_columns = [item for item in schema if "auto_increment" in (item["Extra"] or "").lower().split()]
    # NULL means no AUTO_INCREMENT column, never an implicit starting value.
    # Fail closed if the engine cannot supply a real counter for such a column.
    if len(auto_columns) > 1 or bool(auto_columns) != (value is not None):
        _fail("mysql_auto_increment_mismatch")


def _opened_increment(cursor, name):
    """Call only after SHOW CREATE has opened/initialized the InnoDB table.

    MySQL 8.4's table-statistics hook can peek an uninitialized zero even with
    statistics caching disabled. SHOW CREATE invokes info(HA_STATUS_AUTO), which
    initializes the native counter from persisted metadata before this re-read.
    Never derive a deleted-row high-water mark from NULL or MAX(id).
    """
    cursor.execute("SHOW TABLE STATUS WHERE Name = %s", (name,))
    row = cursor.fetchone()
    if not isinstance(row, dict) or row.get("Name") != name or str(row.get("Engine", "")).lower() != "innodb":
        _fail("mysql_schema_changed")
    value = row.get("Auto_increment")
    if value is not None and (type(value) is not int or not 1 <= value <= 2**64-1):
        _fail("invalid_mysql_auto_increment")
    return value


def _definition(cursor, name, prefix):
    """Compare indexes, constraints, engine and collation, without storing DDL."""
    cursor.execute("SHOW CREATE TABLE "+_ident(name))
    row = cursor.fetchone()
    sql = row.get("Create Table") if isinstance(row, dict) else None
    if not isinstance(sql, str):
        _fail("invalid_mysql_definition")
    sql = re.sub(r"\bAUTO_INCREMENT=\d+\s*", "", sql)
    # Prefix differences on tables are harmless; other structural differences fail.
    for table in TABLES:
        sql = sql.replace(_ident(prefix+table), _ident(table))
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def _select(cursor, name, schema, *, lock=False, probe=False):
    columns = ",".join(_ident(item["Field"]) for item in schema)
    keys = ",".join(_ident(item["Field"]) for item in schema if item["Key"] == "PRI")
    cursor.execute("SELECT "+columns+" FROM "+_ident(name)+" ORDER BY "+keys+(" LIMIT 1" if probe else "")+(" FOR UPDATE" if lock else ""))


def _dump_rows(cursor, schema, target=None):
    count, size, digest = 0, 0, hashlib.sha256()
    while True:
        rows = cursor.fetchmany(256)
        if not rows:
            break
        for row in rows:
            data = _json([_encode(row[item["Field"]]) for item in schema])+b"\n"
            if len(data) > MAX_ROW:
                _fail("invalid_mysql_row_size")
            size += len(data)
            if size > MAX_FILE:
                _fail("backup_file_too_large")
            count += 1
            digest.update(data)
            if target is not None:
                target.write(data)
    return count, size, digest.hexdigest()


def backup_mysql(config, archive, *, apply=False, writers_stopped=False, connect=None):
    _write_guard(apply, writers_stopped)
    con = (connect or _connect)(config)
    try:
        cursor = con.cursor()
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
        tables = _inventory(cursor, config["prefix"])
        files = []
        with tempfile.TemporaryDirectory(prefix="mlw-backup-") as temp:
            directory = Path(temp)
            (directory/"mysql").mkdir()
            for table in TABLES:
                if table not in tables:
                    continue
                schema = _schema(cursor, tables[table])
                definition = _definition(cursor, tables[table], config["prefix"])
                increment = _opened_increment(cursor, tables[table])
                _check_increment(schema, increment)
                _select(cursor, tables[table], schema)
                path = directory/"mysql"/(table+".jsonl")
                with path.open("xb") as target:
                    count, size, digest = _dump_rows(cursor, schema, target if apply else None)
                files.append({"path": "mysql/"+table+".jsonl", "schema": schema, "rows": count, "size": size, "sha256": digest,
                              "definition": definition, "auto_increment": increment})
            if sum(item["size"] for item in files) > MAX_TOTAL:
                _fail("backup_too_large")
            con.rollback()
            if apply:
                _publish(archive, directory, _manifest("mysql", files))
        return {"backend": "mysql", "dry_run": not apply, "tables": len(files), "rows": sum(item["rows"] for item in files)}
    except BackupError:
        con.rollback()
        raise
    except Exception:
        con.rollback()
        _fail("mysql_backup_failed")
    finally:
        con.close()


def restore_mysql(archive, config, *, apply=False, writers_stopped=False, confirm=None, connect=None):
    _write_guard(apply, writers_stopped, restore=True, confirm=confirm)
    with _verified(archive) as (manifest, directory):
        if manifest["backend"] != "mysql":
            _fail("backend_mismatch")
        con = (connect or _connect)(config)
        try:
            cursor = con.cursor()
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cursor.execute("START TRANSACTION")
            tables = _inventory(cursor, config["prefix"])
            increments = {}
            expected = {Path(item["path"]).stem: item for item in manifest["files"]}
            if set(tables) != set(expected):
                if _catalog_name(manifest) != CURRENT_CATALOG:
                    _fail("mysql_catalog_requires_matching_schema_then_upgrade")
                _fail("mysql_schema_inventory_mismatch")
            # Empty-table next-key locks prevent concurrent INSERTs under InnoDB RR.
            # Writers must still be stopped (including DDL) throughout restoration.
            for table in TABLES:
                if table not in tables:
                    continue
                item = expected[table]
                if _schema(cursor, tables[table]) != item["schema"]:
                    _fail("mysql_schema_mismatch")
                if _definition(cursor, tables[table], config["prefix"]) != item["definition"]:
                    _fail("mysql_definition_mismatch")
                increments[table] = _opened_increment(cursor, tables[table])
                _check_increment(item["schema"], increments[table])
                if (item["auto_increment"] is None) != (increments[table] is None):
                    _fail("mysql_auto_increment_mismatch")
                _select(cursor, tables[table], item["schema"], lock=apply, probe=True)
                if cursor.fetchone() is not None:
                    _fail("target_not_empty")
            if apply:
                # ALTER is a fixed local statement and implicitly commits. Only
                # increase counters on verified empty tables, then reacquire all
                # locks before importing. A failed import can leave higher empty
                # counters, but never partial restored rows or reused source IDs.
                con.rollback()
                for table, item in expected.items():
                    if item["auto_increment"] is not None:
                        value = max(item["auto_increment"], increments[table])
                        cursor.execute("ALTER TABLE "+_ident(tables[table])+" AUTO_INCREMENT = "+str(value))
                cursor.execute("START TRANSACTION")
                for table in TABLES:
                    if table in tables:
                        item = expected[table]
                        if _schema(cursor, tables[table]) != item["schema"] or _definition(cursor, tables[table], config["prefix"]) != item["definition"]:
                            _fail("mysql_schema_changed")
                        _select(cursor, tables[table], item["schema"], lock=True, probe=True)
                        if cursor.fetchone() is not None:
                            _fail("target_not_empty")
                for table in TABLES:
                    if table not in tables:
                        continue
                    item = expected[table]
                    sql = "INSERT INTO "+_ident(tables[table])+" ("+",".join(_ident(col["Field"]) for col in item["schema"])+") VALUES ("+",".join(["%s"]*len(item["schema"]))+")"
                    for row in _read_rows(directory/item["path"], item["schema"]):
                        cursor.execute(sql, row)
                for table, item in expected.items():
                    _select(cursor, tables[table], item["schema"])
                    if _dump_rows(cursor, item["schema"]) != (item["rows"], item["size"], item["sha256"]):
                        _fail("mysql_restore_verification_failed")
                con.commit()
            else:
                con.rollback()
            return {"backend": "mysql", "dry_run": not apply, "tables": len(expected), "rows": sum(item["rows"] for item in expected.values())}
        except BackupError:
            con.rollback()
            raise
        except Exception:
            con.rollback()
            _fail("mysql_restore_failed")
        finally:
            con.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="成长状态完整备份/恢复。默认 dry-run；实际操作须停写；恢复绝不覆盖已有数据。")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("backup-sqlite", "restore-sqlite", "backup-mysql", "restore-mysql", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--archive", required=True, type=Path)
        if name == "verify":
            continue
        command.add_argument("--apply", action="store_true")
        command.add_argument("--writers-stopped", action="store_true", help="确认所有插件进程/写入者与迁移已停止")
        if name == "backup-sqlite":
            command.add_argument("--source", required=True, type=Path)
        if name == "restore-sqlite":
            command.add_argument("--target", required=True, type=Path)
        if name.startswith("restore"):
            command.add_argument("--confirm", choices=[CONFIRM])
    args = parser.parse_args(argv)
    try:
        options = {"apply": getattr(args, "apply", False), "writers_stopped": getattr(args, "writers_stopped", False)}
        if args.command == "verify":
            result = verify_archive(args.archive)
        elif args.command == "backup-sqlite":
            result = backup_sqlite(args.source, args.archive, **options)
        elif args.command == "restore-sqlite":
            result = restore_sqlite(args.archive, args.target, confirm=args.confirm, **options)
        elif args.command == "backup-mysql":
            result = backup_mysql(mysql_config(), args.archive, **options)
        else:
            result = restore_mysql(args.archive, mysql_config(), confirm=args.confirm, **options)
        print(_json(result).decode("utf-8"))
        return 0
    except BackupError as exc:
        parser.exit(2, "备份/恢复失败："+str(exc)+"\n")
    except (OSError, ValueError, TypeError):
        parser.exit(2, "备份/恢复失败：state_operation_failed\n")


if __name__ == "__main__":
    raise SystemExit(main())
