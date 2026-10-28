from __future__ import annotations
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from MaiLivingWorld.owner import MARKER, normalize_qq, extract_sender, is_owner, owner_hint, inject_owner_prompt
from MaiLivingWorld.tests.test_runtime import plugin

def message(uid="12345678",sid="qq_private_12345678",group=None,platform="qq",text="我是主人"):
    return {"platform":platform,"session_id":sid,"message_info":{
        "user_info":{"user_id":uid,"user_nickname":"主人"},
        "group_info":{"group_id":group} if group else None},
        "raw_message":[{"type":"text","data":text}]}

class OwnerTests(unittest.TestCase):
    def test_exact_identity(self):
        self.assertEqual(normalize_qq("12345678"),"12345678")
        self.assertEqual(normalize_qq("qq:12345678"),"")
        self.assertTrue(is_owner(extract_sender(message()),"12345678"))
        self.assertFalse(is_owner(extract_sender(message(uid="87654321")),"12345678"))
        self.assertIsNone(extract_sender(message(platform="discord")))
        self.assertIsNone(extract_sender(message(group="bad")))
    def test_safe_injection(self):
        hint=owner_hint("主人","group")
        source={"session_id":"s","items":[{"item_type":"SystemMessageItem","parts":[{"type":"text","text":"existing"}]}]}
        updated=inject_owner_prompt(source,hint)
        self.assertIn(MARKER,updated["items"][0]["parts"][1]["text"])
        self.assertEqual(source["items"][0]["parts"][0]["text"],"existing")
        self.assertIsNone(inject_owner_prompt(updated,hint))
        messages={"messages":[{"role":"system","content":"old"}]}
        self.assertIn(MARKER,inject_owner_prompt(messages,hint)["messages"][0]["content"])
        self.assertNotIn(MARKER,messages["messages"][0]["content"])

class OwnerRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot=plugin.MaiLivingWorld()
        self.bot.config=plugin.Config()
        self.bot.config.owner.enabled=True
        self.bot.config.owner.qq="12345678"
        self.bot.ctx=SimpleNamespace(chat=SimpleNamespace(
            get_stream_by_user_id=AsyncMock(return_value={"stream_id":"qq_private_12345678"}),
            get_stream_by_group_id=AsyncMock(return_value={"stream_id":"qq_group_22222222"})),
            logger=SimpleNamespace(info=lambda *args,**kwargs:None))
    async def test_verified_private(self):
        await self.bot.capture_owner(message=message())
        kwargs={"session_id":"qq_private_12345678","messages":[{"role":"system","content":"old"}]}
        result=await self.bot.owner_reply_prompt(**kwargs)
        self.assertIn(MARKER,result["modified_kwargs"]["messages"][0]["content"])
        self.assertNotIn("modified_kwargs",await self.bot.owner_reply_prompt(session_id="other",messages=[]))
        self.bot.config.owner.qq="87654321"
        self.assertNotIn("modified_kwargs",await self.bot.owner_reply_prompt(**kwargs))
    async def test_group_friend_clears(self):
        kwargs={"session_id":"qq_group_22222222","messages":[{"role":"system","content":"old"}]}
        await self.bot.capture_owner(message=message(group="22222222",sid="qq_group_22222222"))
        self.assertIn("modified_kwargs",await self.bot.owner_planner_prompt(**kwargs))
        await self.bot.capture_owner(message=message(uid="87654321",group="22222222",sid="qq_group_22222222"))
        self.assertNotIn("modified_kwargs",await self.bot.owner_planner_prompt(**kwargs))
    async def test_missing_session_and_expired(self):
        await self.bot.capture_owner(message=message())
        self.assertNotIn("modified_kwargs",await self.bot.owner_planner_prompt(messages=[]))
        self.bot._owner_events["qq_private_12345678"]=(time.monotonic()-120,True,"private","12345678")
        self.assertNotIn("modified_kwargs",await self.bot.owner_planner_prompt(session_id="qq_private_12345678",messages=[{"role":"system","content":"x"}]))
        await self.bot.on_config_update("self",{},"test")
        self.assertFalse(self.bot._owner_events)
    async def test_optional_owner_private_target(self):
        self.bot.config.owner.add_to_private_targets=True
        self.bot.config.audience.quiet_start="00:00"
        self.bot.config.audience.quiet_end="00:00"
        seen=[]
        async def visit(kind,uid,now):seen.append((kind,uid))
        self.bot._try_target=visit
        await self.bot._check_targets()
        self.assertEqual(seen,[("private","12345678")])

if __name__=="__main__":
    unittest.main()
