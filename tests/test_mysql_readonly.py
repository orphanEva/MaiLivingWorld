"""Independent committed reads never finish a ledger writer's transaction."""
from __future__ import annotations

import copy
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from MaiLivingWorld.mysql_compat import CompatRow, MySQLCompatConnection


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.closed = False

    def execute(self, query, parameters):
        self.connection.queries.append((query, parameters))
        if self.connection.query_error:
            raise self.connection.query_error
        return 1

    def fetchall(self):
        if self.connection.fetch_error:
            raise self.connection.fetch_error
        if self.connection.autocommit:
            return [dict(self.connection.engine)]
        if self.connection.snapshot is None:
            self.connection.snapshot = dict(self.connection.engine)
        return [dict(self.connection.snapshot)]

    def close(self):
        self.closed = True
        if self.connection.cursor_close_error:
            raise self.connection.cursor_close_error


class Connection:
    def __init__(self, engine, kwargs):
        self.engine = engine
        self.kwargs = kwargs
        self.autocommit = kwargs["autocommit"]
        self.snapshot = None
        self.pending = {}
        self.queries = []
        self.cursors = []
        self.query_error = None
        self.fetch_error = None
        self.cursor_close_error = None
        self.connection_close_error = None
        self.cursor_error = None
        self.closed = False
        self.begin = Mock(side_effect=self._begin)
        self.commit = Mock()
        self.rollback = Mock()
        self.close = Mock(side_effect=self._close)

    def _begin(self):
        self.snapshot = copy.deepcopy(self.engine)

    def _close(self):
        self.closed = True
        if self.connection_close_error:
            raise self.connection_close_error

    def cursor(self):
        if self.cursor_error:
            raise self.cursor_error
        cursor = Cursor(self)
        self.cursors.append(cursor)
        return cursor


