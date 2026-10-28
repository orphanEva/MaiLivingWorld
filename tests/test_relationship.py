"""主人关系风格的离线验证，不需要真实 QQ 账户。"""
from __future__ import annotations
import sys
import time
import tempfile
import unittest
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from MaiLivingWorld.relationship import PRESETS, MAX_TEMPLATE_CHARS, relationship_note
from MaiLivingWorld.tests.test_runtime import plugin, FakeContext
from MaiLivingWorld.store import Store
from MaiLivingWorld.tests.test_owner import message


class RelationshipPolicyTests(unittest.TestCase):
    def test_presets_are_distinct(self):
        kinds = ['banter', 'gentle', 'tsundere']
        all_private = [relationship_note(mode, 'private') for mode in kinds]
        self.assertEqual(len(set(all_private)), 3)
        for mode in kinds:
            self.assertNotEqual(relationship_note(mode,'private'), relationship_note(mode, 'group'))
            self.assertIn('群聊特别规则', relationship_note(mode, 'group'))

    def test_custom_overrides_presets_and_is_scoped(self):
        self.assertIn('独家私聊', relationship_note('banter','private', '独家私聊', '群聊专用'))
        self.assertNotIn('群聊专用', relationship_note('banter','private', '独家私聊', '群聊专用'))
        self.assertIn('群聊专用', relationship_note('banter','group', '独家私聊', '群聊专用'))
        self.assertNotIn('独家私聊', relationship_note('banter','group', '独家私聊', '群聊专用'))

    def test_custom_mode_empty_does_not_generate_unasked_style(self):
        self.assertEqual(relationship_note('custom','private'), '')
        self.assertEqual(relationship_note('custom','group'), '')
        self.assertEqual(relationship_note('banter','unknown'), '')

    def test_long_custom_prompt_clamped(self):
        note=relationship_note('custom','private', '爱'*5000)
        self.assertEqual(note.count('爱'), MAX_TEMPLATE_CHARS)


class RelationshipRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot=plugin.MaiLivingWorld()
        self.bot.config=plugin.Config()
        self.bot.config.owner.enabled=True
        self.bot.config.owner.qq='12345678'
        self.bot.config.owner.relationship_mode='tsundere'
        self.bot.ctx=FakeContext()

    async def test_owner_incoming_private_and_group_are_different(self):
        await self.bot.capture_owner(message=message(sid='private-one'))
        kwargs={'session_id':'private-one','messages':[{'role':'system','content':'original'}]}
        x=await self.bot.owner_reply_prompt(**kwargs)
        prompt=x['modified_kwargs']['messages'][0]['content']
        self.assertIn('嘴硬',prompt)
        self.assertNotIn('群聊特别规则',prompt)
        self.bot.config.owner.group_template='群聊限定的小暗号'
        await self.bot.capture_owner(message=message(group_id='22222222', sid='group-one'))
        y=await self.bot.owner_planner_prompt(session_id='group-one',messages=[{'role':'system','content':'original'}])
        group=y['modified_kwargs']['messages'][0]['content']
        self.assertIn('群聊限定的小暗号',group)
        self.assertNotIn('嘴硬',group)
        self.assertNotIn('群聊限定的小暗号',prompt)
        await self.bot.capture_owner(message=message(user_id='87654321', group_id='22222222',sid='group-one'))
        z=await self.bot.owner_planner_prompt(session_id='group-one',messages=[{'role':'system','content':'original'}])
        self.assertNotIn('modified_kwargs',z)

    async def test_disabling_style_keeps_recognition(self):
        await self.bot.capture_owner(message=message(sid='private-one'))
        self.bot.config.owner.relationship_enabled=False
        result=await self.bot.owner_reply_prompt(session_id='private-one',messages=[{'role':'system','content':'original'}])
        prompt=result['modified_kwargs']['messages'][0]['content']
        self.assertIn('经 QQ 平台用户 ID 验证',prompt)
        self.assertNotIn('嘴硬',prompt)
        self.bot.config.owner.enabled=False
        self.assertEqual(self.bot._owner_prompt_for({'session_id':'private-one'}),'')

    async def test_proactive_owner_only(self):
        async def check(kind,ident):
            with tempfile.TemporaryDirectory() as d:
                bot=plugin.MaiLivingWorld()
                bot.config=plugin.Config()
                bot.config.owner.enabled=True
                bot.config.owner.qq='12345678'
                bot.config.owner.relationship_mode='gentle'
                bot.config.general.dry_run=False
                bot.config.memory.enabled=False
                bot.config.audience.min_gap_hours=0
                bot.config.audience.history_gate=False
                bot.ctx=FakeContext()
                bot._store=Store(Path(d)/'test.sqlite3')
                story={'key':'sample','source':'test','title':'一条新闻','summary':'公开标题简介','url':'https://example.com/x'}
                async def choose_story(target): return story
                bot._choose_story=choose_story
                await bot._try_target(kind,ident,datetime.now(ZoneInfo('Asia/Shanghai')))
                args=bot.ctx.maisaka.proactive.trigger.await_args
                self.assertIsNotNone(args)
                prompt=args.args[1]
                bot._store.close()
                return prompt
        owner=await check('private','12345678')
        stranger=await check('private','87654321')
        group=await check('group','22222222')
        self.assertIn('面对主人会更放松',owner)
        self.assertNotIn('面对主人会更放松',stranger)
        self.assertNotIn('面对主人会更放松',group)


if __name__=='__main__': unittest.main()
