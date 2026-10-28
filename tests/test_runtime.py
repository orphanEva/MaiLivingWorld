from __future__ import annotations
import asyncio
import importlib
import tempfile
import types
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# 离线的 SDK API 形状模拟，不代表真实容器兼容测试。
from pydantic import BaseModel, Field
fake = types.ModuleType("maibot_sdk")
fake.API = lambda *a, **k: lambda f:f
fake.Tool = lambda *a, **k: lambda f:f
fake.HookHandler = lambda *a, **k: lambda f:f
fake_types = types.ModuleType("maibot_sdk.types")
fake_types.HookMode = types.SimpleNamespace(BLOCKING="blocking")
fake_types.HookOrder = types.SimpleNamespace(EARLY="early", NORMAL="normal")
fake_types.ErrorPolicy = types.SimpleNamespace(SKIP="skip")
sys.modules.setdefault("maibot_sdk.types",fake_types)
fake.Field = Field
fake.PluginConfigBase = BaseModel
fake.MaiBotPlugin = type("MaiBotPlugin",(object,),{})
sys.modules.setdefault("maibot_sdk",fake)
plugin = importlib.import_module("MaiLivingWorld.plugin")
from MaiLivingWorld.store import Store
from MaiLivingWorld.explore import Story


class FakeContext:
    def __init__(self):
        self.chat=types.SimpleNamespace(
            get_stream_by_user_id=AsyncMock(return_value={"stream_id":"private-one"}),
            get_stream_by_group_id=AsyncMock(return_value={"stream_id":"group-one"}))
        self.message=types.SimpleNamespace(
            get_by_time_in_chat=AsyncMock(return_value=[]),
            get_recent=AsyncMock(return_value=[{"a":1},{"b":2}]),
            build_readable=AsyncMock(return_value="昨天的 bug 解决没？"))
        self.llm=types.SimpleNamespace(generate=AsyncMock())
        self.maisaka=types.SimpleNamespace(proactive=types.SimpleNamespace(trigger=AsyncMock(return_value={"success":True})))
        self.call_capability=AsyncMock(return_value={"success":True})
        self.logger=types.SimpleNamespace(info=lambda *a,**k:None,debug=lambda *a,**k:None,warning=lambda *a,**k:None,error=lambda *a,**k:None,exception=lambda *a,**k:None)


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_dryrun_then_real_and_budget(self):
        with tempfile.TemporaryDirectory() as d:
            bot=plugin.MaiLivingWorld()
            bot.ctx=FakeContext()
            bot.config=plugin.Config()
            bot.config.general.enabled=True
            bot.config.general.dry_run=True
            bot.config.audience.private_users=["123"]
            bot.config.audience.quiet_start="00:00"
            bot.config.audience.quiet_end="00:00"
            bot.config.explorer.enabled=True
            bot.config.audience.history_gate=False
            bot._store=Store(Path(d)/"store.db")
            story=Story("key1","B站热门","测试视频","https://www.bilibili.com/video/BV1234567890","有趣的内容",time.time())
            bot._store.save_stories([story])
            bot.ctx.llm.generate.return_value={"content":'{"share":true,"key":"key1"}'}
            from datetime import datetime
            from zoneinfo import ZoneInfo
            now=datetime.now(ZoneInfo("Asia/Shanghai"))
            await bot._try_target("private","123",now)
            self.assertEqual(bot.ctx.maisaka.proactive.trigger.await_count,0)
            self.assertEqual(bot._store.statistics()["wake_attempts"],0)
            bot.config.general.dry_run=False
            await bot._try_target("private","123",now)
            self.assertEqual(bot.ctx.maisaka.proactive.trigger.await_count,1)
            self.assertEqual(bot.ctx.call_capability.await_count,1)
            self.assertEqual(bot._store.statistics(),{"stories":1,"wake_attempts":1,"memory_saved":1})
            await bot._try_target("private","123",now)
            self.assertEqual(bot.ctx.maisaka.proactive.trigger.await_count,1)
            self.assertEqual(bot._store.statistics()["wake_attempts"],1)
            bot._store.close()

if __name__=="__main__": unittest.main()
