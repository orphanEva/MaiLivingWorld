"""Owner QQ input + normal planner MCP entry and real durable reply reservations."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from MaiLivingWorld.tests.test_mcp_plugin import GROUP, OTHER, OWNER, message
from MaiLivingWorld.tests.test_runtime import FakeContext, plugin
from MaiLivingWorld.tests.test_social_runtime import PublicFixture


BODY = "这张照片用了多长的曝光时间？"


class Mastodon(PublicFixture):
    def __init__(self):
        super().__init__()
        self.after_identity = None
        self.after_reply = None

    async def list_tools(self):
        tools = await super().list_tools()
        tools["mastodon_reply"] = {"input_schema": {"type": "object", "properties": {
            key: {"type": "string"} for key in ("id", "text", "idempotency_key")},
            "required": ["id", "text", "idempotency_key"]}}
        return tools

    async def call_tool(self, name, args):
        value = await super().call_tool(name, args)
        if name == "mastodon_identity" and self.after_identity:
            await self.after_identity()
        if name == "mastodon_reply" and self.after_reply:
            await self.after_reply()
        return value


class OwnerReplyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bot = plugin.MaiLivingWorld()
        self.bot.ctx = FakeContext()
        self.bot.ctx.paths = SimpleNamespace(data_dir=Path(self.tmp.name))
        self.bot.set_plugin_config({"owner": {"enabled": True, "qq": OWNER},
            "unified_mind": {"enabled": True, "mode": "active"},
            "brain_memory": {"include_owner": True, "group_ids": [GROUP], "private_users": [OTHER]},
            "mcp": {"enabled": True, "owner_only": False, "allow_group_reads": True, "social_mode": "参与讨论"},
            "general": {"dry_run": False}})
        self.site = Mastodon()
        self.bot._mcp_client = lambda provider: self.site
        self.args = {"id": "99", "text": BODY}

    async def asyncTearDown(self):
        try:
            await self.bot.on_unload()
        finally:
            self.tmp.cleanup()

    def event(self, mid="one", *, text="公开回复 99 " + BODY, **kwargs):
        event = message(mid, text=text, **kwargs)
        event["raw_message"] = [{"type": "text", "data": text}]
        return event

    async def send(self, event, *, args=None):
        await self.bot.capture_unified(message=event)
        return await self.bot.mcp_call("mastodon", "mastodon_reply", args if args is not None else self.args,
            session_id=event["session_id"], reply_message_id=event["message_id"])

    async def test_session_only_planner_reads_raw_verified_command_and_keeps_internal_key_private(self):
        event = self.event()
        await self.bot.capture_unified(message=event)
        kwargs = {"session_id": event["session_id"]}
        tools = await self.bot.mcp_tools("mastodon", **kwargs)
        reply = next(t for t in json.loads(tools["content"]) if t["name"] == "mastodon_reply")
        self.assertEqual(set(reply["parameters"]["properties"]), {"id", "text"})
        self.assertNotIn("idempotency_key", str(reply))
        trusted, _ = self.bot._mcp_context(kwargs)
        blocked = await self.bot._mcp_bridge.execute("mastodon", "mastodon_reply", self.args,
            config=self.bot.config.mcp, context=trusted, owner=OWNER, client=self.site, valid=lambda: True, timezone="UTC")
        self.assertFalse(blocked["success"])
        result = await self.bot.mcp_call("mastodon", "mastodon_reply", self.args, **kwargs)
        self.assertTrue(result["success"], result)
        self.assertIn("https://social.example/@robot/100", result["content"])
        self.assertEqual([name for name, _ in self.site.calls], ["mastodon_identity", "mastodon_reply"])
        sent = self.site.calls[-1][1]
        self.assertEqual((sent["id"], sent["text"]), ("99", BODY))
        self.assertRegex(sent["idempotency_key"], "^[a-f0-9]{64}$")
        again = await self.send(self.event("two"))
        self.assertFalse(again["success"])
        self.assertEqual(sum(name == "mastodon_reply" for name, _ in self.site.calls), 1)

    async def test_rejects_group_stranger_quotations_flattened_media_and_forged_arguments(self):
        events = [self.event(uid=OTHER), self.event(group=GROUP), self.event(text="有人说 公开回复 99 " + BODY)]
        for changes in (
                {"raw_message": None}, {"raw_message": [{"type": "forward", "data": "公开回复 99 " + BODY}]},
                {"reply_to": "old"}, {"is_picture": True}, {"raw_message": [{"type": "text", "data": "另一段话"}]},
                {"message_segment": {"type": "reply", "data": "previous"}}, {"account_id": OWNER}):
            events.append({**self.event(), **changes})
        for index, event in enumerate(events):
            with self.subTest(index=index):
                event["message_id"] = str(index)
                self.assertFalse((await self.send(event))["success"])
        for args in ({"id": 99, "text": BODY}, {"id": "98", "text": BODY}, {"id": "99", "text": BODY + "!"},
                     {**self.args, "idempotency_key": "a" * 64}):
            self.assertFalse((await self.send(self.event(), args=args))["success"])
        self.assertFalse(self.site.calls)

    async def test_disables_write_in_dry_run_browse_or_inactive_mind(self):
        for dry, mode, mind in ((True, "参与讨论", "active"), (False, "只逛逛", "active"), (False, "参与讨论", "shadow")):
            self.bot.config.general.dry_run = dry
            self.bot.config.mcp.social_mode = mode
            self.bot.config.unified_mind.mode = mind
            event = self.event()
            self.assertFalse((await self.send(event))["success"])
            tools = await self.bot.mcp_tools("mastodon", session_id=event["session_id"])
            self.assertNotIn('"mastodon_reply"', tools["content"])
        self.assertFalse(self.site.calls)

    async def test_latest_context_config_generation_and_source_before_and_after_dispatch(self):
        async def next_message():
            await self.bot.capture_unified(message=self.event("new", text="先等等"))
        async def rotate_source():
            self.bot._mcp_source_signature = lambda provider: "rotated"
        async def reload_config():
            self.bot._mcp_generation += 1
        async def dry_run():
            self.bot.config.general.dry_run = True
        for index, change in enumerate((next_message, reload_config, dry_run, rotate_source)):
            self.site.after_identity = change
            self.assertFalse((await self.send(self.event(str(index))))["success"])
            self.bot.config.general.dry_run = False
            self.assertFalse(any(name == "mastodon_reply" for name, _ in self.site.calls))
        self.site.after_identity = None
        self.site.after_reply = next_message
        result = await self.send(self.event("delayed"))
        self.assertFalse(result["success"])
        self.assertIn("context_changed", result["content"])
        result = await self.send(self.event("again"))
        self.assertFalse(result["success"])
        self.assertEqual(sum(name == "mastodon_reply" for name, _ in self.site.calls), 1)
