"""v0.8.0 事件、事实、冲突、目标、隔离、有限反思与可控注入回归。"""
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
        counts=self.db.apply(PRIVATE,"digest-2",[entry("E3")],{
            "progress":[{"goal_id":gid,"state":"done","evidence_ids":["E3"]}]
        },now=self.now+10)
        self.assertEqual(counts["progress"],1)
        self.assertEqual(self.db.recall(PRIVATE)["goals"][0]["status"],"done")
        self.assertEqual(self.db.status(PRIVATE)["cycles"],2)
        self.assertTrue(isinstance(self.db.recall(PRIVATE)["reflections"],list))
        self.assertEqual(self.db.status(GROUP_SCOPE)["events"],0)

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