class MySQLReadonlyTests(unittest.TestCase):
    def setUp(self):
        self.engine = {"id": "F1", "value": "initial"}
        self.connections = []

        def connect(**kwargs):
            connection = Connection(self.engine, kwargs)
            self.connections.append(connection)
            return connection

        self.connect = Mock(side_effect=connect)
        driver = types.SimpleNamespace(connect=self.connect, cursors=types.SimpleNamespace(DictCursor=object))
        self.driver_patch = patch.dict(sys.modules, {"pymysql": driver})
        self.driver_patch.start()
        self.db = MySQLCompatConnection(host="test-db", port=3306, user="test-user",
                                        password="synthetic-test-only", database="test-database",
                                        table_prefix="test_")
        self.writer = self.connections[0]

    def tearDown(self):
        for connection in self.connections:
            connection.connection_close_error = None
        self.db.close()
        self.driver_patch.stop()

    def read(self):
        return self.db.fetch_readonly("SELECT id,value FROM brain_facts WHERE id=?", ("F1",))

    def test_reader_is_lazy_independent_autocommit_and_reuses_connection(self):
        self.assertEqual(self.connect.call_count, 1)
        self.assertFalse(self.writer.autocommit)
        rows = self.read()
        self.assertEqual(self.connect.call_count, 2)
        reader = self.connections[1]
        self.assertTrue(reader.autocommit)
        self.assertIsNot(reader, self.writer)
        self.assertIsInstance(rows[0], CompatRow)
        self.assertEqual(rows[0]["value"], "initial")
        self.assertEqual(reader.queries[0], ("SELECT id,value FROM test_brain_facts WHERE id=%s", ("F1",)))
        self.assertTrue(reader.cursors[0].closed)
        self.read()
        self.assertEqual(self.connect.call_count, 2)
        self.assertTrue(reader.cursors[1].closed)
        reader.commit.assert_not_called()
        reader.rollback.assert_not_called()

    def test_fresh_committed_reads_leave_writer_transaction_and_pending_changes_intact(self):
        self.writer.begin()
        self.writer.pending["value"] = "uncommitted local change"
        self.db._in_block = True
        self.assertEqual(self.read()[0]["value"], "initial")
        self.engine["value"] = "committed by another connection"
        self.assertEqual(self.read()[0]["value"], "committed by another connection")
        self.assertEqual(self.writer.snapshot["value"], "initial")
        self.assertEqual(self.writer.pending["value"], "uncommitted local change")
        self.assertTrue(self.db.in_transaction)
        self.writer.begin.assert_called_once_with()
        self.writer.commit.assert_not_called()
        self.writer.rollback.assert_not_called()
        self.assertEqual(self.writer.queries, [])

    def test_rejects_nonselect_multistatement_and_locking_select_before_connecting_reader(self):
        for sql in ("UPDATE brain_facts SET value=?", "DELETE FROM brain_facts", "COMMIT", "",
                    "SELECT 1; DELETE FROM brain_facts", "SELECT * FROM brain_facts FOR UPDATE",
                    "SELECT * FROM brain_facts FOR SHARE", "SELECT 1 INTO OUTFILE 'some-file'", None):
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                self.db.fetch_readonly(sql)
        self.assertEqual(self.connect.call_count, 1)
        self.assertIsNone(self.db._read_connection)

    def test_query_failure_closes_reader_and_cursor_without_touching_writer(self):
        self.read()
        reader = self.connections[1]
        reader.query_error = RuntimeError("query failed")
        with self.assertRaisesRegex(RuntimeError, "query failed"):
            self.read()
        self.assertTrue(reader.cursors[-1].closed)
        reader.close.assert_called_once_with()
        self.assertIsNone(self.db._read_connection)
        self.writer.commit.assert_not_called()
        self.writer.rollback.assert_not_called()
        self.writer.close.assert_not_called()
        self.assertEqual(self.read()[0]["value"], "initial")
        self.assertEqual(self.connect.call_count, 3)

    def test_fetch_failure_preserves_original_error_despite_cleanup_errors(self):
        self.read()
        reader = self.connections[1]
        reader.fetch_error = ValueError("fetch failed")
        reader.cursor_close_error = RuntimeError("cursor cleanup failed")
        reader.connection_close_error = RuntimeError("connection cleanup failed")
        with self.assertRaisesRegex(ValueError, "fetch failed"):
            self.read()
        self.assertTrue(reader.cursors[-1].closed)
        reader.close.assert_called_once_with()
        self.assertIsNone(self.db._read_connection)
        self.writer.rollback.assert_not_called()

    def test_cursor_creation_and_cursor_close_failures_discard_reader(self):
        self.read()
        reader = self.connections[1]
        reader.cursor_error = RuntimeError("cursor creation failed")
        with self.assertRaisesRegex(RuntimeError, "cursor creation failed"):
            self.read()
        self.assertTrue(reader.closed)
        self.assertIsNone(self.db._read_connection)
        self.read()
        reader = self.connections[2]
        reader.cursor_close_error = RuntimeError("cursor close failed")
        with self.assertRaisesRegex(RuntimeError, "cursor close failed"):
            self.read()
        self.assertTrue(reader.closed)
        self.assertIsNone(self.db._read_connection)

    def test_close_always_closes_writer_even_if_reader_close_raises(self):
        self.read()
        reader = self.connections[1]
        reader.connection_close_error = RuntimeError("close failed")
        with self.assertRaisesRegex(RuntimeError, "close failed"):
            self.db.close()
        self.writer.close.assert_called_once_with()
        self.assertTrue(self.writer.closed)
        self.assertIsNone(self.db._read_connection)
        with self.assertRaisesRegex(RuntimeError, "已关闭"):
            self.read()

    def test_connection_failure_does_not_leave_a_half_initialized_reader(self):
        self.connect.side_effect = RuntimeError("connect failed")
        with self.assertRaisesRegex(RuntimeError, "connect failed"):
            self.read()
        self.assertIsNone(self.db._read_connection)
        self.writer.close.assert_not_called()
        self.writer.commit.assert_not_called()
        self.writer.rollback.assert_not_called()

    def test_reads_use_existing_audit_without_exposing_parameter_values(self):
        audit = Mock(wraps=self.db.audit)
        self.db.audit = audit
        self.read()
        self.assertEqual(audit.run.call_count, 1)
        self.assertEqual(audit.run.call_args.args[:2],
                         ("SELECT id,value FROM test_brain_facts WHERE id=%s", ("F1",)))


if __name__ == "__main__":
    unittest.main()
