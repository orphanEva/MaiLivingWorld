"""v0.9.0 事实复核、快速周期、目标去重、隔离与可控注入回归。"""
from __future__ import annotations

import json
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from MaiLivingWorld.brain_core import BrainBook, valid_scope
from MaiLivingWorld.brain_runtime import evidence_from_qq, parse_proposal, build_prompt
from MaiLivingWorld.tests.test_runtime import FakeContext, plugin


OWNER = "12345678"
OTHER = "87654321"
GROUP = "22222222"
PRIVATE = "private:" + OWNER
GROUP_SCOPE = "group:" + GROUP


def entry(k, who=OWNER, txt="学习深度学习时更喜欢数字计算示例", stamp=10000):
    return {"id":k, "digest":"hash:"+k, "actor_id":who,"text":txt,"timestamp":float(stamp)}


def host_msg(mid, who=OWNER, sid="private-one", gid=None, text="学习深度学习时更喜欢数字计算示例"):
    return {
        "platform":"qq","message_id":mid,"session_id":sid,
        "account_id":"99999999","processed_plain_text":text,
        "message_info":{"user_info":{"user_id":who},
                        "group_info":{"group_id":gid} if gid else None},
    }


class BrainBookTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=BrainBook(Path(self.tmp.name)/"brain.db")
        self.now=time.time()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_action_result_and_explicit_feedback(self):
        self.assertTrue(self.db.record_action(PRIVATE,42,"history",now=self.now))
        self.assertFalse(self.db.record_action(PRIVATE,42,"history",now=self.now))
        self.assertFalse(self.db.record_result(42,"send_confirmed",now=self.now+1))
        self.assertTrue(self.db.record_result(42,"reply_planned",now=self.now+2))
        self.assertTrue(self.db.record_result(42,"send_confirmed",now=self.now+3))
        self.assertFalse(self.db.record_result(42,"planner_stop",now=self.now+4))
        self.assertTrue(self.db.record_user_feedback(42,"negative"))
        self.assertFalse(self.db.record_user_feedback(43,"positive"))
        action=self.db.recall(PRIVATE)["actions"][0]
        self.assertEqual(action["outcome"],"send_confirmed")
        self.assertEqual(action["user_feedback"],"negative")
        self.assertEqual(self.db.recall(GROUP_SCOPE)["actions"],[])

    def test_scope_validation(self):
        self.assertTrue(valid_scope(PRIVATE))
        self.assertTrue(valid_scope(GROUP_SCOPE))
        self.assertFalse(valid_scope("private:../../secret"))
        self.assertFalse(valid_scope("all"))
        self.assertFalse(valid_scope("private:"))

    def test_events_facts_goals_and_progress(self):
        ev=[entry("E1"),entry("E2", stamp=10001)]
        payload={
            "events":[{"actor_id":OWNER,"kind":"achievement","summary":"终于掌握循环神经网络的隐藏状态",
                       "importance":4,"evidence_ids":["E1"]}],
            "facts":[{"subject_id":OWNER,"attribute":"preferred_learning_method",
                      "value":"喜欢带数字计算的示例","confidence":.92,"evidence_ids":["E1","E2"]}],
            "goals":[{"owner_id":OWNER,"title":"完成RNN的基础学习",
                      "criterion":"本人明确说已完成RNN基础内容",
                      "next_action":"follow_up","evidence_ids":["E1"]}],
            "reflection":"用户偏好仍需要日后证据继续修正",
        }
        counts=self.db.apply(PRIVATE,"digest-1",ev,payload,now=self.now)
        self.assertEqual(counts,{"events":1,"facts":1,"goals":1,"progress":0})
        got=self.db.recall(PRIVATE)
        self.assertEqual(got["facts"][0]["status"],"active")
        self.assertEqual(got["goals"][0]["status"],"open")
        self.assertIn("喜欢带数字计算的示例",self.db.hint(PRIVATE))
        self.assertNotIn("喜欢带数字计算的示例",self.db.hint(GROUP_SCOPE))
        gid=got["goals"][0]["id"]
        counts=self.db.apply(PRIVATE,"digest-2",[entry("E3",txt="我已完成RNN基础内容")],{
            "progress":[{"goal_id":gid,"state":"done","evidence_ids":["E3"]}]
        },now=self.now+10)
        self.assertEqual(counts["progress"],1)
        self.assertEqual(self.db.recall(PRIVATE)["goals"][0]["status"],"done")
        self.assertEqual(self.db.status(PRIVATE)["cycles"],2)
        self.assertTrue(isinstance(self.db.recall(PRIVATE)["reflections"],list))
        self.assertEqual(self.db.status(GROUP_SCOPE)["events"],0)

    def test_goal_completion_requires_all_criteria_with_fresh_owner_evidence(self):
        origin = entry("G1", txt="我要完成下单并放入箱子")
        self.db.apply(PRIVATE, "goal-origin", [origin], {
            "goals": [{"owner_id": OWNER, "title": "下单并装箱",
                       "criterion": "完成下单并放入箱子",
                       "next_action": "follow_up", "evidence_ids": ["G1"]}]
        }, now=self.now)
        gid = self.db.recall(PRIVATE)["goals"][0]["id"]

        def progress(evidence_rows, seq):
            diag = {}
            counts = self.db.apply(PRIVATE, "goal-progress-" + str(seq),
                evidence_rows, {"progress": [{
                    "goal_id": gid, "state": "done",
                    "evidence_ids": [e["id"] for e in evidence_rows]
                }]}, now=self.now + seq, diagnostics=diag)
            return counts["progress"], diag

        # 新证据仅表明下单，还明确否定装箱。
        count, diag = progress(
            [entry("G2", txt="我已经下单了，但还没有放入箱子")], 1)
        self.assertEqual(count, 0)
        self.assertEqual(diag["store_rejected"]["progress.criterion_not_proven"], 1)
        self.assertEqual(self.db.recall(PRIVATE)["goals"][0]["status"], "open")

        # 旧的目标创建证据不能被重用为“新进展”。
        count, diag = progress([origin], 2)
        self.assertEqual(count, 0)
        self.assertEqual(diag["store_rejected"]["progress.no_new_owner_evidence"], 1)

        # 两条独立的正向新消息共同证明全部子条件。
        count, _ = progress([
            entry("G3", txt="我已经下单了"),
            entry("G4", txt="我已经放入箱子了")
        ], 3)
        self.assertEqual(count, 1)
        self.assertEqual(self.db.recall(PRIVATE)["goals"][0]["status"], "done")

    def test_goal_completion_rejects_unrelated_or_negated_evidence(self):
        self.db.apply(PRIVATE, "goal-A", [entry("G1", txt="我要学习RNN")], {
            "goals": [{"owner_id": OWNER, "title": "完成RNN基础学习",
                       "criterion": "本人明确说已完成RNN基础内容",
                       "next_action": "follow_up", "evidence_ids": ["G1"]}]
        }, now=self.now)
        gid = self.db.recall(PRIVATE)["goals"][0]["id"]
        for i, text in enumerate(("最近在学RNN基础内容", "我还没完成RNN基础内容"), 1):
            counts = self.db.apply(PRIVATE, "incorrect-progress-"+str(i),
                [entry("G"+str(i+1), txt=text)],
                {"progress": [{"goal_id": gid, "state": "done",
                               "evidence_ids": ["G"+str(i+1)]}]},
                now=self.now+i)
            self.assertEqual(counts["progress"], 0)
            self.assertEqual(self.db.recall(PRIVATE)["goals"][0]["status"], "open")

        counts = self.db.apply(PRIVATE, "correct-progress",
            [entry("G5", txt="我已完成RNN基础内容")],
            {"progress": [{"goal_id": gid, "state": "done", "evidence_ids": ["G5"]}]},
            now=self.now+5)
        self.assertEqual(counts["progress"], 1)
        self.assertEqual(self.db.recall(PRIVATE)["goals"][0]["status"], "done")

    def test_goal_completion_rejects_hypothetical_questions_and_partial_claims(self):
        self.db.apply(PRIVATE, "goal-skirt", [entry("S1", txt="我想选个裙子款式")], {
            "goals": [{"owner_id": OWNER, "title": "选定裙子款式",
                       "criterion": "确定裙子款式",
                       "next_action": "follow_up", "evidence_ids": ["S1"]}]
        }, now=self.now)
        gid = self.db.recall(PRIVATE)["goals"][0]["id"]
        for i, text in enumerate((
            "如果我已经确定裙子款式了，会告诉你",
            "我已经确定裙子款式了吗？",
            "我已经准备确定裙子款式",
            "我只是初步确定裙子款式",
        ), 1):
            eid = "S" + str(i + 1)
            count = self.db.apply(PRIVATE, "skirt-hypothetical-" + str(i),
                [entry(eid, txt=text)], {
                    "progress": [{"goal_id": gid, "state": "done",
                                  "evidence_ids": [eid]}]
                }, now=self.now+i)["progress"]
            self.assertEqual(count, 0, text)
            self.assertEqual(self.db.recall(PRIVATE)["goals"][0]["status"], "open")
        count = self.db.apply(PRIVATE, "skirt-done",
            [entry("S6", txt="裙子款式已经确定")], {
                "progress": [{"goal_id": gid, "state": "done", "evidence_ids": ["S6"]}]
            }, now=self.now+6)["progress"]
        self.assertEqual(count, 1)
        self.assertEqual(self.db.recall(PRIVATE)["goals"][0]["status"], "done")

    def test_history_reflections_are_scoped_recent_deep_only_and_deduplicated(self):
        old = self.now - 35 * 86400
        cases = [
            (PRIVATE, "past-old", "早期记录已经过时，需要重新核对", old, False),
            (GROUP_SCOPE, "other-scope", "群内讨论的目标仍需确认", self.now-90, False),
            (PRIVATE, "first", "主人尚未确认任务确实结束", self.now-60, False),
            (PRIVATE, "repeat", "主人尚未确认任务确实结束", self.now-50, False),
            (PRIVATE, "quick", "快速周期不该成为历史反思", self.now-30, True),
            (PRIVATE, "latest", "学习任务的完成证据依然不明确", self.now-20, False),
        ]
        for scope,signature,reflection,stamp,quick in cases:
            self.db.apply(scope, signature, [entry("E"+signature)], {
                "reflection": reflection
            }, now=stamp, quick=quick)
        rows=self.db.recent_reflections(PRIVATE, now=self.now, limit=3)
        self.assertEqual([r["reflection"] for r in rows], [
            "学习任务的完成证据依然不明确",
            "主人尚未确认任务确实结束",
        ])
        self.assertTrue(all(r["scope"]==PRIVATE for r in rows))
        self.assertEqual(self.db.recent_reflections("private:../../bad", now=self.now),[])
        self.assertEqual(len(self.db.recent_reflections(GROUP_SCOPE, now=self.now)),1)
        self.assertNotIn("学习任务的完成证据依然不明确", self.db.hint(PRIVATE))

    def test_history_reflections_do_not_become_new_evidence(self):
        note="主人也许已经完成装箱，但还没有新证据"
        self.db.apply(PRIVATE,"record-note",[entry("N1")],{"reflection":note},
                      now=self.now-10)
        past=self.db.recent_reflections(PRIVATE,now=self.now)
        prompt=build_prompt(PRIVATE,[entry("E1")],{"facts":[],"goals":[]},
                            prior_reflections=past)
        self.assertIn(note,prompt)
        self.assertIn("unverified_previous_model_review",prompt)
        self.assertIn("不得把它当成新证据",prompt)
        self.assertNotIn(note,build_prompt(PRIVATE,[entry("E1")],{}))
        # 有历史反思却没有本轮真实引用时，解析器不会凭旧笔记产出事实。
        parsed=parse_proposal({"content":json.dumps({"facts":[{
            "subject_id":OWNER,"attribute":"goal_status","value":"已经装箱",
            "confidence":.99,"evidence_ids":["N1"]}]} ,ensure_ascii=False)},
            [entry("E1")])
        self.assertEqual(parsed["facts"],[])

    def test_historical_reflection_prompt_rejects_other_scope_and_sensitive_text(self):
        rows=[
            {"scope":GROUP_SCOPE,"reflection":"其他群的绝密计划，不得出现在私聊"},
            {"scope":PRIVATE,"reflection":"密码12345678已经泄露"},
            {"scope":PRIVATE,"reflection":"仍需核实是否真的完成了数据线整理"},
        ]
        prompt=build_prompt(PRIVATE,[entry("E1")],{},prior_reflections=rows)
        self.assertNotIn("绝密计划",prompt)
        self.assertNotIn("密码12345678",prompt)
        self.assertIn("仍需核实是否真的完成了数据线整理",prompt)

    def test_conflict_requires_explicit_review(self):
        def p(value, i):
            return {"facts":[{"subject_id":OWNER,"attribute":"study_current_topic",
                              "value":value,"confidence":.94,"evidence_ids":[f"E{i}a",f"E{i}b"]}]}
        self.db.apply(PRIVATE,"a",[entry("E1a"),entry("E1b")],
                      p("目前学习RNN基础",1),now=self.now)
        self.db.apply(PRIVATE,"b",[entry("E2a"),entry("E2b")],
                      p("目前学习Transformer结构",2),now=self.now+10)
        values=self.db.recall(PRIVATE)["facts"]
        self.assertEqual({x["status"] for x in values},{"disputed"})
        self.assertEqual(self.db.hint(PRIVATE),"")
        desired=next(x for x in values if "Transformer" in x["value"])
        self.assertFalse(self.db.review_fact(GROUP_SCOPE,desired["id"],"approve"))
        self.assertTrue(self.db.review_fact(PRIVATE,desired["id"],"approve"))
        by_value={x["value"]:x for x in self.db.recall(PRIVATE)["facts"]}
        self.assertEqual(by_value["目前学习Transformer结构"]["status"],"active")
        self.assertEqual(by_value["目前学习RNN基础"]["status"],"superseded")

    def test_equivalent_rnn_facts_share_identity_and_do_not_conflict(self):
        def fact(value, ids):
            return {"facts": [{"subject_id": OWNER, "attribute": "study_current_topic",
                               "value": value, "confidence": .96, "evidence_ids": ids}]}
        self.db.apply(PRIVATE, "same-fact-1", [entry("A1"), entry("A2")],
                      fact("正在学习RNN基础", ["A1", "A2"]), now=self.now)
        first = self.db.recall(PRIVATE)["facts"]
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["status"], "active")
        fid = first[0]["id"]

        self.db.apply(PRIVATE, "same-fact-2", [entry("B1"), entry("B2")],
                      fact("目前在学习循环神经网络基础", ["B1", "B2"]),
                      now=self.now+10)
        after = self.db.recall(PRIVATE)["facts"]
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0]["id"], fid)
        self.assertEqual(after[0]["status"], "active")
        self.assertEqual(after[0]["seen_count"], 4)
        self.assertIn("学习", self.db.hint(PRIVATE))

    def test_equivalent_facts_accumulate_independent_evidence_and_keep_scope(self):
        def one(value, id_):
            return {"facts": [{"subject_id": OWNER, "attribute": "学习进度",
                               "value": value, "confidence": .96, "evidence_ids": [id_]}]}
        self.db.apply(PRIVATE, "pend-1", [entry("A1")],
                      one("正在学习RNN基础", "A1"), now=self.now)
        self.assertEqual(self.db.recall(PRIVATE)["facts"][0]["status"], "pending")
        self.db.apply(PRIVATE, "pend-2", [entry("A2")],
                      one("目前在学习循环神经网络基础", "A2"),
                      now=self.now+10)
        self.assertEqual(len(self.db.recall(PRIVATE)["facts"]), 1)
        self.assertEqual(self.db.recall(PRIVATE)["facts"][0]["status"], "active")
        self.db.apply(GROUP_SCOPE, "group-equiv", [entry("G1")],
                      one("目前在学习循环神经网络基础", "G1"),
                      now=self.now+20)
        self.assertEqual(self.db.recall(GROUP_SCOPE)["facts"][0]["status"], "pending")
        self.assertEqual(self.db.recall(PRIVATE)["facts"][0]["seen_count"], 2)

    def test_equivalence_preserves_meaningful_stage_conflicts_and_review(self):
        def fact(value, prefix):
            return {"facts": [{"subject_id": OWNER, "attribute": "study_current_topic",
                               "value": value, "confidence": .96,
                               "evidence_ids": [prefix+"1", prefix+"2"]}]}
        self.db.apply(PRIVATE, "p1", [entry("A1"), entry("A2")],
                      fact("正在学习RNN基础", "A"), now=self.now)
        self.db.apply(PRIVATE, "p2", [entry("B1"), entry("B2")],
                      fact("已经学完循环神经网络基础", "B"), now=self.now+10)
        facts = self.db.recall(PRIVATE)["facts"]
        self.assertEqual(len(facts), 2)
        self.assertEqual({f["status"] for f in facts}, {"disputed"})
        self.assertEqual(self.db.hint(PRIVATE), "")
        # 冲突发生后不能凭重复同义消息绕过人工复核。
        self.db.apply(PRIVATE, "p3", [entry("C1"), entry("C2")],
                      fact("目前在学习循环神经网络基础", "C"), now=self.now+20)
        facts = self.db.recall(PRIVATE)["facts"]
        self.assertEqual(len(facts), 2)
        self.assertEqual({f["status"] for f in facts}, {"disputed"})

    def test_private_group_isolation_and_clear(self):
        self.db.apply(PRIVATE,"p",[entry("E1")],{
            "events":[{"actor_id":OWNER,"kind":"experience","summary":"私人讨论深度学习方法","importance":3,
                       "evidence_ids":["E1"]}]},now=self.now)
        self.db.apply(GROUP_SCOPE,"g",[entry("E2",who=OTHER)],{
            "events":[{"actor_id":OTHER,"kind":"experience","summary":"群友在群里讨论小游戏","importance":2,
                       "evidence_ids":["E2"]}]},now=self.now)
        self.assertEqual(self.db.status(PRIVATE)["events"],1)
        self.assertEqual(self.db.status(GROUP_SCOPE)["events"],1)
        self.assertTrue(self.db.clear_scope(PRIVATE))
        self.assertEqual(self.db.status(PRIVATE)["events"],0)
        self.assertEqual(self.db.status(GROUP_SCOPE)["events"],1)

    def test_reject_wrong_owner_evidence(self):
        self.db.apply(GROUP_SCOPE,"g",[entry("E1",who=OTHER)],{
            "facts":[{"subject_id":OWNER,"attribute":"favorite_game",
                      "value":"明确喜欢这个游戏","confidence":.95,"evidence_ids":["E1"]}],
            "goals":[{"owner_id":OWNER,"title":"完成群内技术教程",
                      "criterion":"本人明确宣布完成","next_action":"wait","evidence_ids":["E1"]}]
        },now=self.now)
        self.assertEqual(self.db.status(GROUP_SCOPE)["facts"],0)
        self.assertEqual(self.db.status(GROUP_SCOPE)["goals"],0)

    def test_existing_v08_archived_fact_does_not_reuse_old_evidence(self):
        import sqlite3
        old_path=Path(self.tmp.name)/"existing_v08.sqlite3"
        legacy=sqlite3.connect(old_path)
        legacy.execute("""CREATE TABLE brain_facts (
            id TEXT PRIMARY KEY, scope TEXT NOT NULL, subject_id TEXT NOT NULL,
            attribute TEXT NOT NULL, value TEXT NOT NULL, confidence REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', seen_count INTEGER NOT NULL DEFAULT 0,
            created REAL NOT NULL, updated REAL NOT NULL, valid_until REAL)""")
        legacy.execute("""INSERT INTO brain_facts VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            ("Flegacy",PRIVATE,OWNER,"偏好","蓝色",.93,"superseded",2,self.now,self.now,self.now))
        legacy.commit()
        legacy.close()
        migrated=BrainBook(old_path)
        row=migrated.con.execute(
            "SELECT status,seen_count,reviewed_count FROM brain_facts WHERE id='Flegacy'"
        ).fetchone()
        self.assertEqual((row["status"],row["seen_count"],row["reviewed_count"]),
                         ("superseded",2,2))
        migrated.close()

    def test_store_diagnostic_reports_fixed_reason_only(self):
        proof=[entry("D1"),entry("D2")]
        diag={}
        rows={
            "facts":[{"subject_id":OTHER,"attribute":"衣服颜色","value":"蓝白",
                      "confidence":.95,"evidence_ids":["D1","D2"]}],
            "goals":[{"owner_id":OWNER,"title":"整理桌面数据线",
                      "criterion":"全部贴编号标签并拍照",
                      "next_action":"unknown_action","evidence_ids":["D1"]}]
        }
        stats=self.db.apply(PRIVATE,"diagnostic-test",proof,rows,now=self.now,
                            quick=True,diagnostics=diag)
        self.assertEqual(stats["facts"],0)
        self.assertEqual(stats["goals"],0)
        self.assertEqual(diag["store_rejected"]["facts.subject_evidence_mismatch"],1)
        self.assertEqual(diag["store_rejected"]["goals.invalid_action"],1)
        self.assertEqual(diag["store_attempted"]["facts"],1)
        self.assertNotIn("蓝白",str(diag))
        self.assertNotIn(OTHER,str(diag))

    def test_store_diagnostic_counts_pending_fact(self):
        diag={}
        proof=[entry("D1",txt="我喜欢蓝白色")]
        stats=self.db.apply(PRIVATE,"pending-test",proof,{"facts":[{
            "subject_id":OWNER,"attribute":"配色偏好","value":"喜欢蓝白色",
            "confidence":.93,"evidence_ids":["D1"]}]},
            now=self.now,diagnostics=diag)
        self.assertEqual(stats["facts"],1)
        self.assertEqual(self.db.recall(PRIVATE)["facts"][0]["status"],"pending")
        self.assertEqual(diag["store_rejected"],{})

    def test_quick_update_has_independent_cooldown(self):
        self.assertEqual(self.db.quick_due(PRIVATE,"quick-a",now=self.now), (True,"ready"))
        self.db.mark_quick_attempt(PRIVATE,self.now)
        self.assertEqual(self.db.quick_due(PRIVATE,"quick-b",now=self.now+50)[1],"cooldown")
        self.assertEqual(self.db.quick_due(PRIVATE,"quick-b",now=self.now+310), (True,"ready"))
        self.db.apply(PRIVATE,"quick-b",[entry("E1")],{},now=self.now+310,quick=True)
        self.assertFalse(self.db.quick_due(PRIVATE,"quick-b",now=self.now+620)[0])
        self.assertTrue(self.db.due(PRIVATE,"quick-b",now=self.now+620,interval_hours=1))
        cycles=self.db.recall(PRIVATE)["reflections"]
        self.assertEqual(cycles[0]["cycle_kind"],"quick")

    def test_short_chinese_fact_fields_are_not_dropped(self):
        rows=[entry("E1"),entry("E2")]
        stats=self.db.apply(PRIVATE,"short-attr",rows,{"facts":[{
            "subject_id":OWNER,"attribute":"颜色","value":"蓝色",
            "confidence":.97,"evidence_ids":["E1","E2"]}]},now=self.now)
        self.assertEqual(stats["facts"],1)
        self.assertEqual(self.db.recall(PRIVATE)["facts"][0]["status"],"active")
        self.assertIn("颜色＝蓝色",self.db.hint(PRIVATE))

    def test_fact_can_reopen_when_new_independent_evidence_arrives(self):
        def p(value, prefix):
            return {"facts":[{"subject_id":OWNER,"attribute":"preferred_color",
                              "value":value,"confidence":.96,
                              "evidence_ids":[prefix+"1",prefix+"2"]}]}
        self.db.apply(PRIVATE,"a",[entry("A1"),entry("A2")],
                      p("喜欢蓝色裙子","A"),now=self.now)
        self.db.apply(PRIVATE,"b",[entry("B1"),entry("B2")],
                      p("喜欢黑色裙子","B"),now=self.now+10)
        other=next(f for f in self.db.recall(PRIVATE)["facts"] if "黑色" in f["value"])
        self.assertTrue(self.db.review_fact(PRIVATE,other["id"],"approve"))
        self.db.apply(PRIVATE,"c",[entry("C1")],{"facts":[{
            "subject_id":OWNER,"attribute":"preferred_color","value":"喜欢蓝色裙子",
            "confidence":.98,"evidence_ids":["C1"]}]},now=self.now+30)
        blue=next(f for f in self.db.recall(PRIVATE)["facts"] if "蓝色" in f["value"])
        self.assertEqual(blue["status"],"superseded")
        self.db.apply(PRIVATE,"d",[entry("D1")],{"facts":[{
            "subject_id":OWNER,"attribute":"preferred_color","value":"喜欢蓝色裙子",
            "confidence":.98,"evidence_ids":["D1"]}]},now=self.now+40)
        self.assertEqual({f["status"] for f in self.db.recall(PRIVATE)["facts"]},{"disputed"})
        history=self.db.con.execute("SELECT COUNT(*) FROM brain_fact_revisions WHERE scope=?",(PRIVATE,)).fetchone()[0]
        self.assertGreaterEqual(history,4)
        self.assertEqual(self.db.hint(PRIVATE),"")

    def test_goal_alias_dedup_does_not_merge_recurring(self):
        def goal(title,prefix):
            return {"goals":[{"owner_id":OWNER,"title":title,"criterion":"确认款式并付款",
                              "next_action":"follow_up","evidence_ids":[prefix]}]}
        self.db.apply(PRIVATE,"g1",[entry("E1")],goal("给朋友买条裙子","E1"),now=self.now)
        self.db.apply(PRIVATE,"g2",[entry("E2")],goal("购买朋友想要的裙子","E2"),now=self.now+10)
        self.assertEqual(len(self.db.recall(PRIVATE)["goals"]),1)
        self.db.apply(PRIVATE,"g3",[entry("E3")],goal("每年给朋友买条裙子","E3"),now=self.now+20)
        self.assertEqual(len(self.db.recall(PRIVATE)["goals"]),2)

    def test_legacy_database_schema_is_migrated(self):
        # 新字段及事实状态审计表应能够在旧 SQLite 上无损创建。
        columns={x["name"] for x in self.db.con.execute("PRAGMA table_info(brain_runs)")}
        self.assertIn("quick_hash",columns)
        self.assertIn("quick_last_attempt",columns)
        columns={x["name"] for x in self.db.con.execute("PRAGMA table_info(brain_facts)")}
        self.assertIn("reviewed_count",columns)
        self.assertEqual(self.db.con.execute("SELECT COUNT(*) FROM brain_fact_revisions").fetchone()[0],0)

    def test_failed_reflection_is_rate_limited_and_idempotent(self):
        self.assertTrue(self.db.due(PRIVATE,"h1",now=self.now,interval_hours=1))
        self.db.mark_attempt(PRIVATE,self.now)
        self.assertFalse(self.db.due(PRIVATE,"h1",now=self.now+100,interval_hours=1))
        self.assertTrue(self.db.due(PRIVATE,"h2",now=self.now+3700,interval_hours=1))
        self.db.apply(PRIVATE,"h2",[entry("E1")],{},now=self.now+3701)
        self.assertFalse(self.db.due(PRIVATE,"h2",now=self.now+7500,interval_hours=1))


class EvidenceRuntimeTests(unittest.TestCase):
    def test_private_group_boundaries(self):
        msgs=[host_msg("m1"),host_msg("m2",who=OTHER),
              host_msg("m3",gid=GROUP,sid="group-one"),
              host_msg("m4",gid=GROUP,sid="group-one",who=OTHER),
              host_msg("m5",text="密码是123456789"),
              host_msg("m6",sid="wrong")]
        prv=evidence_from_qq(msgs,PRIVATE,"private-one")
        grp=evidence_from_qq(msgs,GROUP_SCOPE,"group-one")
        self.assertEqual(len(prv),1)
        self.assertEqual(len(grp),2)
        self.assertEqual({e["actor_id"] for e in grp},{OWNER,OTHER})
        self.assertFalse(evidence_from_qq(msgs,PRIVATE,"group-one"))

    def test_short_correction_and_missing_host_timestamp(self):
        msg=host_msg("short-1",text="选好了")
        evidence=evidence_from_qq([msg],PRIVATE,"private-one")
        self.assertEqual(len(evidence),1)
        self.assertEqual(evidence[0]["timestamp"],0.0)
        msg["timestamp"]=1234567
        evidence=evidence_from_qq([msg],PRIVATE,"private-one")
        self.assertEqual(evidence[0]["timestamp"],1234567.0)
        msg["timestamp"]=1234567000000
        evidence=evidence_from_qq([msg],PRIVATE,"private-one")
        self.assertEqual(evidence[0]["timestamp"],1234567000.0)

    def test_parser_diagnostics_safe_counts_and_errors(self):
        proof=[entry("E1")]
        diag={}
        payload={"facts":[
            {"subject_id":OWNER,"attribute":"衣服配色","value":"喜欢蓝白",
             "confidence":.93,"evidence_ids":["E1"]},
            {"subject_id":OWNER,"attribute":"衣服配色","value":"喜欢黑色",
             "confidence":.93,"evidence_ids":["forged"]}],
            "goals":[{"owner_id":OWNER,"title":"给数据线贴标签",
                      "criterion":"全部贴完且拍照","next_action":"wait",
                      "evidence_ids":["E1"]}]}
        parsed=parse_proposal({"content":json.dumps(payload,ensure_ascii=False)},proof,diag)
        self.assertEqual(diag["model"]["facts"],2)
        self.assertEqual(diag["parsed"]["facts"],1)
        self.assertEqual(diag["parsed"]["goals"],1)
        self.assertEqual(diag["rejected"]["facts.unknown_or_duplicate_evidence"],1)
        self.assertEqual(len(parsed["facts"]),1)
        self.assertNotIn("蓝白",str(diag))
        self.assertNotIn("forged",str(diag))
        self.assertIsNone(parse_proposal({"content":"not-json"},proof,diag))
        self.assertEqual(diag["parse_error"],"non_json_object")

    def test_parser_rejects_forged_reference_and_sensitive_content(self):
        ev=[entry("E1")]
        parsed=parse_proposal({"content":json.dumps({
            "events":[{"actor_id":OWNER,"kind":"experience","summary":"私人准备技术学习计划",
                       "importance":3,"evidence_ids":["made-up"]}],
            "facts":[{"subject_id":OWNER,"attribute":"learning_style",
                      "value":"喜欢具体计算示例","confidence":.96,"evidence_ids":["E1"]}],
            "goals":[{"owner_id":OWNER,"title":"身份证号码123456789",
                      "criterion":"测试数据","next_action":"wait","evidence_ids":["E1"]}],
        },ensure_ascii=False)},ev)
        self.assertEqual(len(parsed["events"]),0)
        self.assertEqual(len(parsed["facts"]),1)
        self.assertEqual(len(parsed["goals"]),0)


class BrainPluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.bot=plugin.MaiLivingWorld()
        self.bot.ctx=FakeContext()
        self.bot.ctx.paths=types.SimpleNamespace(data_dir=Path(self.tmp.name))
        self.bot.config=plugin.Config()
        self.bot.config.owner.enabled=True
        self.bot.config.owner.qq=OWNER
        self.bot.config.brain_memory.enabled=True
        self.bot.config.brain_memory.auto_reflect=True
        self.bot.config.brain_memory.enable_events=True
        self.bot.config.brain_memory.enable_facts=False
        self.bot.config.brain_memory.min_messages=4

    async def asyncTearDown(self):
        if self.bot._brain:
            self.bot._brain.close()
        self.tmp.cleanup()

    async def test_brain_stage_logs_never_contain_private_values(self):
        from unittest.mock import MagicMock
        self.bot.ctx.logger.info=MagicMock()
        self.bot.config.brain_memory.enable_facts=True
        msgs=[host_msg(f"diag-{i}",text=f"我喜欢蓝白色的衣服，第{i}次确认") for i in range(1,5)]
        self.bot.ctx.message.get_recent.return_value=msgs
        proof=evidence_from_qq(msgs,PRIVATE,"private-one")
        self.bot.ctx.llm.generate.return_value={"content":json.dumps({"facts":[{
            "subject_id":OWNER,"attribute":"衣服配色","value":"喜欢蓝白色",
            "confidence":.95,"evidence_ids":[proof[0]["id"],proof[1]["id"]]}]},ensure_ascii=False)}
        self.assertEqual(await self.bot._reflect_brain(quick=True),1)
        entries=[call for call in self.bot.ctx.logger.info.call_args_list
                 if "BRAIN-STAGES" in str(call.args[0])]
        self.assertEqual(len(entries),1)
        self.assertEqual(entries[0].args[4]["facts"],1)
        self.assertNotIn("喜欢蓝白色",str(entries))

    async def test_prior_reflection_only_in_next_deep_cycle_and_respects_toggle(self):
        note="整理数据线时确认条件不足，需要继续核对"
        db=self.bot._ensure_brain()
        db.apply(PRIVATE,"saved-reflection",[entry("PRE1")],
                 {"reflection":note},now=time.time()-3*3600)
        messages=[host_msg(f"cycle-{i}",text=f"准备检查第{i}根数据线的标签")
                  for i in range(1,5)]
        self.bot.ctx.message.get_recent.return_value=messages
        self.bot.ctx.llm.generate.return_value={"content":"{}"}
        self.bot.config.brain_memory.enable_metacognition=True
        # 测试要满足正常的深度周期门槛；默认 12 小时，本例旧记录距今 3 小时。
        self.bot.config.brain_memory.interval_hours=1
        self.assertEqual(await self.bot._reflect_brain(),1)
        self.assertIn(note,self.bot.ctx.llm.generate.await_args.args[0])
        self.assertNotIn(note,db.hint(PRIVATE))
        # 轻量更新必须看到新增 QQ 消息；内容不变时本来就应被限频保护跳过。
        self.bot.ctx.message.get_recent.return_value=[
            *messages, host_msg("cycle-5",text="刚贴完最后一根数据线的标签")
        ]
        self.bot.ctx.llm.generate.reset_mock()
        self.assertEqual(await self.bot._reflect_brain(quick=True),1)
        self.assertNotIn(note,self.bot.ctx.llm.generate.await_args.args[0])
        # 即使笔记存在，关闭元认知也不能将其读取为模型提示。
        self.bot.ctx.llm.generate.reset_mock()
        self.bot.config.brain_memory.enable_metacognition=False
        self.bot.config.brain_memory.interval_hours=1
        new_msgs=[host_msg(f"new-cycle-{i}",text=f"再次检查第{i}根数据线")
                  for i in range(1,5)]
        self.bot.ctx.message.get_recent.return_value=new_msgs
        db.con.execute("UPDATE brain_runs SET last_attempt=0,last_success=0 WHERE scope=?",
                       (PRIVATE,))
        db.con.commit()
        self.assertEqual(await self.bot._reflect_brain(),1)
        self.assertNotIn(note,self.bot.ctx.llm.generate.await_args.args[0])

    async def test_model_reflection_and_switches(self):
        messages=[host_msg(f"m{i}",text=f"今天在练习第{i}次RNN网络训练") for i in range(1,5)]
        self.bot.ctx.message.get_recent.return_value=messages
        proof=evidence_from_qq(messages,PRIVATE,"private-one")
        self.bot.ctx.llm.generate.return_value={"content":json.dumps({
            "events":[{"actor_id":OWNER,"kind":"experience","summary":"正在反复练习循环网络训练",
                       "importance":3,"evidence_ids":[proof[0]["id"]]}],
            "facts":[{"subject_id":OWNER,"attribute":"learning_topic",
                      "value":"反复练习循环网络","confidence":.99,
                      "evidence_ids":[x["id"] for x in proof[:2]]}],
        },ensure_ascii=False)}
        await self.bot._reflect_brain()
        self.assertEqual(self.bot._brain.status(PRIVATE)["events"],1)
        self.assertEqual(self.bot._brain.status(PRIVATE)["facts"],0)
        await self.bot._reflect_brain()
        self.assertEqual(self.bot.ctx.llm.generate.await_count,1)

    async def test_quick_reflection_persists_without_deep_cycle(self):
        messages=[host_msg(f"quick-{i}",text=f"今天第{i}次练习RNN") for i in range(1,5)]
        self.bot.ctx.message.get_recent.return_value=messages
        proof=evidence_from_qq(messages,PRIVATE,"private-one")
        self.bot.ctx.llm.generate.return_value={"content":json.dumps({
            "events":[{"actor_id":OWNER,"kind":"experience","summary":"连续练习RNN循环网络",
                       "importance":2,"evidence_ids":[proof[0]["id"]]}],
            "reflection":"这轮不应保存元认知"
        },ensure_ascii=False)}
        self.assertEqual(await self.bot._reflect_brain(quick=True),1)
        self.assertEqual(self.bot._brain.recall(PRIVATE)["reflections"][0]["cycle_kind"],"quick")
        self.assertEqual(self.bot._brain.recall(PRIVATE)["reflections"][0]["reflection"],"")
        self.assertEqual(await self.bot._reflect_brain(quick=True),0)
        self.assertEqual(self.bot.ctx.llm.generate.await_count,1)
        self.assertTrue(self.bot._brain.due(PRIVATE,"new-history",now=time.time()+3601,interval_hours=1))

    async def test_group_memory_injection_only_for_exact_reply_id(self):
        self.bot.config.brain_memory.group_ids=[GROUP]
        self.bot.config.brain_memory.mode="active"
        self.bot.config.brain_memory.enable_facts=True
        db=self.bot._ensure_brain()
        db.apply(GROUP_SCOPE,"grp-hash",[entry("E1",who=OTHER),entry("E2",who=OTHER)],{
            "facts":[{"subject_id":OTHER,"attribute":"preferred_learning_method",
                      "value":"喜欢例子帮助理解","confidence":.95,"evidence_ids":["E1","E2"]}]
        },now=time.time())
        msg=host_msg("group-message",who=OTHER,sid="group-one",gid=GROUP)
        await self.bot.capture_brain(message=msg)
        kw={"session_id":"group-one","reply_message_id":"group-message",
            "messages":[{"role":"system","content":"original prompt"}]}
        ok=await self.bot.brain_reply_hint(**kw)
        self.assertIn("喜欢例子帮助理解",ok["modified_kwargs"]["messages"][0]["content"])
        bad=await self.bot.brain_reply_hint(**{**kw,"reply_message_id":"different"})
        self.assertNotIn("modified_kwargs",bad)
        isolated=await self.bot.brain_reply_hint(
            session_id="other-group",reply_message_id="group-message",
            messages=[{"role":"system","content":"original prompt"}])
        self.assertNotIn("modified_kwargs",isolated)

    async def test_proactive_result_and_owner_feedback_reaches_brain(self):
        """v0.7 真实反馈链和 v0.8 情景账本应使用相同的主动事件ID。"""
        from MaiLivingWorld.proactive_feedback import ProactiveFeedback
        from MaiLivingWorld.experience_loop import ExperienceLoop
        from MaiLivingWorld.tests.test_proactive_feedback import outgoing, tool
        self.bot.config.experience.enabled=True
        self.bot.config.experience.mode="active"
        self.bot._experience=ExperienceLoop(Path(self.tmp.name)/"experience.db")
        self.bot._feedback=ProactiveFeedback(Path(self.tmp.name)/"proactive.db")
        db=self.bot._ensure_brain()
        now=time.time()
        eid=self.bot._feedback.begin("qq:private:"+OWNER,"private-one",now)
        self.assertTrue(db.record_action(PRIVATE,eid,"history",now=now))
        self.assertTrue(self.bot._experience.begin(eid,OWNER,"private-one","history","topic",now=now))
        self.bot._pending_proactive["private-one"]=(eid,now,"qq:private:"+OWNER)
        await self.bot.proactive_planner_feedback(session_id="private-one",output_items=tool("reply"))
        await self.bot.proactive_send_feedback(message=outgoing("private-one"),sent=True)
        self.assertEqual(db.recall(PRIVATE)["actions"][0]["outcome"],"send_confirmed")
        await self.bot.capture_experience_feedback(
            message=host_msg("feedback-msg",text="这个话题没兴趣"))
        self.assertEqual(db.recall(PRIVATE)["actions"][0]["user_feedback"],"negative")
        self.assertEqual(db.status(PRIVATE)["decisions"],1)
        self.assertEqual(db.status(GROUP_SCOPE)["decisions"],0)
        self.bot._feedback.close()
        self.bot._experience.close()

    async def test_default_is_off(self):
        cfg=plugin.Config()
        self.assertFalse(cfg.brain_memory.enabled)
        self.assertFalse(cfg.brain_memory.auto_reflect)
        self.assertFalse(cfg.brain_memory.enable_events)
        self.assertFalse(cfg.brain_memory.enable_facts)
        self.assertFalse(cfg.brain_memory.enable_goals)
        self.assertFalse(cfg.brain_memory.enable_progress)
        self.assertFalse(cfg.brain_memory.enable_metacognition)


if __name__ == "__main__":
    unittest.main()
