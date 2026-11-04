from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from MaiLivingWorld.brain_core import BrainBook
from MaiLivingWorld.cognition import CognitiveStore
from MaiLivingWorld.mind import MindStore
from MaiLivingWorld.mysql_compat import MySQLCompatConnection
from MaiLivingWorld.mysql_store import MySQLStore
from MaiLivingWorld.sql_logging import SQLAudit
from MaiLivingWorld.store import Store
from MaiLivingWorld import unified_memory
from MaiLivingWorld.unified_memory import collect_memories


NOW = 1_800_000_000.0
OWNER = "12345678"
OTHER = "87654321"
DAY = 86400


class UnifiedMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        directory = Path(self.temp.name)
        self.brain = BrainBook(directory / "brain.db")
        self.mind = MindStore(directory / "mind.db")
        self.cognition = CognitiveStore(directory / "cognition.db")
        self.stories = Store(directory / "stories.db")
        for store in (self.brain, self.mind, self.cognition, self.stories):
            self.addCleanup(store.close)

    def fact(self, identity, text, *, scope="private:"+OWNER, subject=OWNER,
             status="active", age=0, valid_until=None):
        with self.brain.con:
            self.brain.con.execute("""INSERT INTO brain_facts
                (id,scope,subject_id,attribute,value,confidence,status,created,updated,valid_until)
                VALUES (?,?,?,?,?,1,?,?,?,?)""",
                (identity,scope,subject,"知识或偏好",text,status,NOW-age,NOW-age,valid_until))

    def event(self, identity, text, *, scope="group:23456789", actor=OWNER, age=0):
        with self.brain.con:
            self.brain.con.execute("""INSERT INTO brain_events
                (id,scope,actor_id,kind,summary,importance,happened,created)
                VALUES (?,?,?,'experience',?,3,?,?)""", (identity,scope,actor,text,NOW-age,NOW-age))

    def goal(self, identity, *, status="open"):
        with self.brain.con:
            self.brain.con.execute("""INSERT INTO brain_goals
                (id,scope,owner_id,title,criterion,next_action,status,created,updated)
                VALUES (?,? ,?,'学习编程','能写出程序','follow_up',?,?,?)""",
                (identity,"private:"+OWNER,OWNER,status,NOW,NOW))

    def note(self, identity, *, kind="preference", status="active", age=0, owner=OWNER):
        with self.mind.con:
            self.mind.con.execute("""INSERT INTO mind_notes
                (owner_id,note_key,kind,text,basis,confidence,first_seen,last_seen,status)
                VALUES (?,?,?,'喜欢一起学习编程','user_explicit',1,?,?,?)""",
                (owner,identity,kind,NOW-age,NOW-age,status))

    def public_records(self):
        with self.cognition.con:
            self.cognition.con.execute("""INSERT INTO cognitive_goals
                (goal_id,topic,question,observation,uncertainty,next_action,created_at,updated_at)
                VALUES ('C1','编程','缓存是否能提速','文章标题介绍缓存','尚未测试',
                        'compare_public_sources',?,?)""", (NOW,NOW))
        with self.stories.con:
            self.stories.con.execute("""INSERT INTO stories
                (key,source,title,url,summary,published_at,first_seen)
                VALUES ('S1','B站热门','编程缓存教程','https://example.org/tutorial','介绍缓存原理',?,?)""",
                (NOW,NOW))

    def test_private_and_multiple_groups_are_retrieved_as_one_memory(self):
        self.fact("F1", "缓存可以避免重复计算")
        self.fact("F2", "缓存适合频繁读取的数据", scope="group:23456789")
        self.fact("F3", "缓存需要设置失效时间", scope="group:34567890", subject=OTHER)
        rows = collect_memories(brain=self.brain, query="缓存", owner_id=OWNER, now=NOW)
        self.assertEqual({r["id"] for r in rows}, {"brain:fact:F1", "brain:fact:F2", "brain:fact:F3"})
        self.assertEqual({r["scope"] for r in rows}, {"private:"+OWNER, "group:23456789", "group:34567890"})
        self.assertTrue(all(r["sensitivity"] == "personal" for r in rows))
        self.assertEqual({r["subject_id"] for r in rows}, {OWNER, OTHER})

    def test_same_qq_is_linked_across_sources_without_owner_filter(self):
        self.fact("F1", "对方正在学编程", scope="group:23456789", subject=OTHER)
        self.event("E1", "一起讨论编程", actor=OTHER)
        self.note("N1", owner=OTHER)
        rows = collect_memories(brain=self.brain, mind=self.mind, owner_id=OWNER, query=OTHER, now=NOW)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["subject_id"] == OTHER for r in rows))

    def test_fact_review_and_expiry_are_preserved(self):
        for state in ("active", "pending", "disputed", "superseded", "rejected"):
            self.fact(state, "缓存策略知识", status=state)
        self.fact("expired", "缓存的旧知识", valid_until=NOW-1)
        self.fact("expires_now", "缓存的到期知识", valid_until=NOW)
        self.fact("future", "仍然有效的缓存知识", valid_until=NOW+1)
        rows = collect_memories(brain=self.brain, now=NOW)
        self.assertEqual({r["id"] for r in rows}, {"brain:fact:active", "brain:fact:future"})
        self.assertTrue(all(r["evidence_status"] == "active" for r in rows))

    def test_open_goals_and_mind_lifetimes(self):
        self.goal("open")
        self.goal("done", status="done")
        self.note("fresh")
        self.note("old", age=121*DAY)
        self.note("old_topic", kind="open_topic", age=31*DAY)
        self.note("recent_topic", kind="open_topic", age=29*DAY)
        self.note("pending", status="pending")
        self.note("disputed", status="disputed")
        rows = collect_memories(brain=self.brain, mind=self.mind, now=NOW)
        self.assertEqual({r["id"] for r in rows}, {
            "brain:goal:open", "mind:note:"+OWNER+":fresh", "mind:note:"+OWNER+":recent_topic"})

    def test_event_identity_is_stable_and_not_promoted_to_fact(self):
        self.event("E1", "群友说新版本的缓存更快，还没有验证")
        first = collect_memories(brain=self.brain, now=NOW)
        second = collect_memories(brain=self.brain, now=NOW+1)
        self.assertEqual(first[0]["id"], second[0]["id"])
        self.assertEqual(first[0]["kind"], "event")
        self.assertEqual(first[0]["evidence_status"], "recorded_event")

    def test_chinese_query_finds_old_relevant_record_before_new_unrelated_rows(self):
        self.fact("old", "通过二分查找减少数据查找次数", age=300*DAY)
        for index in range(30):
            self.fact("new"+str(index), "今天吃了美味午餐", age=index)
        rows = collect_memories(brain=self.brain, query="二分查找怎么加速", limit=1, scan_limit=3, now=NOW)
        self.assertEqual(rows[0]["id"], "brain:fact:old")

    def test_english_matching_and_literal_underscore(self):
        self.fact("literal", "Use data_cache in Python", age=DAY)
        self.fact("wildcard", "Use dataXcache in Python")
        rows = collect_memories(brain=self.brain, query="DATA_CACHE", limit=1, scan_limit=3, now=NOW)
        self.assertEqual(rows[0]["id"], "brain:fact:literal")

    def test_source_and_confidentiality_labels_do_not_block_internal_recall(self):
        self.fact("secret", "缓存项目还在保密，请不要告诉别人")
        self.fact("unknown", "缓存项目的普通资料", scope="legacy")
        self.public_records()
        rows = collect_memories(brain=self.brain, cognition=self.cognition, stories=self.stories,
                                query="缓存", now=NOW)
        found = {r["id"]: r for r in rows}
        self.assertEqual(found["brain:fact:secret"]["sensitivity"], "confidential")
        self.assertEqual(found["brain:fact:unknown"]["sensitivity"], "unknown")
        self.assertEqual(found["cognition:goal:C1"]["evidence_status"], "unverified_observation")
        self.assertEqual(found["cognition:goal:C1"]["sensitivity"], "public")
        self.assertEqual(found["story:S1"]["evidence_status"], "source_metadata")
        self.assertEqual(found["story:S1"]["url"], "https://example.org/tutorial")

    def test_total_candidate_budget_is_bounded_and_queries_are_read_only(self):
        for index in range(20):
            self.fact("F"+str(index), "编程缓存资料")
            self.event("E"+str(index), "讨论编程缓存")
            self.note("N"+str(index))
        self.public_records()
        stores = (self.brain,self.mind,self.cognition,self.stories)
        factories = [s.con.row_factory for s in stores]
        changes = [s.con.total_changes for s in stores]
        queries = []
        for store in stores:
            store.con.set_trace_callback(queries.append)
        fetched = []
        real_read = unified_memory._read
        def recording_read(*args):
            rows = real_read(*args)
            fetched.extend(rows)
            return rows
        with patch.object(unified_memory, "_read", side_effect=recording_read):
            rows = collect_memories(brain=self.brain, mind=self.mind, cognition=self.cognition,
                stories=self.stories, query="编程", limit=50, scan_limit=7, now=NOW)
        self.assertLessEqual(len(fetched), 7)
        self.assertLessEqual(len(rows), 7)
        self.assertTrue(queries)
        self.assertTrue(all(q.startswith("SELECT ") and " LIMIT " in q for q in queries))
        self.assertEqual(factories, [s.con.row_factory for s in stores])
        self.assertEqual(changes, [s.con.total_changes for s in stores])

    def test_limits_and_errors_do_not_become_unbounded_reads(self):
        for value in (None, "12", 1.5, True):
            with self.assertRaises(ValueError):
                collect_memories(brain=self.brain, limit=value)
            with self.assertRaises(ValueError):
                collect_memories(brain=self.brain, scan_limit=value)
        with patch.object(unified_memory, "_read") as read:
            self.assertEqual(collect_memories(brain=self.brain, limit=0), [])
            self.assertEqual(collect_memories(brain=self.brain, scan_limit=-1), [])
            read.assert_not_called()
        for value in (float("nan"), float("inf"), True, "now"):
            with self.assertRaises(ValueError):
                collect_memories(now=value)
        for index in range(80):
            self.fact("F"+str(index), "一条可检索的历史记录")
        self.assertEqual(len(collect_memories(brain=self.brain, limit=10000, scan_limit=10000, now=NOW)), 50)
        with patch.object(unified_memory, "_read", side_effect=RuntimeError("database unavailable")):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                collect_memories(brain=self.brain, now=NOW)

    def test_mysql_adapters_receive_parameterized_read_queries(self):
        self.fact("F1", "缓存可以加快编程操作")
        self.public_records()
        brain_adapter = MySQLCompatConnection.__new__(MySQLCompatConnection)
        brain_adapter.prefix = "test_"
        brain_adapter.audit = SQLAudit()
        brain_adapter.con = _MySQLBridge(self.brain.con)
        brain_adapter._in_block = False
        brain_adapter._read_connection = brain_adapter.con
        brain_adapter._closed = False
        story_adapter = MySQLStore.__new__(MySQLStore)
        story_adapter.con = _MySQLBridge(self.stories.con)
        story_adapter.tables = {"stories": "`test_stories`"}
        story_adapter._sql_audit = SQLAudit()
        expected = collect_memories(brain=self.brain, stories=self.stories, query="缓存", now=NOW)
        actual = collect_memories(brain=SimpleNamespace(con=brain_adapter), stories=story_adapter,
                                  query="缓存", now=NOW)
        self.assertEqual(actual, expected)
        sql_calls = brain_adapter.con.calls + story_adapter.con.calls
        self.assertTrue(all("%s" in query and "缓存" not in query for query, _ in sql_calls))
        self.assertTrue(all(query.startswith("SELECT ") for query, _ in sql_calls))


class _MySQLBridge:
    """Exercise SQL/row adapter paths with disposable SQLite, without a network."""
    def __init__(self, sqlite_connection):
        self.connection = sqlite_connection
        self.calls = []

    def ping(self, reconnect):
        assert reconnect

    def cursor(self):
        return _MySQLCursor(self)


class _MySQLCursor:
    def __init__(self, bridge):
        self.bridge = bridge
        self.cursor = None
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def execute(self, query, parameters):
        self.bridge.calls.append((query, parameters))
        query = query.replace("%s", "?").replace("test_brain_", "brain_").replace("`test_stories`", "stories")
        self.cursor = self.bridge.connection.execute(query, parameters)

    def fetchall(self):
        names = [item[0] for item in self.cursor.description]
        return [dict(row) if hasattr(row, "keys") else dict(zip(names, row)) for row in self.cursor.fetchall()]

    def close(self):
        if self.cursor is not None:
            self.cursor.close()


if __name__ == "__main__":
    unittest.main()
