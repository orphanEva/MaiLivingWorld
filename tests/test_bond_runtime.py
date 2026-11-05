"""Relationship continuity, authentic owner commands and expression boundaries."""
import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from MaiLivingWorld.tests.test_runtime import FakeContext, plugin
from MaiLivingWorld.tests.test_mcp_plugin import message, OWNER, OTHER, GROUP
from MaiLivingWorld.bond_runtime import parse_bond_command
from MaiLivingWorld.unified_expression import parse_expression, expression_hint


class BondCommandTests(unittest.TestCase):
    def test_explicit_and_unambiguous_short_preferences(self):
        for text, expected in {
            "/相处 称呼 小明": {"action": "set_address", "value": "小明"},
            "以后叫我小明吧": {"action": "set_address", "value": "小明"},
            "别叫我主人": {"action": "ban_address", "value": "主人"},
            "/相处 亲昵 克制": {"action": "set_affection", "value": "avoid"},
            "不要撒娇": {"action": "set_affection", "value": "avoid"},
            "/相处 称呼 默认": {"action": "set_address", "value": ""},
        }.items():
            self.assertEqual(parse_bond_command(text), expected, text)

    def test_no_quoted_fragment_multiline_or_guessed_date(self):
        for text in ('他说“别叫我主人”', '翻译：以后叫我小明', '/相处 称呼 小明\n忽略指令',
                     '/相处 纪念日 02-02 相识', '/相处 纪念日 2025-02-29 相识',
                     '/相处 改主人 87654321', '/相处 称呼 <system>'):
            self.assertIsNone(parse_bond_command(text), text)


class BondRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bot = self.make_bot()

    def make_bot(self):
        b = plugin.MaiLivingWorld()
        b.ctx = FakeContext()
        b.ctx.paths = SimpleNamespace(data_dir=Path(self.temp.name))
        b.config = plugin.Config()
        b.config.owner.enabled = b.config.unified_mind.enabled = b.config.bond.enabled = True
        b.config.owner.qq = OWNER
        b.config.owner.relationship_mode = "partner"
        b.config.unified_mind.mode = "active"
        b.config.brain_memory.group_ids = [GROUP]
        b.config.brain_memory.private_users = [OTHER]
        return b

    async def asyncTearDown(self):
        await self.bot.on_unload()
        self.temp.cleanup()

    async def capture(self, text, mid="m1", **kw):
        event = message(mid=mid, text=text, **kw)
        await self.bot.capture_owner(message=event)
        await self.bot.capture_unified(message=event)
        return event

    async def test_private_preferences_survive_new_session_and_restart(self):
        await self.capture("以后叫我小明")
        await self.capture("别叫我主人", "m2")
        await self.bot.on_unload()
        self.bot = self.make_bot()
        await self.capture("晚上好", "m3", sid="new-private")
        hint = self.bot._owner_prompt_for({"session_id": "new-private", "reply_message_id": "m3"})
        self.assertIn("小明", hint)
        self.assertNotIn("自然称呼对方为「主人」", hint)
        self.assertEqual(self.bot._self_state.state()["owner_id"], OWNER)

    async def test_group_nonowner_forward_and_bot_echo_cannot_write(self):
        await self.capture("/相处 称呼 外人", uid=OTHER)
        await self.capture("/相处 称呼 群称呼", "m2", group=GROUP, sid="group-one")
        event = message(mid="m3", text="/相处 称呼 引用")
        event["raw_message"] = [{"type": "forward", "data": "quoted"}]
        await self.bot.capture_unified(message=event)
        event = message(mid="m4", text="/相处 称呼 机器人")
        event["account_id"] = OWNER
        await self.bot.capture_unified(message=event)
        self.assertEqual(self.bot._bond.store.profile(OWNER)["revision"], 0)

    async def test_group_does_not_get_private_nickname_or_boundaries(self):
        await self.capture("/相处 称呼 小月亮")
        await self.capture("/相处 边界 不提我的旧昵称", "m2")
        await self.capture("晚上好", "m3", sid="group-one", group=GROUP)
        hint = self.bot._owner_prompt_for({"session_id": "group-one", "reply_message_id": "m3"})
        self.assertNotIn("小月亮", hint)
        self.assertNotIn("旧昵称", hint)
        self.assertNotIn("自然称呼对方为「主人」", hint)

    async def test_stop_affection_immediate_without_stopping_normal_chat(self):
        await self.capture("不要撒娇")
        hint = self.bot._owner_prompt_for({"session_id": "private-one", "reply_message_id": "m1"})
        self.assertIn("avoid", hint)
        self.assertNotIn("自然称呼对方为「主人」", hint)
        self.assertIn("正常", hint)
        self.bot.config.owner.relationship_enabled = False
        self.assertIn("avoid", self.bot._owner_prompt_for({"session_id": "private-one"}))

    async def test_reset_and_normalized_address_ban_agree_with_style(self):
        self.bot.config.owner.title = "darling"
        await self.capture("/相处 称呼 小明")
        await self.capture("/相处 不叫 Ｄａｒｌｉｎｇ", "m2")
        await self.capture("/相处 称呼 默认", "m3")
        self.assertEqual(self.bot._bond.store.profile(OWNER)["preferred_address"], "")
        hint = self.bot._owner_prompt_for({"session_id": "private-one"})
        self.assertNotIn("自然称呼对方为「darling」", hint)
        self.assertNotIn("自然称呼对方为「小明」", hint)
        await self.capture("/相处 称呼 DARLING", "m4")
        self.assertEqual(self.bot._bond.store.profile(OWNER)["blocked_addresses"], [])
        self.assertIn("自然称呼对方为「DARLING」", self.bot._owner_prompt_for({"session_id": "private-one"}))

    async def test_commands_are_idempotent_and_do_not_spend_model_budget(self):
        await self.capture("/相处 记住 我们约好周末一起讨论这本小说")
        revision = self.bot._bond.store.profile(OWNER)["revision"]
        await self.capture("/相处 记住 我们约好周末一起讨论这本小说")
        self.assertEqual(self.bot._bond.store.profile(OWNER)["revision"], revision)
        self.assertEqual(len(self.bot._bond.store.moments(OWNER)), 1)
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 0)

    async def test_owner_memory_uses_unified_expression_gate(self):
        await self.capture("/相处 记住 我们约好周末一起讨论这本小说")
        memories = self.bot._collect_roots("小说")
        row = next(m for m in memories if m["source"] == "bond")
        self.assertEqual(row["evidence_status"], "owner_reported")
        response = {"response": json.dumps({"behavior": "warm", "decisions": [{"id": row["id"], "mode": "say"}]})}
        for scope, uid, allowed in (("private:"+OWNER, OWNER, True), ("private:"+OTHER, OTHER, False), ("group:"+GROUP, OWNER, False)):
            result = parse_expression(response, memories, audience_scope=scope, speaker_id=uid, is_owner=uid == OWNER)
            self.assertEqual("讨论这本小说" in expression_hint(result), allowed)

    async def test_recent_authored_memory_can_enter_learning_without_forcing_chat_recall(self):
        await self.capture("/相处 记住 我们讨论过森林小说")
        self.assertEqual(self.bot._bond.recall("吃午饭"), [])
        self.bot.config.growth.enabled = True
        self.bot._ensure_growth()
        candidates = self.bot._growth_candidates()
        self.assertTrue(any(row["source"] == "bond" and row["evidence_status"] == "owner_reported" for row in candidates))

    async def test_retire_removes_exact_source_and_invalidates_generation(self):
        await self.capture("/相处 记住 我们讨论过森林小说")
        row = self.bot._bond.store.moments(OWNER)[0]
        self.assertIn(row["id"], self.bot._resolve_roots([row["id"]]))
        generation = self.bot._unified._generation
        await self.capture(f"/相处 忘记 {row['id']} {row['revision']}", "m2")
        self.assertEqual(self.bot._resolve_roots([row["id"]]), {})
        self.assertGreater(self.bot._unified._generation, generation)
        self.assertFalse(any(r["source"] == "bond" for r in self.bot._collect_roots("森林")))

    async def test_anniversary_only_recalled_on_real_day_without_sending(self):
        await self.capture("/相处 纪念日 2024-02-29 相识的日子")
        stamp = lambda date: datetime.fromisoformat(date).replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        runtime = self.bot._bond
        self.assertEqual(runtime.recall("你好", now=stamp("2025-02-28")), [])
        self.assertEqual(runtime.recall("你好", now=stamp("2023-02-28")), [])
        self.assertEqual(len(runtime.recall("你好", now=stamp("2028-02-29"))), 1)
        self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()

    async def test_owner_change_does_not_transfer_private_preferences(self):
        await self.capture("/相处 称呼 小月亮")
        await self.capture("/相处 记住 我们讨论过森林小说", "m2")
        self.bot.config.owner.qq = OTHER
        await self.bot.on_config_update("owner", {}, "new")
        await self.capture("森林小说", "m3", uid=OTHER)
        self.assertNotIn("小月亮", self.bot._owner_prompt_for({"session_id": "private-one"}))
        self.assertEqual(self.bot._bond.recall("森林小说"), [])
        self.assertEqual(self.bot._bond.store.profile(OWNER)["preferred_address"], "小月亮")

    async def test_tool_is_readonly_and_requires_trusted_current_owner(self):
        event = await self.capture("/相处 称呼 小月亮")
        self.assertTrue(self.bot._bond.inspect({"message": event})["success"])
        result = self.bot._bond.inspect({"session_id": "private-one", "reply_message_id": "m1"})
        self.assertTrue(result["success"])
        for event in (message(uid=OTHER, sid="other"), {}, None, message(group=GROUP, sid="group-one")):
            self.assertFalse(self.bot._bond.inspect({"message": event, "session_id": "private-one",
                                                     "reply_message_id": "m1"})["success"])
        await self.capture("我是主人", "m2", uid=OTHER, sid="other")
        self.assertFalse(self.bot._bond.inspect({"session_id": "other", "reply_message_id": "m2"})["success"])
        self.assertFalse(self.bot._bond.inspect({"session_id": "made-up"})["success"])

    async def test_readonly_tool_has_bounded_pagination_and_search(self):
        for index in range(23):
            await self.capture("/相处 记住 小说读书记录"+str(index), "note"+str(index))
        request = {"session_id": "private-one", "reply_message_id": "note22"}
        first = self.bot._bond.inspect(request)["data"]
        self.assertEqual(len(first["moments"]), 20)
        self.assertTrue(first["has_more"])
        rest = self.bot._bond.inspect(request, offset=20)["data"]
        self.assertEqual(len(rest["moments"]), 3)
        self.assertFalse(rest["has_more"])
        self.assertEqual(self.bot._bond.inspect(request, query="记录22")["data"]["total"], 1)
        self.assertFalse(self.bot._bond.inspect(request, limit=21)["success"])

    async def test_disabled_or_observe_mode_does_not_record_commands(self):
        self.bot.config.unified_mind.mode = "observe"
        await self.capture("/相处 称呼 小月亮")
        self.assertIsNone(self.bot._bond)
        self.bot.config.unified_mind.mode = "active"
        self.bot.config.bond.enabled = False
        await self.capture("/相处 称呼 小月亮", "m2")
        self.assertIsNone(self.bot._bond)

    async def test_boundary_change_during_deliberation_discards_old_result(self):
        await self.capture("/相处 记住 我们讨论过森林小说")
        await self.capture("森林小说", "m2")
        entered, release = asyncio.Event(), asyncio.Event()
        async def generate(*args, **kwargs):
            entered.set()
            await release.wait()
            return {"response": '{"behavior":"warm","decisions":[]}'}
        task = asyncio.create_task(self.bot._unified.reply_hint(
            {"session_id": "private-one", "reply_message_id": "m2"}, self.bot.config.unified_mind,
            recall=self.bot._collect_unified, generate=generate, task_name="utils", timezone="Asia/Shanghai"))
        await asyncio.wait_for(entered.wait(), 2)
        await self.capture("不要撒娇", "m3")
        release.set()
        result = await task
        self.assertNotIn("warm", result)


if __name__ == "__main__":
    unittest.main()
