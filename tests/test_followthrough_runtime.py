"""Real SQLite/MCPBridge, fake local services; never a real account or QQ send."""
import asyncio
import copy
import hashlib
from pathlib import Path
import sys
import tempfile
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from MaiLivingWorld.followthrough_runtime import FollowthroughRuntime, task_result
from MaiLivingWorld.followthrough_store import digest
from MaiLivingWorld.mcp_runtime import MCPBridge
from MaiLivingWorld.tool_store import ToolStore
from MaiLivingWorld.owner import extract_sender

OWNER, OTHER, TASK = "12345678", "87654321", "a"*32


class Section(SimpleNamespace):
    def model_dump(self):
        return vars(self).copy()


def message(text="查询进度", *, owner=OWNER, group=None, mid="in1", sid="owner-private"):
    return {"platform": "qq", "session_id": sid, "message_id": mid, "processed_plain_text": text,
            "account_id": "99999999", "message_info": {"user_info": {"user_id": owner},
                "group_info": {"group_id": group} if group else None}}


class Client:
    def __init__(self):
        self.calls = []
        self.response = {"task_id": TASK, "state": "running"}
        self.callback = None
        self.error = None

    async def list_tools(self):
        def schema(properties):
            return {"input_schema": {"type": "object", "properties": {key: {"type": "string"} for key in properties},
                                     "required": properties, "additionalProperties": False}}
        return {"jm_get_download_status": schema(["task_id"]), "get_job": schema(["job_id", "queue"])}

    async def call_tool(self, name, args):
        self.calls.append((name, copy.deepcopy(args)))
        if self.callback:
            await self.callback()
        if self.error:
            raise self.error
        return copy.deepcopy(self.response)


class Bot:
    def __init__(self, path):
        self.ctx = Section(paths=Section(data_dir=path))
        self.config = Section(plugin=Section(enabled=True), owner=Section(enabled=True, qq=OWNER),
            followthrough=Section(enabled=True, interval_seconds=300, max_daily_checks=12),
            mcp=Section(enabled=True, owner_only=True, allow_writes=True, allow_file_transfer=True,
                        allow_account_actions=True, allow_group_reads=False, max_daily_calls=100,
                        timeout_seconds=1, remember_results=True), general=Section(timezone="UTC"))
        self.servers = {key: Section(enabled=True, url="http://"+key+":8000/mcp") for key in ("cap", "pixiv")}
        self.current_owner = OWNER
        self._self_state = Section(state=lambda: {"owner_id": self.current_owner})
        self._mcp_generation = 0
        self.tools = ToolStore(path/"tools.sqlite3")
        self._mcp_bridge = MCPBridge(self.tools)
        self.client = Client()

    def _ledger_storage_options(self):
        return {}

    def _ensure_tool_store(self):
        return self.tools

    def _mcp_source_signature(self, provider):
        return digest(self.servers[provider].model_dump())

    def _mcp_signature(self, provider):
        return digest([self._mcp_source_signature(provider), self.config.mcp.model_dump(), self.config.owner.model_dump()])

    def _mcp_server_config(self, provider):
        return self.servers.get(provider)

    def _mcp_client(self, provider):
        return self.client

    def _mcp_context(self, kwargs):
        sender = extract_sender(kwargs.get("message"))
        if not sender:
            return None
        return {"scope": sender.kind+":"+sender.target_id, "speaker_id": sender.user_id,
                "sid": sender.stream_id, "mid": sender.message_id, "query": ""}, dict(kwargs)


class FollowthroughRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bot = Bot(Path(self.temp.name))
        self.runtime = FollowthroughRuntime(self.bot)
        # Delivery verification has its own Host integration suite. Here an
        # explicit trusted receipt fixture tests consumption, races and matching.
        self.delivery = ModuleType("MaiLivingWorld.delivery")
        self.delivery.verify_owner_delivery = AsyncMock(return_value=None)
        self.patcher = patch.dict(sys.modules, {"MaiLivingWorld.delivery": self.delivery})
        self.patcher.start()

    async def asyncTearDown(self):
        self.patcher.stop()
        self.runtime.close()
        self.bot.tools.close()
        self.temp.cleanup()

    def bind(self, provider="cap", operation="jm_start_download", task=TASK, owner=OWNER, proof=True):
        self.bot.tools.bind_task(provider, task, "private:"+owner, owner, operation)
        if proof:
            self.runtime.store.bind_source(provider, task, owner_id=owner, scope="private:"+owner,
                operation=operation, source_signature=self.bot._mcp_source_signature(provider))

    async def watch(self, provider="cap", task=TASK):
        return await self.runtime.tool("watch", provider, task, kwargs={"message": message()})

    def receipt(self, text, mid="out1", owner=OWNER, sid="owner-private"):
        return {"owner": owner, "sid": sid, "mid": mid, "text": text,
                "receipt_id": digest([sid, mid]), "text_digest": hashlib.sha256(text.encode()).hexdigest()}

    async def send(self, text, mid="out1", **changes):
        self.delivery.verify_owner_delivery.return_value = self.receipt(text, mid, **changes)
        return await self.runtime.sent({"sent": True})

    async def active(self, provider="cap", operation="jm_start_download", task=TASK):
        self.bind(provider, operation, task)
        draft = await self.watch(provider, task)
        self.assertTrue(await self.send(draft["delivery_text"]))
        return self.runtime.store.get(OWNER, draft["data"]["id"])

    async def test_full_restart_task_report_chain_and_no_raw_payload(self):
        row = await self.active()
        self.bot.client.response = {"task_id": TASK, "state": "completed", "result": {"path": "/secret/cache.pdf", "token": "secret-value"}}
        await self.runtime.tick()
        self.assertEqual(self.bot.client.calls, [("jm_get_download_status", {"task_id": TASK})])
        self.assertEqual(self.runtime.store.get(OWNER, row["id"])["status"], "ready")
        self.runtime.close()
        self.runtime = FollowthroughRuntime(self.bot)
        report = await self.runtime.tool("report", item_id=row["id"], kwargs={"message": message(sid="new-private")})
        self.assertIn("服务已确认处理成功", report["delivery_text"])
        self.assertEqual(self.runtime.store.get(OWNER, row["id"])["status"], "ready")
        self.assertTrue(await self.send(report["delivery_text"], "out2", sid="new-private"))
        self.assertEqual(self.runtime.store.get(OWNER, row["id"])["status"], "reported")
        self.assertFalse(await self.send(report["delivery_text"], "out2", sid="new-private"))
        saved = str(self.runtime.store.items(OWNER))+str(self.bot.tools._read("SELECT * FROM tool_observations"))
        self.assertNotIn("secret", saved)
        self.assertEqual(self.bot.tools._read("SELECT * FROM tool_observations"), [])

    async def test_watch_requires_original_source_owner_and_operation_permissions(self):
        self.bind(proof=False)
        self.assertFalse((await self.watch())["success"])
        self.bind()
        for kwargs in ({"message": message(owner=OTHER)}, {"message": message(group="22222222")}, {"message": {}}, {}):
            self.assertFalse((await self.runtime.tool("list", kwargs=kwargs))["success"])
        self.bot.config.mcp.allow_writes = False
        self.assertFalse((await self.watch())["success"])
        self.bot.config.mcp.allow_writes = True
        self.bot.servers["cap"].url = "http://other-service/mcp"
        self.assertFalse((await self.watch())["success"])

    async def test_account_and_transfer_permissions_never_follow_write_retry(self):
        self.bind("pixiv", "archive_work:account")
        self.bot.config.mcp.allow_account_actions = False
        self.assertFalse((await self.watch("pixiv"))["success"])
        self.bind("cap", "jm_publish_pdf", "c"*32)
        self.bot.config.mcp.allow_file_transfer = False
        self.assertFalse((await self.watch("cap", "c"*32))["success"])
        self.assertEqual(self.bot.client.calls, [])

    async def test_pixiv_actual_queue_and_status_contracts(self):
        for index, (operation, queue) in enumerate((("archive_work", "archive"), ("generate_ugoira_preview", "media"), ("run_sync_source", "sync"))):
            with self.subTest(queue=queue):
                task = format(index+1, "032x")
                self.bind("pixiv", operation, task)
                draft = await self.watch("pixiv", task)
                self.assertTrue(await self.send(draft["delivery_text"], "out"+str(index)))
                self.bot.client.response = {"job_id": task, "status": "succeeded"}
                # Store fixture moves the persisted control clock back, no sleep.
                with self.runtime.store.con:
                    self.runtime.store.con.execute("UPDATE followthrough_control SET last_check=0 WHERE owner_id=?", (OWNER,))
                await self.runtime.tick()
                self.assertEqual(self.bot.client.calls[-1], ("get_job", {"job_id": task, "queue": queue}))
                self.assertEqual(self.runtime.store.get(OWNER, draft["data"]["id"])["result"], "succeeded")

    async def test_timeouts_mismatch_and_rpc_success_cannot_complete_background_task(self):
        for payload in ({"task_id": "f"*32, "state": "completed"}, {"task_id": TASK, "state": "running"}, {"success": True}, {"task_id": TASK, "state": {"secret": "succeeded"}}):
            self.assertNotEqual(task_result("cap", TASK, payload), "succeeded")
        for state in ("queued", "running", "finalizing", "retry_wait"):
            self.assertEqual(task_result("pixiv", TASK, {"job_id": TASK, "status": state}), "pending")
        self.assertEqual(task_result("pixiv", TASK, {"id": TASK, "status": "succeeded"}), "unknown")
        row = await self.active()
        self.bot.client.error = TimeoutError("secret credential and /private/path")
        await self.runtime.tick()
        current = self.runtime.store.get(OWNER, row["id"])
        self.assertEqual((current["status"], current["result"]), ("active", "unknown"))
        self.assertEqual(len(self.bot.client.calls), 1)
        await self.runtime.tick()
        self.assertEqual(len(self.bot.client.calls), 1)
        self.assertNotIn("secret", str(current))

    async def test_inflight_owner_config_binding_reset_and_cancel_drop_success(self):
        row = await self.active()
        for action in ("owner", "config", "source", "reset", "cancel"):
            with self.subTest(action=action):
                before = self.runtime.store.get(OWNER, row["id"])
                with self.runtime.store.con:
                    before.update(lease_token="", lease_until=0, next_poll=0)
                    self.runtime.store._save(before, time.time())
                    self.runtime.store.con.execute("UPDATE followthrough_control SET last_check=0 WHERE owner_id=?", (OWNER,))
                async def change():
                    if action == "owner":
                        self.bot.current_owner = OTHER
                    elif action == "config":
                        self.bot.config.followthrough.enabled = False
                    elif action == "source":
                        self.bot.servers["cap"].url += "new"
                    elif action == "reset":
                        self.runtime.reset()
                    else:
                        current = self.runtime.store.get(OWNER, row["id"])
                        self.runtime.store.cancel(OWNER, row["id"], current["revision"])
                self.bot.client.callback = change
                self.bot.client.response = {"task_id": TASK, "state": "completed"}
                await self.runtime.tick()
                self.assertNotEqual(self.runtime.store.get(OWNER, row["id"])["result"], "succeeded")
                self.bot.current_owner = OWNER
                self.bot.config.followthrough.enabled = True
                self.bot.servers["cap"].url = "http://cap:8000/mcp"

    async def test_delivery_await_change_failure_and_quotes_do_not_activate(self):
        self.bind()
        draft = await self.watch()
        self.delivery.verify_owner_delivery.return_value = None
        self.assertFalse(await self.runtime.sent({"sent": False}))
        self.assertFalse(await self.send("“"+draft["delivery_text"]+"”"))
        self.assertFalse(await self.send(draft["delivery_text"], owner=OTHER))
        async def change(*args):
            self.bot.config.followthrough.enabled = False
            return self.receipt(draft["delivery_text"])
        self.delivery.verify_owner_delivery.side_effect = change
        self.assertFalse(await self.runtime.sent({"sent": True}))
        self.assertEqual(self.runtime.store.get(OWNER, draft["data"]["id"])["status"], "draft")

    async def test_proactive_report_intent_validates_revision_source_and_only_sent_marks(self):
        row = await self.active()
        self.bot.client.response = {"task_id": TASK, "state": "completed"}
        await self.runtime.tick()
        due = self.runtime.due(OWNER)
        report = self.runtime.prepare_report(OWNER, "owner-private", due["id"], due["revision"])
        self.assertTrue(self.runtime.valid_report(OWNER, report["sid"], report["id"], report["revision"], report["signature"]))
        self.assertIsNone(self.runtime.due(OTHER))
        self.bot.config.mcp.allow_writes = False
        self.assertFalse(self.runtime.valid_report(OWNER, report["sid"], report["id"], report["revision"], report["signature"]))
        self.assertFalse(await self.send(report["text"], "out2"))
        self.assertEqual(self.runtime.store.get(OWNER, row["id"])["status"], "ready")

    async def test_mcp_budget_zero_and_cancellation_do_not_replay_writes(self):
        row = await self.active()
        self.bot.config.mcp.max_daily_calls = 0
        await self.runtime.tick()
        self.assertEqual(self.bot.client.calls, [])
        self.assertEqual(self.runtime.store.get(OWNER, row["id"])["result"], "unknown")
        self.bot.config.mcp.max_daily_calls = 100
        with self.runtime.store.con:
            current = self.runtime.store.get(OWNER, row["id"])
            current.update(next_poll=0)
            self.runtime.store._save(current, time.time())
            self.runtime.store.con.execute("UPDATE followthrough_control SET last_check=0 WHERE owner_id=?", (OWNER,))
        entered = asyncio.Event()
        async def block():
            entered.set()
            await asyncio.Event().wait()
        self.bot.client.callback = block
        task = asyncio.create_task(self.runtime.tick())
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        current = self.runtime.store.get(OWNER, row["id"])
        self.assertEqual((current["result"], current["status"], current["lease_token"]), ("unknown", "active", ""))
        self.assertEqual([name for name, args in self.bot.client.calls], ["jm_get_download_status"])

    async def test_cancel_command_whole_owner_private_and_cas(self):
        row = await self.active()
        command = "/回告 取消 "+row["id"]+" "+str(row["revision"])
        self.assertFalse(self.runtime.capture(message("他说："+command)))
        self.assertFalse(self.runtime.capture(message(command, owner=OTHER)))
        self.assertFalse(self.runtime.capture(message(command, group="22222222")))
        self.assertTrue(self.runtime.capture(message(command)))
        self.assertFalse(self.runtime.capture(message(command)))
        self.assertEqual(self.bot.client.calls, [])

    async def test_foreground_activity_skips_budget_and_invalidates_waiting_poll(self):
        row = await self.active()
        state = {"engaged_until": time.time()+60, "latest_receipt": "first"}
        self.bot._attention = Section(enabled=lambda: True, store=Section(state=lambda owner: state))
        await self.runtime.tick()
        self.assertEqual(self.runtime.store.get(OWNER, row["id"])["polls"], 0)
        self.assertEqual(self.bot.client.calls, [])
        state["engaged_until"] = 0
        async def message_arrives():
            state.update(engaged_until=time.time()+60, latest_receipt="new-message")
        self.bot.client.callback = message_arrives
        self.bot.client.response = {"task_id": TASK, "state": "completed"}
        await self.runtime.tick()
        self.assertEqual(self.runtime.store.get(OWNER, row["id"])["status"], "active")
        self.assertEqual(self.runtime.last_status, "context_changed")

    async def test_new_private_context_invalidates_poll_with_attention_disabled(self):
        row = await self.active()
        self.bot._unified = Section(_generation=0, _private={"owner-private": "in1"},
                                   _contexts={("owner-private", "in1"): {"scope": "private:"+OWNER}})
        async def message_arrives():
            self.bot._unified._private["owner-private"] = "in2"
            self.bot._unified._contexts[("owner-private", "in2")] = {"scope": "private:"+OWNER}
        self.bot.client.callback = message_arrives
        self.bot.client.response = {"task_id": TASK, "state": "completed"}
        await self.runtime.tick()
        self.assertEqual(self.runtime.store.get(OWNER, row["id"])["result"], "pending")

    async def test_real_shared_delivery_helper_requires_bot_and_owner_destination(self):
        self.patcher.stop()
        self.bind()
        draft = await self.watch()
        self.bot.ctx.chat = Section(get_stream_by_user_id=AsyncMock(return_value={"session_id": "owner-private"}))
        outgoing = message(draft["delivery_text"], owner="99999999", mid="sent1")
        for kwargs in ({"sent": False, "message": outgoing}, {"sent": True, "message": message(draft["delivery_text"])},
                       {"sent": True, "message": message(draft["delivery_text"], owner="99999999", group="22222222")}):
            self.assertFalse(await self.runtime.sent(kwargs))
        self.bot.ctx.chat.get_stream_by_user_id.return_value = {"session_id": "somebody-else"}
        self.assertFalse(await self.runtime.sent({"sent": True, "message": outgoing}))
        self.bot.ctx.chat.get_stream_by_user_id.return_value = {"session_id": "owner-private"}
        self.assertTrue(await self.runtime.sent({"sent": True, "message": outgoing}))
        self.assertEqual(self.runtime.store.get(OWNER, draft["data"]["id"])["status"], "active")

    async def test_twelve_old_service_jobs_cannot_starve_current_service(self):
        stale_ids = []
        for index in range(12):
            task = format(index+1, "032x")
            self.bind(task=task)
            draft = await self.watch(task=task)
            self.assertTrue(await self.send(draft["delivery_text"], "old-promise-"+str(index)))
            stale_ids.append(draft["data"]["id"])
        self.bot.servers["cap"].url = "http://new-service:8000/mcp"
        self.bind(task=TASK)
        current = await self.watch(task=TASK)
        self.assertTrue(await self.send(current["delivery_text"], "new-promise"))
        self.bot.client.response = {"task_id": TASK, "state": "completed"}
        await self.runtime.tick()
        self.assertEqual(self.bot.client.calls, [("jm_get_download_status", {"task_id": TASK})])
        self.assertEqual(self.runtime.store.get(OWNER, current["data"]["id"])["status"], "ready")
        for ident in stale_ids:
            row = self.runtime.store.get(OWNER, ident)
            self.assertEqual((row["polls"], row["lease_token"]), (0, ""))
        control = self.runtime.store._read("SELECT daily_checks FROM followthrough_control WHERE owner_id=?", (OWNER,))[0]
        self.assertEqual(control["daily_checks"], 1)

    async def test_no_current_binding_spends_no_poll_budget_or_lease(self):
        row = await self.active()
        self.bot.servers["cap"].url = "http://new-service:8000/mcp"
        await self.runtime.tick()
        self.assertEqual(self.bot.client.calls, [])
        current = self.runtime.store.get(OWNER, row["id"])
        self.assertEqual((current["polls"], current["lease_token"]), (0, ""))
        control = self.runtime.store._read("SELECT daily_checks,last_check FROM followthrough_control WHERE owner_id=?", (OWNER,))[0]
        self.assertEqual((control["daily_checks"], control["last_check"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
