"""真实 MySQL 8.4 验收五套持久化账本和 SQLite 29 表迁移（CI 专用）。"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from MaiLivingWorld.brain_core import BrainBook
from MaiLivingWorld.mind import MindStore
from MaiLivingWorld.cognition import CognitiveStore
from MaiLivingWorld.experience_loop import ExperienceLoop
from MaiLivingWorld.proactive_feedback import ProactiveFeedback
from MaiLivingWorld.store import Store
from MaiLivingWorld.explore import Story
from MaiLivingWorld.scripts.migrate_all_sqlite_to_mysql import snapshot, apply, ALL_TABLES


@unittest.skipUnless(os.getenv("MLW_LIVE_MYSQL")=="1","Only with ephemeral MySQL CI")
class AllMySQLLedgersLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings={
            "host":os.environ["MLW_MYSQL_HOST"],
            "port":int(os.environ.get("MLW_MYSQL_PORT","3306")),
            "user":os.environ["MLW_MYSQL_USER"],
            "password":os.environ["MLW_MYSQL_PASSWORD"],
            "database":os.environ["MLW_MYSQL_DATABASE"],
            "table_prefix":"unified_",
        }
        cls.migration={**cls.settings,"table_prefix":"migration_"}
        cls.temp=tempfile.TemporaryDirectory()
        cls.dir=Path(cls.temp.name)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def evidence(self,id,text,actor="12345678"):
        return {"id":id,"digest":"sha:"+id,"actor_id":actor,
                "timestamp":time.time(),"text":text}

    def test_01_brain_evidence_scopes_and_progress_reopen(self):
        scope="private:12345678"
        db=BrainBook(self.dir/"brain.db",mysql_settings=self.settings)
        now=time.time()
        e=[self.evidence("A1","我要完成下单并放入箱子")]
        count=db.apply(scope,"cycle1",e,{"goals":[{
            "owner_id":"12345678","title":"下单并装箱",
            "criterion":"完成下单并放入箱子",
            "next_action":"wait","evidence_ids":["A1"]}]},now=now)
        self.assertEqual(count["goals"],1)
        gid=db.recall(scope)["goals"][0]["id"]
        step=db.apply(scope,"cycle2",[self.evidence("A2","我已经下单，但还没有装箱")],
            {"progress":[{"goal_id":gid,"state":"in_progress","evidence_ids":["A2"]}]},
            now=now+1)
        self.assertEqual(step["progress"],1)
        self.assertEqual(db.recall(scope)["goals"][0]["status"],"open")
        bad=db.apply(scope,"cycle3",[self.evidence("A3","我已经下单了，但还没放入箱子")],
            {"progress":[{"goal_id":gid,"state":"done","evidence_ids":["A3"]}]},
            now=now+2)
        self.assertEqual(bad["progress"],0)
        done=db.apply(scope,"cycle4",[
            self.evidence("A4","我已经完成下单了"),
            self.evidence("A5","我已经放入箱子了")],
            {"progress":[{"goal_id":gid,"state":"done","evidence_ids":["A4","A5"]}]},
            now=now+3)
        self.assertEqual(done["progress"],1)
        self.assertEqual([r["kind"] for r in db.goal_trace(scope,gid)["timeline"]],
                         ["origin","in_progress","done","done"])
        self.assertEqual(db.recall(scope)["goals"][0]["status"],"done")
        self.assertFalse(db.goal_trace("group:98765432",gid)["found"])
        db.close()
        reopened=BrainBook(self.dir/"brain.db",mysql_settings=self.settings)
        self.assertEqual(reopened.recall(scope)["goals"][0]["status"],"done")
        reopened.close()

    def test_02_mind_snapshot_and_recovery(self):
        db=MindStore(self.dir/"mind.db",mysql_settings=self.settings)
        owner="12345678"
        now=time.time()
        evidence=[{"id":"Eabc","digest":"sha:Eabc","role":"owner","text":"我喜欢RNN"}]
        notes=[{"note_key":"rnn-study","kind":"preference","text":"喜欢学习RNN",
                "basis":"user_explicit","confidence":0.96,"evidence_ids":["Eabc"]}]
        result=db.save_reflection(owner,"digest-one",notes,[],evidence,now,20)
        self.assertGreaterEqual(result["candidates"],1)
        self.assertEqual(len(db.notes(owner)),1)
        self.assertTrue(db.snapshots(owner))
        self.assertEqual(db.state(owner)["reflection_count"],1)
        self.assertTrue(db.restore(owner,db.snapshots(owner)[0]["id"]))
        db.close()

    def test_03_cognition_and_events(self):
        db=CognitiveStore(self.dir/"cog.db",mysql_settings=self.settings)
        now=time.time()
        self.assertTrue(db.eligible(now,now-10,1,2)[0])
        cid=db.begin(now,1)
        self.assertGreater(cid,0)
        db.failed(cid)
        self.assertEqual(db.summary()["cycle_count"],1)
        db.close()

    def test_04_experience_policy_and_feedback(self):
        db=ExperienceLoop(self.dir/"exp.db",mysql_settings=self.settings)
        now=time.time()
        self.assertTrue(db.begin(101,"12345678","sid","explore","topic",now=now))
        self.assertTrue(db.mark(101,"reply_planned",now=now+1))
        self.assertTrue(db.mark(101,"send_confirmed",now=now+2))
        self.assertEqual(db.observe("12345678","sid","M1","这个话题没兴趣",
                                     now=now+3,active=True),"negative")
        self.assertEqual(db.status("12345678")["recent"][0]["feedback"],"negative")
        db.close()

    def test_05_proactive_backoff(self):
        db=ProactiveFeedback(self.dir/"feed.db",mysql_settings=self.settings)
        now=time.time()
        target="qq:private:12345678"
        eid=db.begin(target,"sid",now)
        self.assertGreater(eid,0)
        self.assertTrue(db.finish(eid,"planner_stop",now=now))
        self.assertFalse(db.check(target,now+10,now-60,100,1)[0])
        self.assertEqual(db.outcome(eid),"planner_stop")
        db.close()

    def test_06_migration_all_tables_hashes_and_reimport_rejection(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)
            stories=Store(directory/"living_world.sqlite3")
            stories.save_stories([Story("example-story","B站热门","消息标题",
                                       "https://example.org/item","介绍",time.time())])
            stories.mark_memory_saved("example-story")
            stories.record_attempt("qq:private:12345678",time.time(),"explore","example-story")
            stories.close()
            brain=BrainBook(directory/"brain_book.sqlite3")
            brain.apply("private:12345678","init",[self.evidence("MIG1","我要买裙子")],{
                "goals":[{"owner_id":"12345678","title":"买裙子",
                          "criterion":"已经购买裙子",
                          "next_action":"wait","evidence_ids":["MIG1"]}]},now=time.time())
            brain.close()
            mind=MindStore(directory/"mind.sqlite3");mind.close()
            cog=CognitiveStore(directory/"cognition.sqlite3");cog.close()
            exp=ExperienceLoop(directory/"experience_loop.sqlite3");exp.close()
            feedback=ProactiveFeedback(directory/"proactive_feedback.sqlite3");feedback.close()
            rows,missing=snapshot(directory)
            self.assertEqual(missing,[])
            self.assertEqual(len(rows),len(ALL_TABLES))
            self.assertEqual(apply(rows,self.migration)["stories"],1)
            self.assertEqual(apply.__name__,"apply")
            with self.assertRaisesRegex(ValueError,"目标表非空"):
                apply(rows,self.migration)
