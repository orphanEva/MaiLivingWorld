"""Proactive expression uses verified destinations, not invented inbound speakers."""
from __future__ import annotations

import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from MaiLivingWorld.self_state import SelfState
from MaiLivingWorld.unified_expression import parse_expression
from MaiLivingWorld.unified_runtime import UnifiedRuntime


OWNER = "12345678"
OTHER = "87654321"
GROUP = "22222222"
RAW = "之前在私聊讨论过缓存技术，计划下周继续实验"
GENERAL = "缓存可以减少相同输入的重复计算"


def memory(**changes):
    value = {"id": "brain:fact:F1", "source": "brain_fact", "kind": "fact",
             "scope": "private:"+OWNER, "subject_id": OWNER, "text": RAW,
             "sensitivity": "personal", "evidence_status": "active", "timestamp": 1000.0}
    value.update(changes)
    return value


def context(*, group=False, target=OWNER):
    return {"scope": "group:"+GROUP if group else "private:"+target,
            "speaker_id": "" if group else target,
            "query": "缓存技术", "sid": "group-one" if group else "private-one"}


def config(**changes):
    result = dict(enabled=True,mode="active",min_interval_seconds=0,
                  max_daily_model_calls=60,model_timeout_seconds=2)
    result.update(changes)
    return SimpleNamespace(**result)


def response(mode="internal", behavior="patient"):
    decision = {"id": "brain:fact:F1", "mode": mode}
    if mode == "generalize":
        decision["text"] = GENERAL
    return {"content": json.dumps({"behavior": behavior, "decisions": [decision]}, ensure_ascii=False)}


class UnifiedProactiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"self.db"
        self.state = SelfState(self.path)
        self.state.configure_owner(OWNER)
        self.runtime = UnifiedRuntime(self.state)
        self.memories = [memory()]
        self.settings = config()
        self.generate = AsyncMock(return_value=response())

    async def asyncTearDown(self):
        self.state.close()
        self.temp.cleanup()

    async def hint(self, target=None, *, valid=lambda: True, generate=None, runtime=None, settings=None):
        return await (runtime or self.runtime).proactive_hint(target or context(), settings or self.settings,
            recall=lambda query: copy.deepcopy(self.memories), generate=generate or self.generate,
            task_name="utils",timezone="Asia/Shanghai",valid=valid)

    async def test_verified_owner_private_can_recall_own_memory_without_inbound_message(self):
        self.generate.return_value = response("say")
        result = await self.hint()
        self.assertIn("私聊对象是唯一的主人",result)
        self.assertIn(RAW,result)
        self.assertNotIn(OWNER,result)
        self.assertNotIn("brain:fact:F1",result)
        self.assertIn("不代表收到对方的新消息",result)
        self.assertEqual(self.runtime._contexts,{})

    async def test_other_private_target_gets_no_owner_relationship(self):
        self.memories = [memory(scope="private:"+OTHER,subject_id=OTHER)]
        self.generate.return_value = response("say")
        result = await self.hint(context(target=OTHER))
        self.assertIn("普通私聊联系人",result)
        self.assertNotIn("私聊对象是唯一的主人",result)
        self.assertIn(RAW,result)

    async def test_proactive_group_has_no_speaker_or_owner_and_blocks_private_original(self):
        self.generate.return_value = response("say")
        result = await self.hint(context(group=True))
        prompt = self.generate.await_args.args[0]
        data = json.loads(prompt.split("UNTRUSTED_INPUT_JSON\n",1)[1])
        self.assertEqual(data["audience"]["speaker_id"],"")
        self.assertFalse(data["audience"]["is_owner"])
        self.assertTrue(data["audience"]["proactive"])
        self.assertIn(RAW,prompt)
        self.assertNotIn(RAW,result)
        self.assertIn("没有经验证的当前发言者",result)
        self.assertNotIn("当前目标消息来自你唯一的主人",result)

    async def test_group_can_use_private_knowledge_only_after_independent_review(self):
        self.generate.side_effect = [response("generalize"),
            {"content": '{"approved_ids":["brain:fact:F1"]}'}]
        result = await self.hint(context(group=True))
        self.assertEqual(self.generate.await_count,2)
        self.assertIn(GENERAL,result)
        self.assertNotIn(RAW,result)
        self.assertEqual(self.state.state()["thought_count"],2)
        review = json.loads(self.generate.await_args.args[0].split("UNTRUSTED_REVIEW_JSON\n",1)[1])
        self.assertEqual(review["speaker_id"],"")
        self.assertFalse(review["is_owner"])

    async def test_group_internal_mode_changes_only_fixed_behavior(self):
        self.generate.return_value = response("internal",behavior="warm")
        result = await self.hint(context(group=True))
        self.assertIn("温和",result)
        self.assertNotIn(RAW,result)
        self.assertNotIn("下周",result)

    async def test_unverified_or_malformed_target_never_calls_model(self):
        candidates = [
            ({**context(),"sid":""},lambda:True),
            ({**context(),"speaker_id":OTHER},lambda:True),
            ({**context(group=True),"speaker_id":OWNER},lambda:True),
            ({**context(),"scope":"private:invalid"},lambda:True),
            (context(),lambda:False),
            (context(),lambda:{"verified":True}),
        ]
        for target, valid in candidates:
            with self.subTest(target=target):
                self.assertEqual(await self.hint(target,valid=valid),"")
        def raises():
            raise RuntimeError("verification unavailable")
        self.assertEqual(await self.hint(valid=raises),"")
        self.generate.assert_not_awaited()
        self.assertEqual(self.state.state()["thought_count"],0)

    async def test_observe_never_adds_outward_text_and_disabled_does_not_call(self):
        self.generate.return_value = response("say")
        result = await self.hint(settings=config(mode="observe"))
        self.assertEqual(result,"")
        self.assertEqual(self.runtime.last_status,"observed")
        self.assertEqual(self.generate.await_count,1)
        result = await self.hint(settings=config(enabled=False))
        self.assertEqual(result,"")
        self.assertEqual(self.generate.await_count,1)

    async def test_reply_and_proactive_calls_share_persistent_budget(self):
        self.settings.max_daily_model_calls = 1
        self.runtime.capture({"platform":"qq","session_id":"inbound","message_id":"inbound-one",
            "processed_plain_text":"缓存","account_id":"99999999",
            "message_info":{"user_info":{"user_id":OWNER},"group_info":None}},
            private_users=[OWNER],group_ids=[])
        await self.runtime.reply_hint({"session_id":"inbound"},self.settings,
            recall=lambda query:copy.deepcopy(self.memories),generate=self.generate,
            task_name="utils",timezone="Asia/Shanghai")
        self.assertEqual(self.generate.await_count,1)
        result = await self.hint(context(group=True))
        self.assertEqual(self.generate.await_count,1)
        self.assertEqual(self.runtime.last_status,"budget_or_cooldown")
        self.assertIn("continuous-self",result)
        self.assertNotIn("unified-expression",result)
        self.assertNotIn(RAW,result)
        self.state.close()
        self.state = SelfState(self.path)
        self.assertEqual(self.state.state()["thought_count"],1)

    async def test_budget_exhausted_before_review_cannot_release_generalization(self):
        self.settings.max_daily_model_calls = 1
        self.generate.return_value = response("generalize")
        result = await self.hint(context(group=True))
        self.assertEqual(self.generate.await_count,1)
        self.assertNotIn(GENERAL,result)
        self.assertNotIn(RAW,result)
        self.assertEqual(self.runtime.last_counts["generalized"],0)

    async def test_changes_during_await_cancel_owner_config_target_and_source(self):
        for change in ("owner","mode","disable","reset","verified","sid","source"):
            with self.subTest(change=change):
                self.state.configure_owner(OWNER)
                self.runtime = UnifiedRuntime(self.state)
                self.settings = config()
                self.memories = [memory()]
                target = context(group=True)
                flag = [True]
                async def generate(*args,**kwargs):
                    if change == "owner": self.state.configure_owner(OTHER)
                    elif change == "mode": self.settings.mode="observe"
                    elif change == "disable": self.settings.enabled=False
                    elif change == "reset": self.runtime.reset()
                    elif change == "verified": flag[0]=False
                    elif change == "sid": target["sid"]="changed-stream"
                    else: self.memories[0]["timestamp"]+=1
                    return response("say")
                result = await self.hint(target,valid=lambda:flag[0],generate=generate)
                self.assertEqual(result,"")
                self.assertEqual(self.runtime.last_status,"context_changed")

    async def test_changes_during_second_review_cancel_generalized_result(self):
        calls=0
        async def generate(*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls==1:
                return response("generalize")
            self.memories[0]["timestamp"]+=1
            return {"content":'{"approved_ids":["brain:fact:F1"]}'}
        result=await self.hint(context(group=True),generate=generate)
        self.assertEqual(calls,2)
        self.assertEqual(result,"")
        self.assertEqual(self.runtime.last_status,"context_changed")

    async def test_failure_returns_only_fixed_base_and_never_provider_echo(self):
        self.generate.side_effect=TimeoutError("provider echoed "+RAW)
        result=await self.hint(context(group=True))
        self.assertIn("continuous-self",result)
        self.assertNotIn(RAW,result)
        self.assertNotIn("unified-expression",result)
        self.assertEqual(self.runtime.last_status,"model_failed")
        self.assertEqual(self.state.state()["thought_count"],1)

    async def test_no_inbound_cache_or_legacy_reply_callback_side_effects(self):
        self.runtime._contexts={"sentinel":{"unchanged":True}}
        self.runtime._private={"sentinel":"unchanged"}
        self.runtime._cache={"sentinel":(time.monotonic(),"unchanged")}
        before=copy.deepcopy((self.runtime._contexts,self.runtime._private,self.runtime._cache))
        self.runtime.capture=Mock(side_effect=AssertionError("fabricated incoming message"))
        self.runtime.context=Mock(side_effect=AssertionError("inbound context consulted"))
        self.runtime.reply_hint=AsyncMock(side_effect=AssertionError("legacy reply path used"))
        result=await self.hint(context(group=True))
        self.assertIn("unified-expression",result)
        self.assertEqual((self.runtime._contexts,self.runtime._private,self.runtime._cache),before)
        self.runtime.capture.assert_not_called()
        self.runtime.context.assert_not_called()
        self.runtime.reply_hint.assert_not_awaited()

    async def test_proactive_group_support_does_not_relax_normal_reply_authentication(self):
        raw=response("internal")
        ordinary=parse_expression(raw,self.memories,audience_scope="group:"+GROUP,speaker_id="",is_owner=False)
        proactive=parse_expression(raw,self.memories,audience_scope="group:"+GROUP,speaker_id="",is_owner=False,proactive=True)
        self.assertIsNone(ordinary)
        self.assertIsNotNone(proactive)


if __name__=="__main__":
    unittest.main()
