"""Actual plugin Tool/Hook paths with host stubs, SQLite and offline MCP clients."""
from __future__ import annotations

import asyncio
import copy
import json
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from MaiLivingWorld.tests.test_runtime import FakeContext, plugin
from MaiLivingWorld import mcp_client, mcp_media
from MaiLivingWorld.mcp_client import MCPError
from MaiLivingWorld.tool_store import ToolStore
from MaiLivingWorld.store import Store


OWNER, OTHER, GROUP = "12345678", "87654321", "22222222"
TASK = "a" * 32
IMAGE = {"content": "已读取一张图片", "content_items": [{"type": "image", "data": "sensitive-image-bytes", "mime_type": "image/png"}]}


def message(mid="m1", *, uid=OWNER, sid="private-one", group=None, text="查询作品123"):
    return {"platform": "qq", "message_id": mid, "session_id": sid, "account_id": "99999999",
            "processed_plain_text": text, "message_info": {
                "user_info": {"user_id": uid, "user_nickname": "主人"},
                "group_info": {"group_id": group} if group else None}}


def detail(**changes):
    data = {"pixiv_id": 123, "type": "illust", "status": "active", "current_version": 1,
            "title": "A cached illustration", "current_files": [{"page_index": 0,
            "file_type": "image", "filename": "0.jpg", "size_bytes": 32,
            "download_url": "/media/illust/123/versions/1/0.jpg"}]}
    data.update(changes)
    return data


def schema(properties, required):
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


class OfflineMCP:
    def __init__(self, provider):
        self.provider = provider
        self.calls = []
        self.lists = 0
        self.closed = False
        self.on_call = None
        self.failure = None
        self.response = None

    @property
    def retired(self):
        return self.closed

    async def close(self):
        self.closed = True

    async def list_tools(self):
        self.lists += 1
        if self.closed:
            raise MCPError("closed")
        string, integer = {"type": "string"}, {"type": "integer"}
        return {
            "jm_get_album": {"input_schema": schema({"jm_id": string}, ["jm_id"])},
            "jm_start_download": {"input_schema": schema({"jm_id": string}, ["jm_id"])},
            "jm_get_download_status": {"input_schema": schema({"task_id": string}, ["task_id"])},
            "get_library_work": {"input_schema": schema({"kind": string, "pixiv_id": integer,
                "file_offset": integer, "file_limit": integer}, ["kind", "pixiv_id"])},
            "admin_send_private_message": {"input_schema": schema({}, [])},
        }

    async def call_tool(self, name, arguments):
        if self.closed:
            raise MCPError("closed")
        self.calls.append((name, copy.deepcopy(arguments)))
        if self.on_call:
            await self.on_call()
        if self.failure:
            self.closed = True
            raise MCPError(self.failure)
        if self.response is not None:
            return copy.deepcopy(self.response)
        if name == "jm_get_album":
            return {"jm_id": arguments["jm_id"], "title": "A sourced title", "tags": ["space"]}
        if name == "jm_start_download":
            return {"task_id": TASK, "state": "running"}
        if name == "get_library_work":
            return detail()
        return {"task_id": TASK, "state": "completed", "result": {"file_path": "/srv/private/a.pdf"}}


class MCPPluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.created = []

        def factory(url, bearer_token, **options):
            client = OfflineMCP("cap" if "cap" in url else "pixiv")
            self.created.append(client)
            return client

        self.transport_patch = patch.object(mcp_client, "HTTPMCPClient", side_effect=factory)
        self.transport_patch.start()
        self.bot = self.make_bot()

    def make_bot(self):
        bot = plugin.MaiLivingWorld()
        bot.ctx = FakeContext()
        bot.ctx.paths = SimpleNamespace(data_dir=Path(self.temp.name))
        bot.config = plugin.Config()
        bot.config.owner.enabled = True
        bot.config.owner.qq = OWNER
        bot.config.unified_mind.enabled = True
        bot.config.unified_mind.mode = "active"
        bot.config.brain_memory.private_users = [OTHER]
        bot.config.brain_memory.group_ids = [GROUP]
        bot.config.mcp.enabled = True
        bot.config.mcp_cap.enabled = True
        bot.config.mcp_pixiv.enabled = True
        return bot

    async def asyncTearDown(self):
        try:
            await self.bot.on_unload()
        finally:
            self.transport_patch.stop()
            self.temp.cleanup()

    async def capture(self, **options):
        event = message(**options)
        await self.bot.capture_unified(message=event)
        return event

    async def call(self, tool="jm_get_album", arguments=None, **kwargs):
        kwargs.setdefault("session_id", "private-one")
        return await self.bot.mcp_call("cap", tool, {"jm_id": "123"} if arguments is None else arguments, **kwargs)

    def client(self, provider="cap"):
        return self.bot._mcp_client(provider)

    async def test_tools_require_fresh_trusted_capture_not_model_supplied_identity(self):
        denied = await self.call(speaker_id=OWNER, owner=True)
        self.assertFalse(denied["success"])
        self.assertEqual(self.created, [])
        await self.capture(uid=OTHER)
        spoofed = await self.call(message=message(uid=OWNER))
        self.assertFalse(spoofed["success"])
        self.assertEqual(self.created, [])

    async def test_actual_owner_catalog_and_tool_output_are_filtered_and_recorded(self):
        event = await self.capture()
        catalog = await self.bot.mcp_tools("cap", message=event)
        self.assertTrue(catalog["success"])
        self.assertNotIn("admin_send_private_message", catalog["content"])
        self.client().response = {"jm_id": "123", "title": "Known title", "token": "private-token",
                                  "file_path": "/srv/private/file.pdf", "download_url": "https://files/a?signature=secret"}
        result = await self.call(message=event)
        self.assertEqual(set(result), {"success", "content"})
        self.assertTrue(result["success"])
        self.assertIn("Known title", result["content"])
        for private in ("private-token", "/srv/private", "signature=secret"):
            self.assertNotIn(private, result["content"])
        records = self.bot._tool_store.observations()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["scope"], "private:"+OWNER)
        self.assertEqual(records[0]["subject_id"], OWNER)
        self.assertEqual(records[0]["sensitivity"], "personal")
        self.bot.ctx.call_capability.assert_not_awaited()
        self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()

    async def test_disabled_plugin_mcp_unified_owner_or_provider_never_launches(self):
        await self.capture()
        for section in ("plugin", "mcp", "unified_mind", "owner", "mcp_cap"):
            settings = getattr(self.bot.config, section)
            settings.enabled = False
            denied = await self.call()
            self.assertFalse(denied["success"], section)
            settings.enabled = True
        self.assertEqual(self.created, [])

    async def test_same_nickname_nonowner_has_no_owner_tools(self):
        await self.capture(uid=OTHER)
        self.assertFalse((await self.call())["success"])
        self.bot.config.mcp.owner_only = False
        self.assertTrue((await self.call())["success"])
        self.bot.config.mcp.allow_writes = True
        self.assertFalse((await self.call(tool="jm_start_download"))["success"])
        self.assertEqual(self.bot._self_state.state()["owner_id"], OWNER)
        self.assertEqual([name for name, _ in self.client().calls], ["jm_get_album"])

    async def test_group_requires_exact_target_and_never_gains_write_authority(self):
        self.bot.config.mcp.allow_group_reads = True
        self.bot.config.mcp.allow_writes = True
        await self.capture(mid="owner-group", sid="group-one", group=GROUP)
        await self.capture(mid="other-group", uid=OTHER, sid="group-one", group=GROUP)
        self.assertFalse((await self.call(session_id="group-one"))["success"])
        self.assertFalse((await self.call(session_id="group-one", reply_message_id="other-group"))["success"])
        self.assertTrue((await self.call(session_id="group-one", reply_message_id="owner-group"))["success"])
        self.assertFalse((await self.call(tool="jm_start_download", session_id="group-one", reply_message_id="owner-group"))["success"])
        self.assertEqual([name for name, _ in self.client().calls], ["jm_get_album"])

    async def test_private_media_or_command_message_clears_old_session_fallback(self):
        for media in (True, False):
            with self.subTest(media=media):
                await self.capture(mid="text-"+str(media))
                event = message(mid="nontext-"+str(media), text="")
                event["raw_message"] = [{"type": "image", "data": {"file": "image"}}]
                if not media:
                    event["is_command"] = True
                await self.bot.capture_unified(message=event)
                self.assertFalse((await self.call())["success"])
        self.assertEqual(self.created, [])

    async def test_normalized_write_request_is_deduplicated_in_actual_tool_path(self):
        self.bot.config.mcp.allow_writes = True
        await self.capture()
        first = await self.call(tool="jm_start_download", arguments={"jm_id": "000123"})
        second = await self.call(tool="jm_start_download", arguments={"jm_id": "123"})
        self.assertTrue(first["success"])
        self.assertFalse(second["success"])
        self.assertEqual(len(self.client().calls), 1)
        self.assertIn("不等于任务完成", first["content"])

    async def test_private_task_cannot_be_read_from_owner_group(self):
        self.bot.config.mcp.allow_writes = True
        self.bot.config.mcp.allow_group_reads = True
        await self.capture()
        self.assertTrue((await self.call(tool="jm_start_download"))["success"])
        await self.capture(mid="group-task", sid="group-one", group=GROUP)
        result = await self.call(tool="jm_get_download_status", arguments={"task_id": TASK},
                                 session_id="group-one", reply_message_id="group-task")
        self.assertFalse(result["success"])
        self.assertEqual(len(self.client().calls), 1)

    async def test_owner_or_unified_configuration_change_while_waiting_discards_result(self):
        for change in ("owner", "unified"):
            with self.subTest(change=change):
                self.bot.config.owner.qq = OWNER
                self.bot.config.unified_mind.enabled = True
                await self.capture(mid="waiting-"+change)
                client = self.client()

                async def mutate():
                    if change == "owner":
                        self.bot.config.owner.qq = OTHER
                    else:
                        self.bot.config.unified_mind.enabled = False

                client.on_call = mutate
                client.response = {"jm_id": "123", "title": "private stale result"}
                result = await self.call()
                self.assertFalse(result["success"])
                self.assertNotIn("private stale result", result["content"])
                self.assertEqual(self.bot._tool_store.observations(), [])

    async def test_disconnected_client_is_replaced_only_for_a_new_independent_request(self):
        await self.capture()
        original = self.client()
        original.failure = "disconnected"
        self.assertFalse((await self.call())["success"])
        self.assertEqual(len(original.calls), 1)
        self.assertEqual(len(self.created), 1)
        await self.capture(mid="new-explicit-request")
        result = await self.call()
        self.assertTrue(result["success"])
        self.assertEqual(len(self.created), 2)
        self.assertEqual(len(original.calls), 1)
        self.assertEqual(len(self.created[-1].calls), 1)

    async def test_restart_retains_budget_but_requires_new_trusted_context(self):
        await self.capture()
        self.assertTrue((await self.call())["success"])
        await self.bot.on_unload()
        self.bot = self.make_bot()
        result = await self.call()
        self.assertFalse(result["success"])
        self.assertEqual(len(self.created), 1)
        self.assertEqual(self.bot._ensure_tool_store().status()["call_budget"]["count"], 1)

    async def test_observation_source_withdrawal_is_applied_through_public_tool(self):
        await self.capture()
        args = {"kind": "illust", "pixiv_id": 123}
        first = await self.bot.mcp_call("pixiv", "get_library_work", args, session_id="private-one")
        self.assertTrue(first["success"])
        old = self.bot._tool_store.observations()[0]
        await self.capture(mid="withdrawn")
        self.client("pixiv").response = detail(accessible=False, status="private", title="stale private title")
        second = await self.bot.mcp_call("pixiv", "get_library_work", args, session_id="private-one")
        self.assertNotIn("stale private title", second["content"])
        self.assertEqual(self.bot._tool_store.resolve([old["id"]]), [])

    async def test_stdio_blank_cwd_uses_explicit_safe_directory_and_env_allowlist(self):
        await self.capture()
        self.bot.config.mcp_cap.transport = "stdio"
        self.bot.config.mcp_cap.command = "configured-python"
        self.bot.config.mcp_cap.args = ["configured-server.py"]
        self.bot.config.mcp_cap.cwd = ""
        self.bot.config.mcp_cap.env_names = ["TEST_ALLOWED"]
        fake = OfflineMCP("cap")
        with patch.dict(os.environ, {"TEST_ALLOWED": "explicit", "TEST_UNEXPECTED_SECRET": "not-inherited"}), patch.object(plugin, "StdioMCPClient", return_value=fake) as constructor:
            self.assertTrue((await self.call())["success"])
        args = constructor.call_args.args
        self.assertEqual(Path(args[2]), Path(self.temp.name).resolve())
        self.assertEqual(args[3]["TEST_ALLOWED"], "explicit")
        self.assertNotIn("TEST_UNEXPECTED_SECRET", args[3])

    async def test_media_permission_requires_owner_private_and_explicit_switch(self):
        await self.capture()
        with patch.object(mcp_media, "fetch_pixiv_image", new_callable=AsyncMock) as fetch:
            self.assertFalse((await self.bot.pixiv_image(123, session_id="private-one"))["success"])
            self.bot.config.mcp.media_enabled = True
            await self.capture(uid=OTHER)
            self.assertFalse((await self.bot.pixiv_image(123, session_id="private-one"))["success"])
            await self.capture(mid="group-image", sid="group-one", group=GROUP)
            self.assertFalse((await self.bot.pixiv_image(123, session_id="group-one", reply_message_id="group-image"))["success"])
            fetch.assert_not_awaited()
        self.assertEqual(self.created, [])

    async def test_media_success_uses_two_durable_budget_slots_without_sending_qq(self):
        self.bot.config.mcp.media_enabled = True
        await self.capture()
        with patch.object(mcp_media, "fetch_pixiv_image", new_callable=AsyncMock, return_value=copy.deepcopy(IMAGE)) as fetch:
            result = await self.bot.pixiv_image(123, session_id="private-one")
            self.assertTrue(result["success"])
            self.assertEqual(result["content_items"], IMAGE["content_items"])
            self.assertEqual(fetch.await_args.kwargs["details"]["pixiv_id"], 123)
        self.assertEqual(self.bot._tool_store.status()["call_budget"]["count"], 2)
        self.assertNotIn("sensitive-image-bytes", json.dumps(self.bot._tool_store.observations()))
        self.bot.ctx.call_capability.assert_not_awaited()
        self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()

    async def test_media_persistent_owner_change_during_fetch_never_returns_image(self):
        self.bot.config.mcp.media_enabled = True
        await self.capture()

        async def fetch(**_):
            self.bot._self_state.configure_owner(OTHER)
            return copy.deepcopy(IMAGE)

        with patch.object(mcp_media, "fetch_pixiv_image", side_effect=fetch):
            result = await self.bot.pixiv_image(123, session_id="private-one")
        self.assertFalse(result["success"])
        self.assertNotIn("content_items", result)
        self.assertNotIn("sensitive-image-bytes", json.dumps(result))

    async def test_config_reload_cancels_media_before_discarding_task_tracking(self):
        self.bot.config.mcp.media_enabled = True
        await self.capture()
        entered, canceled = asyncio.Event(), asyncio.Event()

        async def fetch(**_):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                canceled.set()
                raise

        with patch.object(mcp_media, "fetch_pixiv_image", side_effect=fetch):
            running = asyncio.create_task(self.bot.pixiv_image(123, session_id="private-one"))
            await asyncio.wait_for(entered.wait(), 1)
            try:
                await self.bot.on_config_update("mcp", {}, "new")
                self.assertTrue(running.done())
                self.assertTrue(canceled.is_set())
                with self.assertRaises(asyncio.CancelledError):
                    await running
                self.assertEqual(self.bot._tool_store.status()["calls"]["uncertain"], 1)
            finally:
                if not running.done():
                    running.cancel()
                    await asyncio.gather(running, return_exceptions=True)

    async def test_unload_cancels_media_before_closing_sqlite(self):
        self.bot.config.mcp.media_enabled = True
        await self.capture()
        entered = asyncio.Event()

        async def fetch(**_):
            entered.set()
            await asyncio.Event().wait()

        with patch.object(mcp_media, "fetch_pixiv_image", side_effect=fetch):
            running = asyncio.create_task(self.bot.pixiv_image(123, session_id="private-one"))
            await asyncio.wait_for(entered.wait(), 1)
            try:
                await self.bot.on_unload()
                self.assertTrue(running.done())
                with self.assertRaises(asyncio.CancelledError):
                    await running
                restored = ToolStore(Path(self.temp.name) / "tool_memory.sqlite3")
                try:
                    self.assertEqual(restored.status()["calls"]["uncertain"], 1)
                finally:
                    restored.close()
            finally:
                if not running.done():
                    running.cancel()
                    await asyncio.gather(running, return_exceptions=True)

    def prepare_proactive(self):
        self.bot.config.general.enabled = True
        self.bot.config.general.dry_run = False
        self.bot.config.audience.private_users = [OWNER]
        self.bot.config.audience.group_ids = [GROUP]
        self.bot.config.audience.quiet_start = "00:00"
        self.bot.config.audience.quiet_end = "00:00"
        self.bot.config.audience.history_gate = False
        self.bot.config.unified_mind.min_interval_seconds = 0
        self.bot.config.mind.enabled = True
        self.bot.config.brain_memory.enabled = True
        self.bot.config.brain_memory.mode = "active"
        self.bot._store = Store(Path(self.temp.name) / "story.sqlite3")
        self.bot._choose_story = AsyncMock(return_value={"key": "public-story", "source": "公开资料",
            "title": "公开的学习方法", "summary": "练习时可以逐步尝试", "url": "https://example.org/article"})
        brain = self.bot._ensure_brain()
        stamp = time.time()
        with brain.con:
            brain.con.execute("""INSERT INTO brain_events
                (id,scope,actor_id,kind,summary,importance,happened,created)
                VALUES (?,?,?,'experience',?,3,?,?)""",
                ("P1", "private:"+OWNER, OWNER, "此前在私聊倾诉工作变化后很失落", stamp, stamp))
        brain.hint = Mock(return_value="UNFILTERED LEGACY BRAIN")
        mind = self.bot._ensure_mind()
        mind.private_hint = Mock(return_value="UNFILTERED LEGACY MIND")
        self.bot.ctx.llm.generate.return_value = {"content": json.dumps({"behavior": "warm",
            "decisions": [{"id": "brain:event:P1", "mode": "internal"}]})}
        return brain, mind

    async def proactive(self, kind="private", ident=OWNER):
        await self.bot._try_target(kind, ident, datetime.now(ZoneInfo("Asia/Shanghai")))

    async def test_active_owner_proactive_uses_real_unified_filter_and_skips_legacy_hints(self):
        brain, mind = self.prepare_proactive()
        await self.proactive()
        self.bot.ctx.maisaka.proactive.trigger.assert_awaited_once()
        sid, intent = self.bot.ctx.maisaka.proactive.trigger.await_args.args
        self.assertEqual(sid, "private-one")
        self.assertIn("continuous-self", intent)
        self.assertIn("唯一的主人", intent)
        self.assertNotIn("工作变化后很失落", intent)
        self.assertNotIn("UNFILTERED", intent)
        brain.hint.assert_not_called()
        mind.private_hint.assert_not_called()
        self.assertEqual(self.bot._store.statistics()["wake_attempts"], 1)

    async def test_active_group_proactive_has_no_speaker_or_owner_privilege(self):
        self.prepare_proactive()
        await self.proactive("group", GROUP)
        self.bot.ctx.maisaka.proactive.trigger.assert_awaited_once()
        prompt = self.bot.ctx.llm.generate.await_args.args[0]
        data = json.loads(prompt.split("UNTRUSTED_INPUT_JSON\n", 1)[1])
        self.assertEqual(data["audience"]["speaker_id"], "")
        self.assertFalse(data["audience"]["is_owner"])
        self.assertEqual(data["audience"]["scope"], "group:"+GROUP)
        intent = self.bot.ctx.maisaka.proactive.trigger.await_args.args[1]
        self.assertNotIn("工作变化后很失落", intent)
        self.assertIn("没有经验证的当前发言者", intent)
        self.assertNotIn("私聊对象是唯一的主人", intent)

    async def test_proactive_configuration_change_during_deliberation_does_not_trigger(self):
        self.prepare_proactive()
        response = self.bot.ctx.llm.generate.return_value

        async def generate(*_, **__):
            self.bot.config.unified_mind.enabled = False
            return response

        self.bot.ctx.llm.generate.side_effect = generate
        await self.proactive()
        self.bot.ctx.llm.generate.assert_awaited_once()
        self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()
        self.assertEqual(self.bot._store.statistics()["wake_attempts"], 0)

    async def test_proactive_new_inbound_message_while_deciding_does_not_trigger(self):
        self.prepare_proactive()
        self.bot.ctx.message.get_by_time_in_chat.side_effect = [[], [message("new-message")]]
        await self.proactive()
        self.bot.ctx.llm.generate.assert_awaited_once()
        self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()
        self.assertEqual(self.bot._store.statistics()["wake_attempts"], 0)

    async def test_proactive_retracted_source_during_deliberation_does_not_trigger(self):
        brain, _ = self.prepare_proactive()
        response = self.bot.ctx.llm.generate.return_value

        async def generate(*_, **__):
            with brain.con:
                brain.con.execute("DELETE FROM brain_events WHERE id='P1'")
            return response

        self.bot.ctx.llm.generate.side_effect = generate
        await self.proactive()
        self.bot.ctx.llm.generate.assert_awaited_once()
        self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()
        self.assertEqual(self.bot._store.statistics()["wake_attempts"], 0)


if __name__ == "__main__":
    unittest.main()
