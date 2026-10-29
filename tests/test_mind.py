"""内在反思：记忆持久化、低频门控、私聊隔离与隐私过滤。"""
from __future__ import annotations

import json
import tempfile
import time
import tomllib
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from MaiLivingWorld.tests.test_runtime import plugin
from MaiLivingWorld.mind import MindStore, parse_reflection

ROOT=Path(__file__).resolve().parents[1]


class MindPolicyTests(unittest.TestCase):
    def test_config_matches_schema_defaults(self):
        data=tomllib.loads((ROOT/"config.example.toml").read_text(encoding="utf-8"))
        self.assertEqual(data, plugin.Config().model_dump())
        self.assertFalse(data["mind"]["enabled"])
        self.assertFalse(data["mind"]["auto_reflect"])

    def test_schema_and_sensitive_filtering(self):
        obj={"notes":[
          {"kind":"preference","basis":"user_explicit","text":"主人明确表示喜欢讨论AI工作站搭建的细节","confidence":0.92},
          {"kind":"boundary","basis":"conversation","text":"主人讨厌任何人","confidence":0.99},
          {"kind":"preference","basis":"user_explicit","text":"主人的密码是password12345","confidence":1.0},
          {"kind":"open_topic","basis":"conversation","text":"上次讨论的工作站散热还没解决","confidence":0.6}
        ], "behavior_goal":"技术讨论时先问具体环境，别无意义夸赞"}
        notes, goal=parse_reflection({"content":json.dumps(obj,ensure_ascii=False)})
        self.assertEqual(len(notes),1)
        self.assertEqual(notes[0]["kind"],"preference")
        self.assertIn("技术",goal)
        self.assertEqual(parse_reflection({"content":"not json"}),([], ""))

    def test_persist_isolation_dedup_and_clear(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"mind.sqlite3"
            one=MindStore(path)
            note={"kind":"preference","text":"主人明确说他喜欢讨论深度学习","basis":"user_explicit","confidence":0.92}
            one.mark_attempt("11111111",time.time())
            one.save_reflection("11111111","h1",[note],"遇到问题要先分析上下文",time.time(),20)
            self.assertIn("深度学习",one.private_hint("11111111"))
            self.assertEqual(one.private_hint("22222222"),"")
            self.assertFalse(one.should_reflect("11111111","h1",time.time(),12))
            one.close()
            two=MindStore(path)
            self.assertEqual(len(two.notes("11111111")),1)
            two.clear("11111111")
            self.assertEqual(two.notes("11111111"),[])
            two.close()


class MindRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_reflection_owner_private_only(self):
        with tempfile.TemporaryDirectory() as d:
            bot=plugin.MaiLivingWorld()
            bot.config=plugin.Config()
            bot.config.owner.enabled=True
            bot.config.owner.qq="12345678"
            bot.config.mind.enabled=True
            bot.config.mind.auto_reflect=True
            history="主人明确表示喜欢学习深度学习课程，提到最近正在研究RNN。"*14
            model_data={"notes":[
              {"kind":"preference","basis":"user_explicit","text":"主人明确表示最近喜欢研究RNN与深度学习","confidence":0.95}
            ],"behavior_goal":"遇到技术难题时优先一起找具体例子"}
            model=types.SimpleNamespace(generate=AsyncMock(return_value={"content":json.dumps(model_data,ensure_ascii=False)}))
            bot.ctx=types.SimpleNamespace(
                paths=types.SimpleNamespace(data_dir=Path(d)),
                chat=types.SimpleNamespace(get_stream_by_user_id=AsyncMock(return_value={"session_id":"private-one"})),
                message=types.SimpleNamespace(
                    get_recent=AsyncMock(return_value=[{"id":i} for i in range(10)]),
                    build_readable=AsyncMock(return_value=history)),
                llm=model,logger=types.SimpleNamespace(info=lambda *a,**k:None,warning=lambda *a,**k:None))
            await bot._reflect_owner()
            self.assertEqual(model.generate.await_count,1)
            await bot._reflect_owner()
            self.assertEqual(model.generate.await_count,1)
            bot._owner_events["private-one"]=(time.monotonic(),True,"private","12345678")
            self.assertIn("RNN",bot._owner_prompt_for({"session_id":"private-one"}))
            bot._owner_group_messages[("group-one","owner-msg")]=(time.monotonic(),True,"12345678")
            self.assertNotIn("RNN",bot._owner_prompt_for({"session_id":"group-one","reply_message_id":"owner-msg"}))
            bot.config.mind.enabled=False
            self.assertNotIn("RNN",bot._owner_prompt_for({"session_id":"private-one"}))
            bot._mind.close()
            bot._mind=None

if __name__=="__main__":
    unittest.main()
