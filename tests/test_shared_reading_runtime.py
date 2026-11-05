"""Real source, reading, identity and context stores with an offline Host receipt double."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from MaiLivingWorld.companion_runtime import CompanionRuntime
from MaiLivingWorld.self_state import SelfState
from MaiLivingWorld.shared_reading_runtime import SharedReadingRuntime
from MaiLivingWorld.shared_reading_store import command_text, digest
from MaiLivingWorld.tool_store import ToolStore
from MaiLivingWorld.unified_runtime import UnifiedRuntime
from test_shared_reading_store import OWNER, OTHER, message


class Settings(SimpleNamespace):
    def model_dump(self):
        return dict(vars(self))


class SharedReadingRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        b = self.bot = SimpleNamespace()
        b.ctx = SimpleNamespace(paths=SimpleNamespace(data_dir=Path(self.temp.name)),
            chat=SimpleNamespace(get_stream_by_user_id=AsyncMock(return_value={"stream_id": "private-one"})))
        b.config = SimpleNamespace(owner=Settings(enabled=True, qq=OWNER), plugin=Settings(enabled=True),
            shared_reading=Settings(enabled=True), reading=Settings(enabled=True), mcp=Settings(enabled=True),
            mcp_pixiv=Settings(enabled=True, url="http://first"), mcp_cap=Settings(enabled=True, url="http://cap"))
        b._mcp_generation = 0
        b._ledger_storage_options = lambda: {}
        b._self_state = SelfState(Path(self.temp.name)/"self.sqlite3")
        b._self_state.configure_owner(OWNER)
        b._unified = UnifiedRuntime(b._self_state)
        b._tool_store = ToolStore(Path(self.temp.name)/"tools.sqlite3")
        b._ensure_tool_store = lambda: b._tool_store
        b._mcp_server_config = lambda provider: getattr(b.config, "mcp_"+provider)
        b._mcp_signature = lambda provider: digest(b._mcp_server_config(provider).model_dump())
        b._mcp_source_signature = b._mcp_signature
        b._resolve_roots = lambda ids: {row["id"]: row for row in b._tool_store.resolve(ids)}
        b._companion = CompanionRuntime(b)
        b._ensure_companion = lambda: b._companion
        self.runtime = SharedReadingRuntime(b)
        source = b._tool_store.put_observation("pixiv", "reading:novel:123:source", "version 1", "private:"+OWNER,
                                              subject_id=OWNER, origin_key="work:novel:123")
        self.ref = b._companion._ref(source)
        row = b._companion.store.open_reading("pixiv", "novel", "123", "1", owner_id=OWNER, scope="private:"+OWNER,
                                            source_refs=[self.ref], total_units=1000, title="合成共读")
        self.rid = row["id"]
        self.add_unit("第一段只有森林与旅人。")
        self.add_unit("第二段出现未读结局。")
        self.capture("开始", "start")
        self.capture("看到", "position", units=1)
        self.context()

    def add_unit(self, text, *, reviewed=True):
        c = self.bot._companion.store
        row = c.reading(self.rid, owner_id=OWNER)
        claim = c.claim_reading(self.rid, owner_id=OWNER, expected_revision=row["revision"])
        return c.complete_unit(self.rid, claim["token"], owner_id=OWNER, expected_revision=claim["revision"],
            version="1", index=row["cursor"], unit_id="unit-"+str(row["cursor"]), source_refs=[self.ref], excerpt=text,
            note={"summary": text, "quotes": [text], "questions": [], "self_response": ""},
            note_status="reviewed" if reviewed else "pending")

    def capture(self, label, action, *, units=0, allow=False, mid=None, **kw):
        return self.runtime.capture(message(mid=mid or label+str(units)+str(allow),
            text=command_text(action, self.rid, units=units, allow_spoilers=allow), **kw))

    def context(self, mid="current", sid="private-one", uid=OWNER, group=None):
        event = message(mid=mid, sid=sid, uid=uid, group=group, text="一起聊这本作品")
        self.bot._unified.capture(event, private_users=[OWNER, OTHER], group_ids=["22222222"])
        return {"message": event}

    async def asyncTearDown(self):
        self.runtime.close()
        self.bot._companion.close()
        self.bot._tool_store.close()
        self.bot._self_state.close()
        self.temp.cleanup()

    async def test_boundary_notes_only_reviewed_prefix_and_receipt_marks_mentioned(self):
        result = await self.runtime.tool("notes", self.rid, kwargs=self.context())
        self.assertTrue(result["success"])
        self.assertIn("森林", result["data"]["excerpt"])
        self.assertNotIn("未读结局", str(result))
        self.assertFalse((await self.runtime.tool("notes", self.rid, 2, self.context()))["success"])
        prepared = await self.runtime.tool("discuss", self.rid, 1, self.context())
        row = self.runtime.store.session(OWNER, self.rid)
        self.assertEqual(row["discussed_units"], 0)
        out = message(mid="out", uid="99999999", text=prepared["delivery_text"])
        self.assertFalse(await self.runtime.sent({"sent": False, "message": out}))
        self.assertTrue(await self.runtime.sent({"sent": True, "message": out}))
        self.assertEqual(self.runtime.store.session(OWNER, self.rid)["discussed_units"], 1)
        self.assertFalse(await self.runtime.sent({"sent": True, "message": out}))
        self.assertFalse((await self.runtime.tool("discuss", self.rid, kwargs=self.context()))["success"])

    async def test_rewind_pause_or_service_change_rejects_prepared_delivery(self):
        prepared = await self.runtime.tool("discuss", self.rid, 1, self.context())
        out = message(mid="out", uid="99999999", text=prepared["delivery_text"])
        self.capture("退回", "position", units=0)
        self.assertFalse(await self.runtime.sent({"sent": True, "message": out}))
        self.capture("又读到", "position", units=1)
        prepared = await self.runtime.tool("discuss", self.rid, 1, self.context())
        self.bot.config.mcp_pixiv.url = "http://different"
        self.assertFalse(await self.runtime.sent({"sent": True, "message": message(mid="later", uid="99999999", text=prepared["delivery_text"])}))
        self.assertFalse((await self.runtime.tool("notes", self.rid, 1, self.context()))["success"])
        self.assertTrue(self.runtime.protected_work("pixiv", "novel", "123"))

    async def test_stopped_or_disabled_source_keeps_boundary_until_owner_allows(self):
        self.capture("停下", "stop")
        self.bot.config.reading.enabled = self.bot.config.mcp.enabled = self.bot.config.mcp_pixiv.enabled = False
        self.assertTrue(self.runtime.protected_work("pixiv", "novel", "123"))
        self.assertFalse((await self.runtime.tool("notes", self.rid, 1, self.context()))["success"])
        self.assertTrue(self.capture("允许", "spoilers", allow=True))
        self.assertFalse(self.runtime.protected_work("pixiv", "novel", "123"))
        self.assertTrue(self.capture("禁止", "spoilers"))
        self.assertTrue(self.runtime.protected_work("pixiv", "novel", "123"))

    async def test_untrusted_and_stale_context_cannot_read_or_change(self):
        old = self.context(mid="old")
        self.context(mid="new")
        requests = [old, {"message": {}}, self.context(uid=OTHER, sid="other"),
                    self.context(group="22222222", sid="group-one")]
        for request in requests:
            self.assertFalse((await self.runtime.tool("list", kwargs=request))["success"])
        self.assertFalse(self.capture("群命令", "position", units=2, group="22222222"))
        self.assertFalse(self.capture("伪命令", "position", units=2, uid=OTHER))
        event = message(mid="media", text=command_text("position", self.rid, units=2))
        event["raw_message"] = [{"type": "image"}]
        self.assertFalse(self.runtime.capture(event))
        self.assertEqual(self.runtime.store.session(OWNER, self.rid)["owner_units"], 1)

    async def test_real_sender_lookup_rechecks_owner_and_boundary_after_await(self):
        prepared = await self.runtime.tool("discuss", self.rid, 1, self.context())
        async def change(*args, **kwargs):
            self.capture("退回", "position", units=0)
            return {"stream_id": "private-one"}
        self.bot.ctx.chat.get_stream_by_user_id.side_effect = change
        self.assertFalse(await self.runtime.sent({"sent": True,
            "message": message(mid="out", uid="99999999", text=prepared["delivery_text"])}))
        self.assertEqual(self.runtime.store.session(OWNER, self.rid)["discussed_units"], 0)

    async def test_filter_blocks_whole_work_and_recursive_derivatives_without_deleting_memory(self):
        raw = self.bot._tool_store.put_observation("pixiv", "reading:novel:123:1:0:0", "未读结局",
            "private:"+OWNER, subject_id=OWNER, origin_key="work:novel:123")
        unrelated = {"id": "elsewhere", "source": "story", "text": "不相关公开资料"}
        middle = {"id": "derived-one", "sources": [{"id": raw["id"]}], "text": "改写结局"}
        outer = {"id": "derived-two", "derived_from": [middle["id"]], "text": "再改写结局"}
        items = [middle, outer, unrelated]
        self.assertEqual(self.runtime.filter_records(items, OWNER), [unrelated])
        self.assertEqual(self.runtime.filter_records([raw], OWNER), [])
        self.assertIsNotNone(self.bot._tool_store.resolve([raw["id"]])[0])
        self.assertEqual(self.runtime.filter_records(items, OTHER), items)

    async def test_pending_notes_never_present_as_read_understanding(self):
        self.add_unit("第三段尚未经过独立复核。", reviewed=False)
        self.capture("看到第三段", "position", units=3)
        self.assertFalse((await self.runtime.tool("notes", self.rid, 3, self.context()))["success"])
        result = await self.runtime.tool("list", kwargs=self.context())
        self.assertEqual(result["data"][0]["ai_fetched_units"], 3)
        self.assertEqual(result["data"][0]["last_mentioned_unit"], 0)

    async def test_numeric_resource_aliases_cannot_bypass_protection(self):
        self.assertTrue(self.runtime.protected_work("pixiv", "novel", "00123"))
        # cap accepts leading-zero jm_id arguments and normalizes them before fetching.
        row = self.bot._companion.store.open_reading("cap", "pdf", "456", "1", owner_id=OWNER,
            scope="private:"+OWNER, source_refs=[self.ref], total_units=2, title="合成PDF")
        self.rid = row["id"]
        self.assertTrue(self.capture("PDF开始", "start"))
        self.assertTrue(self.runtime.protected_work("cap", "pdf", "000456"))
        # Imported older rows can retain a numeric alias. Protect both stored origins.
        alias = self.bot._companion.store.open_reading("pixiv", "novel", "00123", "1", owner_id=OWNER,
            scope="private:"+OWNER, source_refs=[self.ref], total_units=1000, title="旧编号别名")
        self.rid = alias["id"]
        self.assertTrue(self.capture("别名开始", "start"))
        originals = [self.bot._tool_store.put_observation("pixiv", "alias:"+ident, "受保护原文",
            "private:"+OWNER, subject_id=OWNER, origin_key="work:novel:"+ident) for ident in ("123", "00123")]
        self.assertEqual(self.runtime.filter_records(originals, OWNER), [])
        works = [row for row in self.runtime.protected_works() if row["provider"] == "pixiv"]
        self.assertEqual(len(works), 1)
        self.assertEqual(works[0]["resource_id"], "123")

    async def test_restart_keeps_independent_positions_and_source_retraction_blocks_notes(self):
        prepared = await self.runtime.tool("discuss", self.rid, 1, self.context())
        self.assertTrue(await self.runtime.sent({"sent": True,
            "message": message(mid="out", uid="99999999", text=prepared["delivery_text"])}))
        self.runtime.close()
        self.runtime = SharedReadingRuntime(self.bot)
        state = await self.runtime.tool("list", kwargs=self.context(mid="restart", sid="second-session"))
        self.assertEqual((state["data"][0]["ai_fetched_units"], state["data"][0]["owner_confirmed_units"],
                          state["data"][0]["last_mentioned_unit"]), (2, 1, 1))
        self.bot._tool_store.retract_origin("pixiv", "work:novel:123")
        self.assertFalse((await self.runtime.tool("notes", self.rid, 1, self.context()))["success"])
        self.assertTrue(self.runtime.protected_work("pixiv", "novel", "123"))

    async def test_expired_or_embedded_quote_and_foreign_recipient_do_not_advance(self):
        from unittest.mock import patch
        prepared = await self.runtime.tool("discuss", self.rid, 1, self.context())
        draft = self.runtime.store.session(OWNER, self.rid)["draft"]
        for changes in ({"sid": "someone-else"}, {"group": "22222222"}, {"uid": OWNER},
                        {"text": "引用："+prepared["delivery_text"]}):
            options = {"mid": "out", "uid": "99999999", "text": prepared["delivery_text"], **changes}
            self.assertFalse(await self.runtime.sent({"sent": True, "message": message(**options)}))
        with patch("time.time", return_value=draft["until"]+1):
            self.assertFalse(await self.runtime.sent({"sent": True,
                "message": message(mid="late", uid="99999999", text=prepared["delivery_text"])}))
        self.assertEqual(self.runtime.store.session(OWNER, self.rid)["discussed_units"], 0)

    async def test_missing_or_cyclic_derived_dependencies_are_not_expressed(self):
        missing = {"id": "missing-derivative", "sources": [{"id": "tool:observation:"+"f"*64}], "text": "未知依赖"}
        first = {"id": "first", "sources": [{"id": "second"}], "text": "循环1"}
        second = {"id": "second", "sources": [{"id": "first"}], "text": "循环2"}
        self.assertEqual(self.runtime.filter_records([missing, first, second], OWNER), [])

    async def test_different_reading_has_unique_natural_delivery_marker(self):
        first = await self.runtime.tool("discuss", self.rid, 1, self.context())
        original = self.bot._companion.store.reading(self.rid, owner_id=OWNER)
        second = self.bot._companion.store.open_reading("pixiv", "novel", "456", "1", owner_id=OWNER,
            scope="private:"+OWNER, source_refs=[self.ref], total_units=1000, title="另一份合成作品")
        self.rid = second["id"]
        self.add_unit("第一段只有森林与旅人。")
        self.capture("第二本开始", "start")
        self.capture("第二本看到", "position", units=1)
        prepared = await self.runtime.tool("discuss", self.rid, 1, self.context())
        self.assertNotEqual(first["delivery_text"], prepared["delivery_text"])
        self.assertEqual(self.runtime.store.session(OWNER, self.rid)["label_number"], 2)
