"""Actual source, identity, context and topic stores with offline send receipts."""
import asyncio
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from MaiLivingWorld.growth_store import source_fingerprint
from MaiLivingWorld.reference_runtime import ReferenceRuntime, is_followup
from MaiLivingWorld.self_state import SelfState
from MaiLivingWorld.tool_store import ToolStore
from MaiLivingWorld.unified_runtime import UnifiedRuntime
from test_reference_store import OWNER, OTHER


class Settings(SimpleNamespace):
    def model_dump(self):
        return dict(vars(self))


def message(mid="m1", *, uid=OWNER, sid="private-one", group=None, text="聊聊明天的编程面试"):
    return {"platform": "qq", "message_id": mid, "session_id": sid, "account_id": "99999999",
        "processed_plain_text": text, "message_info": {"user_info": {"user_id": uid},
            "group_info": {"group_id": group} if group else None}}


class ReferenceRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        b = self.bot = SimpleNamespace()
        b.ctx = SimpleNamespace(paths=SimpleNamespace(data_dir=Path(self.temp.name)),
            chat=SimpleNamespace(get_stream_by_user_id=AsyncMock(return_value={"stream_id": "private-one"})))
        b.config = SimpleNamespace(plugin=Settings(enabled=True), owner=Settings(enabled=True, qq=OWNER),
            reference=Settings(enabled=True, ttl_hours=24), unified_mind=Settings(enabled=True, mode="active"))
        b._mcp_generation = 0
        b._ledger_storage_options = lambda: {}
        b._self_state = SelfState(Path(self.temp.name)/"self.sqlite3")
        b._self_state.configure_owner(OWNER)
        b._unified = UnifiedRuntime(b._self_state)
        b._unified.latest_private_only = True
        b._unified_active = lambda: b.config.unified_mind.enabled and b.config.unified_mind.mode == "active"
        b._tool_store = ToolStore(Path(self.temp.name)/"tools.sqlite3")
        b._resolve_roots = lambda ids: {row["id"]: row for row in b._tool_store.resolve(ids)}
        b._expression_records = lambda records, context=None: records
        self.runtime = ReferenceRuntime(b)
        self.first = self.source("first", "明天下午我准备参加一个编程岗位面试。")
        self.second = self.source("second", "周末我打算坐高铁去海边看日出。")

    def source(self, key, text, *, owner=OWNER):
        return self.bot._tool_store.put_observation("cap", key, text, "private:"+owner, subject_id=owner)

    def capture(self, **kw):
        event = message(**kw)
        return self.capture_event(event)

    def capture_event(self, event):
        b = self.bot
        b._unified.capture(event, private_users=[OWNER, OTHER], group_ids=["22222222"])
        if self.runtime.capture(event):
            b._unified.reset()
            b._unified.capture(event, private_users=[OWNER, OTHER], group_ids=["22222222"])
        return {"message": event}

    async def sent(self, text, *, mid="out", sid="private-one", success=True, **kwargs):
        return await self.runtime.sent({"sent": success, "message": message(mid, uid="99999999", sid=sid, text=text, **kwargs)})

    async def focus(self, *, records=None):
        rows = records or [self.first]
        context = self.capture()
        self.runtime.stage(context, rows)
        self.assertTrue(await self.sent("我记得你说过："+" ".join(row["text"] for row in rows)))

    async def asyncTearDown(self):
        self.runtime.close()
        self.bot._tool_store.close()
        self.bot._self_state.close()
        self.temp.cleanup()

    async def test_successful_literal_mention_resolves_across_restart_and_new_session(self):
        context = self.capture()
        self.runtime.stage(context, [self.first])
        self.assertFalse(await self.sent(self.first["text"], success=False))
        self.assertEqual(self.runtime.store.state(OWNER)["focus_refs"], [])
        self.assertTrue(await self.sent(self.first["text"]))
        self.runtime.close()
        self.runtime = ReferenceRuntime(self.bot)
        kwargs = self.capture(mid="later", sid="new-private", text="后来呢？")
        before = self.runtime.store.state(OWNER)
        resolved = self.runtime.resolve(kwargs)
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual([row["id"] for row in resolved["records"]], [self.first["id"]])
        self.assertIn("编程岗位面试", resolved["query"])
        self.assertNotIn("编程岗位面试", resolved["hint"])
        self.assertEqual(self.runtime.resolve(kwargs), resolved)
        self.assertEqual(self.runtime.store.state(OWNER), before)

    async def test_reading_reaction_preserves_only_a_verified_mentioned_source(self):
        await self.focus()
        self.runtime.close()
        self.runtime = ReferenceRuntime(self.bot)
        for index, text in enumerate(("你觉得它哪里有意思？", "你怎么看刚才读的？")):
            event = message("reaction-"+str(index), sid="return-private", text=text)
            event["raw_message"] = [{"type": "text", "data": text}]
            result = self.runtime.resolve(self.capture_event(event))
            self.assertEqual(result["status"], "resolved")
            self.assertEqual([r["id"] for r in result["records"]], [self.first["id"]])
            self.assertIn("还没形成看法", result["hint"])
            self.assertIn("自己的判断", result["hint"])
        self.source("first", "此前的来源已经修订。")
        event = message("after-change", text="你怎么看刚才读的？")
        event["raw_message"] = [{"type": "text", "data": event["processed_plain_text"]}]
        self.assertEqual(self.runtime.resolve(self.capture_event(event))["status"], "unavailable")

    async def test_reaction_does_not_infer_an_unmentioned_ambiguous_or_quoted_subject(self):
        text = "你觉得它哪里有意思"
        def event(mid):
            return {**message(mid, text=text), "raw_message": [{"type": "text", "data": text}]}
        self.assertEqual(self.runtime.resolve(self.capture_event(event("no-focus")))["status"], "unavailable")
        await self.focus(records=[self.first, self.second])
        self.assertEqual(self.runtime.resolve(self.capture_event(event("two-focus")))["status"], "ambiguous")
        for kind in ("forward", "reply", "different", "missing"):
            context = self.capture(mid="new-focus-"+kind)
            self.runtime.stage(context, [self.first])
            self.assertTrue(await self.sent(self.first["text"], mid="new-sent-"+kind))
            quoted = event("quoted-"+kind)
            if kind == "forward":
                quoted["raw_message"] = [{"type": "forward", "data": text}]
            elif kind == "reply":
                quoted["reply_to"] = "earlier-message"
            elif kind == "different":
                quoted["raw_message"] = [{"type": "text", "data": "主人引用了别人的问句"}]
            else:
                quoted.pop("raw_message")
            result = self.runtime.resolve(self.capture_event(quoted))
            self.assertEqual(result["status"], "unavailable", kind)
            self.assertNotIn("自己的判断", result["hint"])

    async def test_multiple_mentions_require_clarification_without_source_text(self):
        await self.focus(records=[self.first, self.second])
        result = self.runtime.resolve(self.capture(mid="later", text="那件事呢"))
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(result["records"], [])
        self.assertNotIn("面试", result["hint"])
        self.assertNotIn("日出", result["hint"])

    async def test_split_reply_accumulates_actual_topics_and_duplicate_chunk_does_not_renew(self):
        context = self.capture()
        self.runtime.stage(context, [self.first, self.second])
        self.assertTrue(await self.sent(self.first["text"], mid="part-one"))
        first = self.runtime.store.state(OWNER)
        self.assertEqual(len(first["focus_refs"]), 1)
        self.assertFalse(await self.sent(self.first["text"], mid="part-one"))
        self.assertEqual(self.runtime.store.state(OWNER), first)
        self.runtime.stage(context, [self.second])
        self.assertTrue(await self.sent(self.second["text"], mid="part-two"))
        self.assertEqual(len(self.runtime.store.state(OWNER)["focus_refs"]), 2)
        self.assertEqual(self.runtime.resolve(self.capture(mid="followup", text="那个呢"))["status"], "ambiguous")

    async def test_split_reply_old_second_chunk_cannot_restore_cleared_or_new_topic(self):
        for clear_text in ("/话题 清除", "我们来聊聊今天的天气"):
            with self.subTest(clear_text=clear_text):
                suffix = str(len(clear_text))
                context = self.capture(mid="start"+suffix)
                self.runtime.stage(context, [self.first, self.second])
                self.assertTrue(await self.sent(self.first["text"], mid="one"+suffix))
                self.capture(mid="interrupt"+suffix, text=clear_text)
                self.assertFalse(await self.sent(self.second["text"], mid="two"+suffix))
                self.assertEqual(self.runtime.store.state(OWNER)["focus_refs"], [])

    async def test_concurrent_split_receipts_accumulate_without_losing_second_topic(self):
        context = self.capture()
        self.runtime.stage(context, [self.first, self.second])
        async def destination(*args, **kwargs):
            await asyncio.sleep(0)
            return {"stream_id": "private-one"}
        self.bot.ctx.chat.get_stream_by_user_id.side_effect = destination
        results = await asyncio.gather(self.sent(self.first["text"], mid="chunk-a"), self.sent(self.second["text"], mid="chunk-b"))
        self.assertEqual(results, [True, True])
        self.assertEqual(len(self.runtime.store.state(OWNER)["focus_refs"]), 2)

    async def test_queued_old_chunk_cannot_attach_to_new_reply_even_with_same_source(self):
        context = self.capture()
        self.runtime.stage(context, [self.first])
        await self.runtime._send_lock.acquire()
        queued = asyncio.create_task(self.sent(self.first["text"], mid="old-queued"))
        try:
            await asyncio.sleep(0)
            self.capture(mid="clear", text="/话题 清除")
            newer = self.capture(mid="new-question", text="重新聊一下明天的面试")
            self.runtime.stage(newer, [self.first])
        finally:
            self.runtime._send_lock.release()
        self.assertFalse(await queued)
        self.bot.ctx.chat.get_stream_by_user_id.assert_not_awaited()
        self.assertEqual(self.runtime.store.state(OWNER)["focus_refs"], [])
        self.assertTrue(await self.sent(self.first["text"], mid="actual-new-reply"))

    async def test_split_reply_source_revision_and_original_window_expiry_reject_late_chunk(self):
        context = self.capture()
        self.runtime.stage(context, [self.first, self.second])
        self.assertTrue(await self.sent(self.first["text"], mid="part-one"))
        before = self.runtime.store.state(OWNER)
        self.source("second", "原来的海边计划取消，这是一条更新后的记录。")
        self.assertFalse(await self.sent(self.second["text"], mid="stale-two"))
        self.assertEqual(self.runtime.store.state(OWNER), before)
        with patch("time.monotonic", return_value=time.monotonic()+91):
            self.assertFalse(await self.sent(self.first["text"], mid="expired-two"))
        self.assertEqual(self.runtime.store.state(OWNER), before)

    async def test_paraphrase_or_generic_reply_does_not_claim_a_topic_was_mentioned(self):
        context = self.capture()
        self.runtime.stage(context, [self.first])
        self.assertFalse(await self.sent("祝你一切顺利，加油哦！"))
        self.assertEqual(self.runtime.resolve(self.capture(mid="later", text="继续说"))["status"], "unavailable")

    async def test_new_topic_and_clear_invalidate_old_stage_and_old_input_replay(self):
        await self.focus()
        followup = self.capture(mid="later", text="后来呢")
        self.runtime.stage(followup, [self.first])
        self.capture(mid="clear", text="/话题 清除")
        self.assertFalse(await self.sent(self.first["text"], mid="late"))
        self.assertFalse(self.runtime.capture(followup["message"]))
        self.assertEqual(self.runtime.store.state(OWNER)["focus_refs"], [])
        self.assertEqual(self.runtime.resolve(self.capture(mid="new", text="你觉得茶叶怎么泡"))["status"], "normal")

    async def test_expired_or_revised_source_never_resolves_old_reference(self):
        await self.focus()
        with patch("time.time", return_value=time.time()+25*3600):
            self.assertEqual(self.runtime.resolve(self.capture(mid="expired", text="后来呢"))["status"], "unavailable")
        self.source("first", "面试已经取消，不再是原来的安排。")
        self.assertEqual(self.runtime.resolve(self.capture(mid="changed", text="那件事"))["status"], "unavailable")

    async def test_shortened_ttl_uses_actual_mention_time_not_followup_capture_time(self):
        await self.focus()
        mentioned_at = self.runtime.store.state(OWNER)["mentioned_at"]
        with patch("time.time", return_value=mentioned_at+2*3600):
            context = self.capture(mid="later", text="后来呢")
            self.assertEqual(self.runtime.resolve(context)["status"], "resolved")
            self.bot.config.reference.ttl_hours = 1
            self.assertEqual(self.runtime.resolve(context)["status"], "unavailable")
            self.assertFalse(self.runtime.inspect(context)["data"]["active"])

    async def test_return_greeting_does_not_erase_topic_or_extend_its_lifetime(self):
        await self.focus()
        old = self.runtime.store.state(OWNER)
        self.capture(mid="back", sid="new-private", text="我回来了")
        self.assertEqual(self.runtime.store.state(OWNER)["mentioned_at"], old["mentioned_at"])
        self.assertEqual(self.runtime.store.state(OWNER)["expires"], old["expires"])
        self.assertEqual(self.runtime.resolve(self.capture(mid="later", sid="new-private", text="上次那件事"))["status"], "resolved")

    async def test_other_private_source_and_group_context_never_gain_owner_focus(self):
        hidden = self.source("secret", "另一个人的私人面试还没有告诉任何人。", owner=OTHER)
        context = self.capture()
        self.runtime.stage(context, [hidden])
        self.assertFalse(await self.sent(hidden["text"]))
        self.assertEqual(self.runtime.resolve(self.capture(mid="group", group="22222222", text="后来呢"))["status"], "untrusted")
        self.assertEqual(self.runtime.resolve(self.capture(mid="other", uid=OTHER, text="后来呢"))["status"], "untrusted")

    async def test_source_or_protection_change_during_receipt_await_prevents_commit(self):
        context = self.capture()
        self.runtime.stage(context, [self.first])
        async def changed(*args, **kwargs):
            self.bot._expression_records = lambda records, context=None: []
            return {"stream_id": "private-one"}
        self.bot.ctx.chat.get_stream_by_user_id.side_effect = changed
        self.assertFalse(await self.sent(self.first["text"]))
        self.assertEqual(self.runtime.store.state(OWNER)["focus_refs"], [])

    async def test_new_input_during_receipt_await_prevents_commit(self):
        context = self.capture()
        self.runtime.stage(context, [self.first])
        async def changed(*args, **kwargs):
            self.capture(mid="interrupt", text="先不聊这个了")
            return {"stream_id": "private-one"}
        self.bot.ctx.chat.get_stream_by_user_id.side_effect = changed
        self.assertFalse(await self.sent(self.first["text"]))
        self.assertEqual(self.runtime.store.state(OWNER)["focus_refs"], [])

    async def test_unknown_media_preserves_reference_but_suspends_guessing_old_topic(self):
        await self.focus()
        context = self.capture(mid="later", text="后来呢")
        self.runtime.stage(context, [self.first])
        media = message("photo", text="后来呢")
        media["is_picture"] = True
        media_context = self.capture_event(media)
        self.runtime.stage(media_context, [self.first])
        self.assertFalse(await self.sent(self.first["text"], mid="late"))
        self.assertEqual(len(self.runtime.store.state(OWNER)["focus_refs"]), 1)
        self.assertEqual(self.runtime.resolve(self.capture(mid="after", text="那个呢"))["status"], "unavailable")
        self.runtime.close()
        self.runtime = ReferenceRuntime(self.bot)
        self.assertEqual(self.runtime.resolve(self.capture(mid="restart", text="那个呢"))["status"], "unavailable")

    async def test_command_interrupts_stage_without_guessing_or_discarding_saved_topic(self):
        await self.focus()
        context = self.capture(mid="later", text="后来呢")
        self.runtime.stage(context, [self.first])
        event = message("command", text="/其他命令")
        event["is_command"] = True
        command = self.capture_event(event)
        self.runtime.stage(command, [self.first])
        self.assertFalse(await self.sent(self.first["text"], mid="late"))
        self.assertEqual(self.runtime.resolve(self.capture(mid="after", text="继续说"))["status"], "resolved")

    async def test_duplicate_or_group_outbound_receipt_cannot_create_focus(self):
        context = self.capture()
        self.runtime.stage(context, [self.first])
        self.assertFalse(await self.sent(self.first["text"], group="22222222"))
        self.assertTrue(await self.sent(self.first["text"]))
        self.assertFalse(await self.sent(self.first["text"]))

    def marked(self, request, rows):
        event = request["message"]
        context = self.bot._unified.context({"session_id": event["session_id"], "reply_message_id": event["message_id"]})
        return [row["id"] for row in self.runtime.mark_prior_mentions(rows, context) if row.get("previously_mentioned")]

    async def test_only_actual_owner_delivery_confers_a_mention_across_session_and_restart(self):
        rows = [{**self.first, "previously_mentioned": True}, self.second]
        first = self.capture()
        self.runtime.stage(first, rows)
        self.assertEqual(self.marked(first, rows), [])
        self.assertFalse(await self.sent(self.first["text"], success=False))
        self.assertEqual(self.marked(first, rows), [])
        self.assertTrue(await self.sent(self.first["text"]))
        self.runtime.close()
        self.runtime = ReferenceRuntime(self.bot)
        new = self.capture(sid="new-private", mid="new", text="换个话题吧")
        self.assertEqual(self.runtime.store.state(OWNER)["focus_refs"], [])
        self.assertEqual(self.marked(new, rows), [self.first["id"]])
        self.assertEqual(self.marked(first, rows), [])
        self.assertEqual(self.marked(self.capture(mid="group", group="22222222"), rows), [])
        self.assertEqual(self.marked(self.capture(sid="other", mid="third", uid=OTHER), rows), [])
        self.bot.config.reference.enabled = False
        self.assertEqual(self.marked(new, rows), [])
        self.assertTrue(rows[0]["previously_mentioned"])

    async def test_mention_expiration_source_replacement_and_explicit_clear(self):
        await self.focus()
        later = self.capture(mid="later", text="聊聊编程")
        self.assertEqual(self.marked(later, [self.first]), [self.first["id"]])
        with patch("time.time", return_value=time.time()+31*86400):
            self.assertEqual(self.marked(later, [self.first]), [])
        revised = self.source("first", "我已经取消原来的安排，周末准备在家看书。")
        self.assertEqual(self.marked(later, [self.first]), [])
        self.assertEqual(self.marked(later, [revised]), [])
        self.runtime.stage(later, [revised])
        self.assertTrue(await self.sent(revised["text"], mid="new-send"))
        self.assertIn(source_fingerprint(revised), self.runtime.store.state(OWNER)["prior_mentions"])
        self.capture(mid="clear", text="/话题 清除")
        self.assertEqual(self.runtime.store.state(OWNER)["prior_mentions"], {})
        self.assertEqual(self.marked(self.capture(mid="again", text="编程"), [revised]), [])

    async def test_shared_or_partial_anchor_cannot_establish_durable_mentions(self):
        shared = "明天下午我准备参加一个编程岗位面试"
        extra = self.source("other-interview", shared+"，第二天再确认地点。")
        context = self.capture()
        self.runtime.stage(context, [self.first, extra])
        self.assertTrue(await self.sent(shared))
        self.assertEqual(self.runtime.store.state(OWNER)["prior_mentions"], {})
        self.assertEqual(len(self.runtime.store.state(OWNER)["focus_refs"]), 2)
        self.assertTrue(await self.sent(extra["text"], mid="specific"))
        self.assertEqual(self.marked(context, [self.first, extra]), [extra["id"]])

    async def test_repeat_hook_replaces_unsent_approvals_and_keeps_only_previous_deliveries(self):
        context = self.capture()
        self.runtime.stage(context, [self.first, self.second])
        self.runtime.stage(context, [self.first])
        self.assertFalse(await self.sent(self.second["text"], mid="dropped"))
        self.assertTrue(await self.sent(self.first["text"], mid="part-one"))
        self.runtime.stage(context, [])
        self.assertFalse(await self.sent(self.second["text"], mid="empty"))
        self.runtime.stage(context, [self.second])
        self.assertFalse(await self.sent(self.first["text"], mid="old-approval"))
        self.assertTrue(await self.sent(self.second["text"], mid="part-two"))
        self.assertEqual(len(self.runtime.store.state(OWNER)["focus_refs"]), 2)

    async def test_staging_reads_roots_once_and_retains_only_current_allowed_candidates(self):
        hidden = self.source("private-book", "我们准备在登山露营结束之后讨论一本探险小说。")
        stale = self.source("old-topic", "这次冬季徒步行程预计十二月出发。")
        self.source("old-topic", "这次冬季徒步行程已经取消。")
        context = self.capture()
        # This new restriction is only exposed during source revalidation.
        calls = 0
        def protected(rows, context=None):
            nonlocal calls
            calls += 1
            return rows if calls == 1 else [row for row in rows if row["id"] != hidden["id"]]
        self.bot._expression_records = protected
        with patch.object(self.bot, "_resolve_roots", wraps=self.bot._resolve_roots) as lookup:
            legacy = {**self.first, "id": "story:unsupported/legacy-key", "source": "story"}
            self.runtime.stage(context, [self.first, legacy, stale, hidden])
            lookup.assert_called_once_with([self.first["id"], stale["id"], hidden["id"]])
        self.assertEqual(calls, 2)
        self.assertFalse(await self.sent(stale["text"], mid="stale"))
        self.assertFalse(await self.sent(hidden["text"], mid="protected"))
        self.assertTrue(await self.sent(self.first["text"], mid="valid"))
        self.assertEqual(self.marked(context, [self.first]), [self.first["id"]])

    async def test_repeat_hook_during_verification_rejects_pending_chunk_even_with_same_anchor(self):
        context = self.capture()
        self.runtime.stage(context, [self.first])
        async def replace_during_verification(*args, **kwargs):
            self.runtime.stage(context, [self.first, self.second])
            return {"stream_id": "private-one"}
        self.bot.ctx.chat.get_stream_by_user_id.side_effect = replace_during_verification
        self.assertFalse(await self.sent(self.first["text"], mid="in-flight"))
        self.assertEqual(self.runtime.store.state(OWNER)["prior_mentions"], {})
        self.bot.ctx.chat.get_stream_by_user_id.side_effect = None
        self.assertTrue(await self.sent(self.first["text"], mid="new-approval"))

    def test_only_self_contained_short_followups_match(self):
        for text in ("那件事", "后来呢？", "继续说。", "上次那件事", "你觉得它哪里有意思？", "你怎么看刚才读的"):
            self.assertTrue(is_followup(text))
        for text in ("你觉得那件事里的角色名字怎么样", "他说：后来呢", "别继续说", "那件事和旅游哪个好",
                     "她问你怎么看刚才读的", "“你觉得它哪里有意思”", "你怎么看刚才读的那本新书", "你觉得茶叶怎么泡"):
            self.assertFalse(is_followup(text))
