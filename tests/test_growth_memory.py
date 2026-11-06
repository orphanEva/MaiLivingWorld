"""Source-dependent growth recall: stale or withdrawn evidence cannot be republished."""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from MaiLivingWorld.brain_core import BrainBook
from MaiLivingWorld.cognition import CognitiveStore
from MaiLivingWorld.mind import MindStore
from MaiLivingWorld.mysql_compat import MySQLCompatConnection
from MaiLivingWorld.store import Store
from MaiLivingWorld.unified_memory import collect_memories, derived_memories, resolve_memories
from MaiLivingWorld import unified_memory


NOW = 1_800_000_000.0
OWNER = "12345678"
OTHER = "87654321"
DAY = 86400


def fingerprint(record):
    """Test callback only; production callers supply growth_store's implementation."""
    fields = ("id", "text", "scope", "subject_id", "sensitivity", "evidence_status", "timestamp", "source", "kind")
    data = {key: record[key] for key in fields}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def reference(record):
    return {"id": record["id"], "digest": fingerprint(record),
            "scope": record["scope"], "sensitivity": record["sensitivity"]}


def item(category, records, *, topic="缓存技术", text="缓存可以减少重复计算", **changes):
    prefix = {"interests": "i", "beliefs": "b", "knowledge": "k", "questions": "q"}[category]
    value = {"key": prefix+":"+hashlib.sha256(topic.encode()).hexdigest()[:24],
             "topic": topic, "revision": 1, "sources": [reference(record) for record in records]}
    if category == "interests":
        value["weight"] = 0.4
    else:
        value.update(text=text, confidence=0.7, status="open" if category == "questions" else "active")
    value.update(changes)
    return value


class GrowthMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        directory = Path(self.temp.name)
        self.brain = BrainBook(directory/"brain.db")
        self.mind = MindStore(directory/"mind.db")
        self.cognition = CognitiveStore(directory/"cognition.db")
        self.stories = Store(directory/"stories.db")
        for store in (self.brain, self.mind, self.cognition, self.stories):
            self.addCleanup(store.close)

    def fact(self, ident="F1", text="缓存能减少重复计算", *, scope="private:"+OWNER,
             subject=OWNER, status="active", age=0, valid_until=None):
        with self.brain.con:
            self.brain.con.execute("""INSERT INTO brain_facts
                (id,scope,subject_id,attribute,value,confidence,status,created,updated,valid_until)
                VALUES (?,?,?,'学习知识',?,1,?,?,?,?)""",
                (ident,scope,subject,text,status,NOW-age,NOW-age,valid_until))
        return "brain:fact:"+ident

    def note(self, ident="N1", *, age=0, kind="preference", status="active", owner=OWNER):
        with self.mind.con:
            self.mind.con.execute("""INSERT INTO mind_notes
                (owner_id,note_key,kind,text,basis,confidence,first_seen,last_seen,status)
                VALUES (?,?,?,'喜欢缓存设计','user_explicit',1,?,?,?)""",
                (owner,ident,kind,NOW-age,NOW-age,status))
        return "mind:note:"+owner+":"+ident

    def originals(self):
        ids = [self.fact(), self.note()]
        with self.brain.con:
            self.brain.con.execute("""INSERT INTO brain_events
                VALUES ('E1','group:22222222',?,'experience','讨论了缓存技术',3,?,?)""", (OWNER,NOW,NOW))
            self.brain.con.execute("""INSERT INTO brain_goals
                (id,scope,owner_id,title,criterion,next_action,created,updated)
                VALUES ('G1','group:22222222',?,'学习缓存','完成实验','follow_up',?,?)""", (OWNER,NOW,NOW))
        with self.cognition.con:
            self.cognition.con.execute("""INSERT INTO cognitive_goals
                (goal_id,topic,question,observation,uncertainty,next_action,created_at,updated_at)
                VALUES ('C1','缓存','缓存是否有效','标题介绍缓存','尚未验证','compare_public_sources',?,?)""", (NOW,NOW))
        with self.stories.con:
            self.stories.con.execute("""INSERT INTO stories
                (key,source,title,url,summary,published_at,first_seen)
                VALUES ('S1','公开资讯','缓存教程','https://example.org/cache','介绍缓存原理',?,?)""", (NOW,NOW))
        return ids+["brain:event:E1", "brain:goal:G1", "cognition:goal:C1", "story:S1"]

    def resolve(self, ids, **kwargs):
        return resolve_memories(ids, brain=self.brain, mind=self.mind,
                                cognition=self.cognition, stories=self.stories, now=kwargs.get("now", NOW))

    def test_exact_resolution_matches_all_original_record_shapes_and_does_not_write(self):
        ids = self.originals()
        before = [store.con.total_changes for store in (self.brain,self.mind,self.cognition,self.stories)]
        resolved = self.resolve(ids+ids)
        collected = collect_memories(brain=self.brain,mind=self.mind,cognition=self.cognition,stories=self.stories,now=NOW)
        self.assertEqual(set(resolved), set(ids))
        for record in collected:
            self.assertEqual(fingerprint(record), fingerprint(resolved[record["id"]]))
        self.assertEqual(before, [store.con.total_changes for store in (self.brain,self.mind,self.cognition,self.stories)])
        self.assertEqual(resolved["brain:event:E1"]["evidence_status"], "recorded_event")
        self.assertEqual(resolved["cognition:goal:C1"]["evidence_status"], "unverified_observation")

    def test_exact_resolution_finds_old_requested_id_without_recent_window_sampling(self):
        old = self.fact("old", age=500*DAY)
        for index in range(30):
            self.fact("new"+str(index), age=index)
        resolved = self.resolve([old, "brain:fact:missing"])
        self.assertEqual(list(resolved), [old])

    def test_invalid_ids_or_excessive_batches_are_rejected_before_querying(self):
        invalid = ["growth:knowledge:k:"+"a"*24, "brain:fact:F1' OR 1=1 --",
                   "story:../../secret", "brain:fact:", "mind:note:not-qq:N1", "unknown:F1", None]
        with patch.object(unified_memory, "_read") as read:
            for identity in invalid:
                with self.assertRaises(ValueError):
                    self.resolve([identity])
            for batch in ((str(i) for i in range(2)), "story:S1", ["story:S1"]*129):
                with self.assertRaises(ValueError):
                    self.resolve(batch)
            read.assert_not_called()
        self.assertEqual(self.resolve([]), {})

    def test_invalidated_facts_old_notes_and_closed_items_are_not_resolved(self):
        ids = self.originals()
        ids += [self.fact("pending", status="pending"), self.fact("expired", valid_until=NOW),
                self.note("old", age=121*DAY), self.note("old-open", age=31*DAY, kind="open_topic")]
        with self.brain.con:
            self.brain.con.execute("UPDATE brain_facts SET status='disputed' WHERE id='F1'")
            self.brain.con.execute("UPDATE brain_goals SET status='done' WHERE id='G1'")
        with self.cognition.con:
            self.cognition.con.execute("UPDATE cognitive_goals SET status='resolved' WHERE goal_id='C1'")
        with self.stories.con:
            self.stories.con.execute("DELETE FROM stories WHERE key='S1'")
        self.assertEqual(set(self.resolve(ids)), {"mind:note:"+OWNER+":N1", "brain:event:E1"})

    def test_deleted_changed_or_expired_sources_cannot_reappear_as_growth(self):
        for action in ("delete", "change", "expire"):
            with self.subTest(action=action):
                identity = self.fact(action, valid_until=NOW+100)
                sources = self.resolve([identity])
                state = {"knowledge": [item("knowledge", list(sources.values()))]}
                self.assertEqual(len(derived_memories(state,sources,fingerprint=fingerprint)),1)
                with self.brain.con:
                    if action == "delete":
                        self.brain.con.execute("DELETE FROM brain_facts WHERE id=?", (action,))
                    elif action == "change":
                        self.brain.con.execute("UPDATE brain_facts SET value='原观点已被修订' WHERE id=?", (action,))
                refreshed = self.resolve([identity], now=NOW+101 if action=="expire" else NOW)
                self.assertEqual(derived_memories(state,refreshed,fingerprint=fingerprint), [])

    def test_changed_source_privacy_invalidates_even_unchanged_derived_text(self):
        identity = self.fact(scope="public:12345678")
        sources = self.resolve([identity])
        state = {"beliefs": [item("beliefs", list(sources.values()))]}
        before = derived_memories(state,sources,fingerprint=fingerprint)
        self.assertEqual(before[0]["sensitivity"], "public")
        with self.brain.con:
            self.brain.con.execute("UPDATE brain_facts SET scope='private:12345678' WHERE id='F1'")
        self.assertEqual(derived_memories(state,self.resolve([identity]),fingerprint=fingerprint), [])

    def test_cross_scope_private_and_group_sources_share_internal_memory_without_public_label(self):
        ids = [self.fact("private"), self.fact("group",scope="group:22222222")]
        sources = self.resolve(ids)
        state = {"knowledge": [item("knowledge",list(sources.values()))]}
        rows = derived_memories(state,sources,fingerprint=fingerprint,query="缓存")
        self.assertEqual(len(rows), 1)
        record = rows[0]
        self.assertEqual(record["scope"], "global:derived")
        self.assertEqual(record["subject_id"], OWNER)
        self.assertEqual(record["sensitivity"], "personal")
        self.assertEqual(record["evidence_status"], "inferred")
        self.assertEqual(record["source"], "growth")
        self.assertIn("待验证知识", record["text"])
        self.assertEqual({ref["id"] for ref in record["sources"]}, set(ids))

    def test_all_public_sources_are_public_derived_and_mixed_people_do_not_merge(self):
        ids = self.originals()
        public = self.resolve(["cognition:goal:C1", "story:S1"])
        state = {"beliefs": [item("beliefs",list(public.values()))]}
        row = derived_memories(state,public,fingerprint=fingerprint)[0]
        self.assertEqual(row["scope"], "public:derived")
        self.assertEqual(row["sensitivity"], "public")
        self.assertIn("主观看法",row["text"])
        self.assertEqual(row["evidence_status"],"inferred")
        other = self.fact("other",scope="group:22222222",subject=OTHER)
        mixed = self.resolve([ids[0], other])
        state = {"knowledge": [item("knowledge",list(mixed.values()))]}
        self.assertEqual(derived_memories(state,mixed,fingerprint=fingerprint)[0]["subject_id"], "")

    def test_most_restrictive_source_label_is_retained_through_paraphrase(self):
        ids = [self.fact("plain"), self.fact("unknown",scope="legacy"),
               self.fact("secret",text="这项缓存设计还要保密")]
        sources = self.resolve(ids)
        state = {"knowledge": [item("knowledge",list(sources.values()),text="有些缓存设计值得学习")]}
        row = derived_memories(state,sources,fingerprint=fingerprint)[0]
        self.assertEqual(row["sensitivity"],"confidential")
        unknown = {key: value for key,value in sources.items() if key != "brain:fact:secret"}
        state = {"knowledge": [item("knowledge",list(unknown.values()))]}
        self.assertEqual(derived_memories(state,unknown,fingerprint=fingerprint)[0]["sensitivity"],"unknown")

    def test_tampered_refs_and_derived_sources_do_not_become_new_evidence(self):
        identity = self.fact()
        sources = self.resolve([identity])
        base = item("knowledge",list(sources.values()))
        for field,value in (("digest","0"*64),("scope","public"),("sensitivity","public"),
                            ("id","growth:knowledge:k:"+"a"*24)):
            candidate = copy.deepcopy(base)
            candidate["sources"][0][field] = value
            self.assertEqual(derived_memories({"knowledge":[candidate]},sources,fingerprint=fingerprint),[])
        fake_sources = copy.deepcopy(sources)
        fake_sources[identity]["source"] = "growth"
        fake = item("knowledge",list(fake_sources.values()))
        self.assertEqual(derived_memories({"knowledge":[fake]},fake_sources,fingerprint=fingerprint),[])

    def test_keyword_ranking_negative_interests_and_inferred_categories(self):
        identity = self.fact()
        sources = self.resolve([identity])
        records = list(sources.values())
        state = {
            "interests": [item("interests",records,topic="广告推送",weight=-0.8)],
            "beliefs": [item("beliefs",records,topic="工程设计",text="工程决策需要明确边界")],
            "knowledge": [item("knowledge",records,topic="二分查找",text="有序序列可以每次排除一半范围")],
            "questions": [item("questions",records,topic="缓存失效",text="如何确认缓存已失效")],
        }
        rows = derived_memories(state,sources,fingerprint=fingerprint,query="二分查找",limit=4)
        self.assertEqual(rows[0]["kind"],"knowledge")
        interest = next(row for row in rows if row["kind"]=="interest")
        self.assertEqual(interest["weight"],-0.8)
        self.assertEqual(interest["topic"],"广告推送")
        self.assertIn("负值表示不喜欢",interest["text"])
        self.assertTrue(all(row["evidence_status"]=="inferred" for row in rows))
        state["questions"][0]["status"]="resolved"
        state["beliefs"][0]["status"]="rejected"
        rows = derived_memories(state,sources,fingerprint=fingerprint)
        self.assertEqual({row["kind"] for row in rows},{"interest","knowledge","question"})
        question = next(row for row in rows if row["kind"] == "question")
        self.assertEqual(question["status"], "resolved")
        self.assertIn("已标记解决的问题记录", question["text"])
        self.assertNotIn("待解问题", question["text"])

    def test_resolved_question_keeps_all_source_boundaries_and_current_conclusion(self):
        ids = [self.fact("question", text="缓存过期后是否会返回旧值", scope="group:22222222"),
               self.fact("answer", text="在过期时重新验证可阻止旧值被当成最新结果")]
        originals = self.resolve(ids)
        current = item("questions", list(originals.values()), text="应在过期时重新验证", status="resolved", revision=2)
        state = {"questions": [current]}
        before = copy.deepcopy(state)
        result = derived_memories(state, originals, fingerprint=fingerprint, query="缓存")[0]
        self.assertEqual((result["scope"], result["sensitivity"]), ("global:derived", "personal"))
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["revision"], 2)
        self.assertIn("应在过期时重新验证", result["text"])
        self.assertIn("答案仍需原始材料支持，可随新证据修订", result["text"])
        self.assertEqual(result["evidence_status"], "inferred")
        self.assertEqual(result["sources"], current["sources"])
        self.assertEqual(state, before)
        with self.brain.con:
            self.brain.con.execute("UPDATE brain_facts SET value='旧结论需要更正' WHERE id='answer'")
        self.assertEqual(derived_memories(state, self.resolve(ids), fingerprint=fingerprint), [])

    def test_question_open_resolved_and_legacy_statuses_have_distinct_recall(self):
        originals = self.resolve([self.fact()])
        for status in ("open", "resolved", "rejected", "paused", None):
            state = {"questions": [item("questions", list(originals.values()), status=status)]}
            result = derived_memories(state, originals, fingerprint=fingerprint)
            if status in {"open", "resolved"}:
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0]["status"], status)
                self.assertEqual("待解问题" in result[0]["text"], status == "open")
            else:
                self.assertEqual(result, [])

    def test_derived_bounds_and_duplicate_keys_fail_closed_without_mutation(self):
        sources = self.resolve([self.fact()])
        base = item("knowledge",list(sources.values()))
        state = {"knowledge":[base,copy.deepcopy(base)]}
        before = copy.deepcopy((state,sources))
        self.assertEqual(derived_memories(state,sources,fingerprint=fingerprint),[])
        self.assertEqual((state,sources),before)
        candidate = copy.deepcopy(base)
        candidate["sources"] *= 25
        self.assertEqual(derived_memories({"knowledge":[candidate]},sources,fingerprint=fingerprint),[])
        self.assertEqual(derived_memories({"knowledge":[base]},sources,fingerprint=fingerprint,limit=0),[])
        for value in (True,"12",1.5):
            with self.assertRaises(ValueError):
                derived_memories(state,sources,fingerprint=fingerprint,limit=value)

    def test_mysql_exact_reads_use_readonly_connection_and_parameterized_ids(self):
        identity = self.fact()
        adapter = MySQLCompatConnection.__new__(MySQLCompatConnection)
        def read(sql, parameters):
            cursor = self.brain.con.execute(sql,parameters)
            try:
                return cursor.fetchall()
            finally:
                cursor.close()
        adapter.fetch_readonly = Mock(side_effect=read)
        adapter.execute = Mock(side_effect=AssertionError("writer connection used"))
        resolved = resolve_memories([identity],brain=SimpleNamespace(con=adapter),now=NOW)
        self.assertIn(identity,resolved)
        adapter.fetch_readonly.assert_called_once()
        adapter.execute.assert_not_called()
        sql,parameters = adapter.fetch_readonly.call_args.args
        self.assertNotIn("F1",sql)
        self.assertIn("F1",parameters)
        self.assertTrue(sql.startswith("SELECT "))


if __name__ == "__main__":
    unittest.main()
