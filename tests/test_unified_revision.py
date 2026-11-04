"""A changed evidence version invalidates approved text even when text is identical."""
from __future__ import annotations

import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from MaiLivingWorld.unified_runtime import UnifiedRuntime


OWNER = "12345678"
OTHER = "87654321"
GROUP = "22222222"


class Identity:
    def __init__(self):
        self.calls = 0

    def state(self):
        return {"owner_id": OWNER}

    def reserve_thought(self, **kwargs):
        self.calls += 1
        return True


def record():
    return {"id": "growth:knowledge:k:"+"a"*24, "source": "growth", "kind": "knowledge",
            "scope": "global:derived", "subject_id": OWNER,
            "text": "待验证知识：字典的 get 方法可提供默认值", "timestamp": 1000.0,
            "sensitivity": "personal", "evidence_status": "inferred", "revision": 1,
            "sources": [{"id": "brain:fact:F1", "digest": "a"*64,
                         "scope": "private:"+OWNER, "sensitivity": "personal"}]}


def changed(field):
    values = {"timestamp": 1001.0, "source": "brain_fact", "kind": "belief", "revision": 2,
              "sources": [{"id": "brain:fact:F1", "digest": "b"*64,
                           "scope": "private:"+OWNER, "sensitivity": "personal"}]}
    return copy.deepcopy(values[field])


def response(mode="internal"):
    decision = {"id": record()["id"], "mode": mode}
    if mode == "generalize":
        decision["text"] = "字典的 get 方法支持设置默认返回值"
    return {"content": json.dumps({"behavior": "patient", "decisions": [decision]}, ensure_ascii=False)}


class UnifiedRevisionTests(unittest.IsolatedAsyncioTestCase):
    def setup_runtime(self):
        identity = Identity()
        runtime = UnifiedRuntime(identity)
        runtime.capture({"platform": "qq", "session_id": "group-one", "message_id": "message-one",
            "account_id": "99999999", "processed_plain_text": "字典如何提供默认值",
            "message_info": {"user_info": {"user_id": OTHER}, "group_info": {"group_id": GROUP}}},
            private_users=[], group_ids=[GROUP])
        config = SimpleNamespace(enabled=True, mode="active", min_interval_seconds=0,
                                 max_daily_model_calls=60, model_timeout_seconds=2)
        return runtime, config, identity

    async def invoke(self, runtime, config, memories, generate):
        return await runtime.reply_hint({"session_id": "group-one", "reply_message_id": "message-one"},
            config, recall=lambda query: copy.deepcopy(memories), generate=generate,
            task_name="utils", timezone="Asia/Shanghai")

    async def test_only_evidence_version_change_during_deliberation_discards_result(self):
        for field in ("timestamp", "source", "kind", "revision", "sources"):
            with self.subTest(field=field):
                runtime, config, identity = self.setup_runtime()
                memories = [record()]
                async def generate(*args, **kwargs):
                    memories[0][field] = changed(field)
                    return response()
                mock = AsyncMock(side_effect=generate)
                result = await self.invoke(runtime,config,memories,mock)
                self.assertEqual(result, "")
                self.assertEqual(runtime.last_status, "context_changed")
                self.assertEqual(mock.await_count, 1)
                self.assertEqual(identity.calls, 1)

    async def test_only_dependency_digest_change_during_review_discards_generalization(self):
        runtime, config, identity = self.setup_runtime()
        memories = [record()]
        calls = 0
        async def generate(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return response("generalize")
            memories[0]["sources"] = changed("sources")
            return {"content": json.dumps({"approved_ids": [record()["id"]]})}
        result = await self.invoke(runtime,config,memories,generate)
        self.assertEqual(calls, 2)
        self.assertEqual(identity.calls, 2)
        self.assertEqual(result, "")
        self.assertEqual(runtime.last_status, "context_changed")

    async def test_changed_evidence_version_bypasses_previous_cache(self):
        for field in ("timestamp", "source", "kind", "revision", "sources"):
            with self.subTest(field=field):
                runtime, config, identity = self.setup_runtime()
                memories = [record()]
                generate = AsyncMock(return_value=response())
                first = await self.invoke(runtime,config,memories,generate)
                self.assertIn("unified-expression",first)
                same = await self.invoke(runtime,config,memories,generate)
                self.assertEqual(first,same)
                self.assertEqual(generate.await_count,1)
                memories[0][field] = changed(field)
                refreshed = await self.invoke(runtime,config,memories,generate)
                self.assertIn("unified-expression",refreshed)
                self.assertEqual(generate.await_count,2)
                self.assertEqual(identity.calls,2)
                self.assertEqual(runtime.last_status,"ready")


if __name__ == "__main__":
    unittest.main()
