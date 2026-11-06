"""Public-source discovery, memory and access control via the host plugin's normal entry points."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from MaiLivingWorld.tests.test_mcp_plugin import GROUP, OTHER, OWNER, message, schema
from MaiLivingWorld.tests.test_runtime import FakeContext, plugin


NEWS_ID = "ithome:0:875:123"
NEWS_URL = "https://www.ithome.com/0/875/123.htm"
NEWS = {"id": NEWS_ID, "source": "IT之家", "url": NEWS_URL, "title": "火星沙丘观测",
        "published_at": "2026-11-06T02:00:00+00:00", "basis": "article", "text": "观测小组比较了沙丘两侧的风蚀痕迹。"}
REFERENCE = {"page_id": 42, "language": "zh", "revision_id": 98, "offset": 0, "next_offset": None,
             "title": "木卫二", "source": "中文维基百科", "url": "https://zh.wikipedia.org/wiki/Europa",
             "updated_at": "2026-10-01T00:00:00Z", "text": "冰壳中可见线状裂纹。", "basis": "encyclopedia_excerpt"}


def fixture_client():
    string, integer = {"type": "string"}, {"type": "integer"}
    definitions = {
        "news_search": schema({"query": string}, []), "news_read": schema({"id": string}, ["id"]),
        "web_search": schema({"query": string}, ["query"]), "web_read": schema({"id": string}, ["id"]),
        "reference_search": schema({"query": string, "language": string}, ["query"]),
        "reference_read": schema({"page_id": integer, "language": string, "offset": integer}, ["page_id"]),
        "mastodon_search": schema({"query": string}, ["query"]), "mastodon_reply": schema({}, []),
        "expose_private_history": schema({}, []),
    }
    return SimpleNamespace(retired=False, close=AsyncMock(), call_tool=AsyncMock(),
        list_tools=AsyncMock(return_value={name: {"input_schema": value} for name, value in definitions.items()}))


class PublicToolsPluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.client = fixture_client()
        self.factory = self.enterContext(patch.object(plugin, "StdioMCPClient", return_value=self.client))
        self.bot = self.make_bot()

    def make_bot(self):
        bot = plugin.MaiLivingWorld()
        bot.ctx = FakeContext()
        bot.ctx.paths = SimpleNamespace(data_dir=self.folder)
        bot.config = plugin.Config()
        bot.config.owner.enabled = True
        bot.config.owner.qq = OWNER
        bot.config.brain_memory.group_ids = [GROUP]
        bot.config.brain_memory.private_users = [OTHER]
        bot.config.unified_mind.enabled = True
        bot.config.unified_mind.mode = "active"
        bot.config.mcp.enabled = True
        bot.config.mcp.public_sources_enabled = True
        self.addAsyncCleanup(bot.on_unload)
        return bot

    async def capture(self, mid, text, **audience):
        event = message(mid, text=text, **audience)
        event["raw_message"] = [{"type": "text", "data": text}]
        await self.bot.capture_unified(message=event)
        return event

    async def call(self, event, provider, tool, args):
        return await self.bot.mcp_call(provider, tool, args, message=event)

    async def test_discovery_separates_credentials_and_public_social_operations(self):
        secrets = {"MLW_BRAVE_SEARCH_KEY": "test-search-key", "MLW_MASTODON_INSTANCE": "https://social.example",
                   "MLW_MASTODON_TOKEN": "test-account-token", "UNRELATED_HOST_SECRET": "test-host-secret"}
        event = await self.capture("catalog", "有哪些新闻？")
        self.bot.config.mcp.social_mode = "参与讨论"
        self.bot.config.mcp.allow_writes = True
        self.bot.config.mcp.allow_account_actions = True
        expected = {
            "news": ({"news_search", "news_read", "web_search", "web_read"}, {"MLW_BRAVE_SEARCH_KEY"}),
            "reference": ({"reference_search", "reference_read"}, set()),
            "mastodon": ({"mastodon_search"}, {"MLW_MASTODON_INSTANCE", "MLW_MASTODON_TOKEN"}),
        }
        with patch.dict(os.environ, secrets):
            for provider, (allowed, inherited) in expected.items():
                catalog = await self.bot.mcp_tools(provider, message=event)
                self.assertTrue(catalog["success"], catalog)
                self.assertEqual({item["name"] for item in json.loads(catalog["content"])}, allowed)
                executable, argv, cwd, env = self.factory.call_args.args
                self.assertEqual(executable, sys.executable)
                self.assertEqual(argv, [str(Path(plugin.__file__).resolve().with_name("public_mcp_server.py")), provider])
                self.assertEqual(Path(cwd), Path(argv[0]).parent)
                self.assertEqual(set(env), {"PYTHONUTF8", "PYTHONDONTWRITEBYTECODE"} | inherited)
                self.assertEqual({k: env[k] for k in inherited}, {k: secrets[k] for k in inherited})
            result = await self.call(event, "mastodon", "mastodon_reply", {"id": "23", "text": "hello"})
        self.assertFalse(result["success"])
        self.client.call_tool.assert_not_called()

    async def test_search_is_ephemeral_read_becomes_public_memory_and_budget_survives_restart(self):
        self.bot.config.mcp.max_daily_calls = 2
        event = await self.capture("browse", "我给这次聊天取个名字：金色的灯塔。请查查星球。")
        self.client.call_tool.return_value = {"items": [{"id": NEWS_ID, "title": "搜索页专有标题"}]}
        result = await self.call(event, "news", "news_search", {"query": "星球"})
        self.assertTrue(result["success"], result)
        self.assertEqual(self.bot._tool_store.observations(), [])
        self.client.call_tool.return_value = NEWS
        result = await self.call(event, "news", "news_read", {"id": NEWS_ID})
        self.assertTrue(result["success"], result)
        self.assertIn(NEWS_URL, result["content"])
        root, = self.bot._tool_store.observations()
        self.assertEqual((root["scope"], root["sensitivity"], root["subject_id"]), ("public:news", "public", ""))
        self.assertEqual((root["evidence_status"], root["url"]), ("publisher_excerpt", NEWS_URL))
        for value in (NEWS["source"], NEWS["text"], NEWS["title"], NEWS["published_at"]):
            self.assertIn(value, root["text"])
        for excluded in ("搜索页专有标题", "金色的灯塔", "星球", OWNER):
            self.assertNotIn(excluded, str(root))
        await self.bot.on_unload()
        self.bot = self.make_bot()
        self.bot.config.mcp.max_daily_calls = 2
        group_event = await self.capture("group-after-restart", "提到的沙丘报道", group=GROUP, sid="different-group")
        self.assertIn(root["id"], {r["id"] for r in self.bot._collect_roots("沙丘")})
        self.bot.config.mcp.allow_group_reads = True
        denied = await self.call(group_event, "news", "news_search", {})
        self.assertFalse(denied["success"], denied)
        self.assertIn("budget_exhausted", denied["content"])
        self.assertEqual(self.client.call_tool.await_count, 2)

    async def test_online_queries_require_public_terms_from_current_trusted_message(self):
        self.bot.config.mcp.social_mode = "只逛逛"
        self.client.call_tool.return_value = {"items": []}
        for mid, text, topic in [("absent", "聊聊天文望远镜", "室友小王"),
                ("secret", "木卫二观测是秘密，别告诉别人", "木卫二"), ("personal", "号码 1234567890", "1234567890")]:
            event = await self.capture(mid, text)
            for provider, tool in [("news", "web_search"), ("reference", "reference_search"),
                                   ("mastodon", "mastodon_search")]:
                result = await self.call(event, provider, tool, {"query": topic})
                self.assertFalse(result["success"], result)
                self.assertIn("ask_for_public_search_terms", result["content"])
        self.client.call_tool.assert_not_called()
        event = await self.capture("public-topic", "想了解木卫二的表面地形")
        result = await self.call(event, "reference", "reference_search", {"query": "木卫二"})
        self.assertTrue(result["success"], result)
        self.client.call_tool.assert_awaited_once_with("reference_search", {"query": "木卫二"})
        self.assertEqual(self.bot._tool_store.observations(), [])
        self.client.call_tool.return_value = REFERENCE
        result = await self.call(event, "reference", "reference_read", {"page_id": 42})
        self.assertTrue(result["success"], result)
        memory, = self.bot._tool_store.observations()
        self.assertEqual((memory["evidence_status"], memory["scope"], memory["url"]),
                         ("encyclopedia_excerpt", "public:reference", REFERENCE["url"]))

    async def test_public_group_access_requires_enabled_group_and_never_inherits_account_access(self):
        self.bot.config.mcp.social_mode = "参与讨论"
        self.client.call_tool.return_value = {"items": []}
        args = {"query": "天文"}
        event = await self.capture("group", "查查天文新闻", group=GROUP, uid=OTHER, sid="group-one")
        self.assertFalse((await self.call(event, "news", "news_search", args))["success"])
        self.bot.config.mcp.owner_only = False
        self.assertFalse((await self.call(event, "news", "news_search", args))["success"])
        self.bot.config.mcp.allow_group_reads = True
        allowed = await self.call(event, "news", "news_search", args)
        self.assertTrue(allowed["success"], allowed)
        self.assertFalse((await self.call(event, "mastodon", "mastodon_search", args))["success"])
        self.bot.config.mcp.public_sources_enabled = False
        self.assertFalse((await self.bot.mcp_tools("news", message=event))["success"])
        self.client.call_tool.assert_awaited_once_with("news_search", args)

    async def test_reference_revision_is_preserved_and_updates_same_excerpt_identity(self):
        from MaiLivingWorld.growth_store import source_fingerprint
        records = []
        for revision in (123456789, 123456790):
            self.client.call_tool.return_value = {**REFERENCE, "revision_id": revision}
            event = await self.capture("revision-"+str(revision), "请读木卫二百科")
            result = await self.call(event, "reference", "reference_read", {"page_id": 42})
            self.assertTrue(result["success"], result)
            row, = self.bot._tool_store.observations()
            self.assertIn("修订"+str(revision), row["text"])
            records.append(row)
        self.assertEqual(records[0]["id"], records[1]["id"])
        self.assertEqual(records[0]["origin_key"], records[1]["origin_key"])
        self.assertEqual(records[0]["url"], records[1]["url"])
        self.assertNotEqual(source_fingerprint(records[0]), source_fingerprint(records[1]))

    async def test_realistic_reference_revision_survives_normal_reply_after_restart(self):
        from MaiLivingWorld.tests.test_unified_runtime import kwargs, response
        event = await self.capture("read-long-revision", "请读木卫二百科")
        self.client.call_tool.return_value = {**REFERENCE, "revision_id": 123456789}
        self.assertTrue((await self.call(event, "reference", "reference_read", {"page_id": 42}))["success"])
        await self.bot.on_unload()
        self.bot = self.make_bot()
        self.bot.config.unified_mind.min_interval_seconds = 0
        record, = self.bot._ensure_tool_store().observations()
        await self.capture("recall-long-revision", "木卫二百科介绍了什么？", group=GROUP, uid=OTHER, sid="other-group")
        self.bot.ctx.llm.generate.return_value = response([{"id": record["id"], "mode": "say"}])
        hint = await self.bot.brain_reply_hint(**kwargs("other-group", "recall-long-revision"))
        reply = json.dumps(hint, ensure_ascii=False)
        self.assertIn("冰壳中可见线状裂纹", reply)
        self.assertIn(REFERENCE["url"], reply)
        self.assertIn("123456789", reply)

    async def test_reference_without_verified_revision_cannot_create_memory(self):
        for index, revision in enumerate((None, True, False, 0, -1, "98", 98.0)):
            with self.subTest(revision=revision):
                self.client.call_tool.return_value = {**REFERENCE, "revision_id": revision}
                event = await self.capture("invalid-revision-"+str(index), "请读木卫二百科")
                await self.call(event, "reference", "reference_read", {"page_id": 42})
                self.assertEqual(self.bot._tool_store.observations(), [])

    async def test_external_search_never_extracts_an_unrequested_private_name(self):
        self.bot.config.mcp.social_mode = "只逛逛"
        self.client.call_tool.return_value = {"items": []}
        for mid, text in (
                ("private-name", "我室友周小宁今天睡得很差"),
                ("different-request", "我室友周小宁今天睡得很差，帮我查一下木卫二"),
                ("quoted", "朋友说“帮我查周小宁”，我只是转述一下"),
                ("quoted-clauses", "朋友说“查木卫二，搜索周小宁”，我只是转述一下"),
                ("hypothetical", "如果你搜周小宁会发生什么"),
                ("refused", "不要搜周小宁"),
                ("descriptive", "周小宁最近想了解天文")):
            event = await self.capture(mid, text)
            for provider, tool in (("news", "web_search"), ("reference", "reference_search"),
                                   ("mastodon", "mastodon_search")):
                result = await self.call(event, provider, tool, {"query": "周小宁"})
                self.assertFalse(result["success"], (mid, result))
                self.assertIn("ask_for_public_search_terms", result["content"])
        self.client.call_tool.assert_not_called()
        self.assertEqual(self.bot._ensure_tool_store().status()["call_budget"]["count"], 0)

        for mid, text, topic in (
                ("direct", "我室友周小宁今天睡得很差，帮我查一下木卫二", "木卫二"),
                ("quoted-object", "请搜索“木卫二”", "木卫二"),
                ("english", "Please search for Europa", "Europa")):
            event = await self.capture(mid, text)
            result = await self.call(event, "reference", "reference_search", {"query": topic})
            self.assertTrue(result["success"], (mid, result))
        self.assertEqual([call.args[1] for call in self.client.call_tool.await_args_list],
                         [{"query": "木卫二"}, {"query": "木卫二"}, {"query": "Europa"}])

    async def test_search_request_must_come_from_original_text_and_the_current_context(self):
        self.client.call_tool.return_value = {"items": []}
        raw = [{"type": "text", "data": "请搜索木卫二"}]
        variants = (
            {"raw_message": [{"type": "forward", "data": {}}]},
            {"raw_message": raw, "reply_to": "quoted-message"},
            {"raw_message": [{"type": "text", "data": "我只是在转述"}]},
            {"raw_message": raw + [{"type": "image", "data": "fixture"}]},
            {},
        )
        for index, changes in enumerate(variants):
            event = {**message(f"source-{index}", text="请搜索木卫二"), **changes}
            await self.bot.capture_unified(message=event)
            result = await self.call(event, "reference", "reference_search", {"query": "木卫二"})
            self.assertFalse(result["success"], (index, result))
        self.client.call_tool.assert_not_called()

        first = await self.capture("asked-before", "请搜索木卫二")
        await self.capture("new-message", "现在只想和你聊聊")
        result = await self.bot.mcp_call("reference", "reference_search", {"query": "木卫二"},
                                        session_id=first["session_id"])
        self.assertFalse(result["success"], result)
        self.client.call_tool.assert_not_called()

    async def test_search_objects_cannot_expand_into_a_private_or_reported_tail(self):
        self.bot.config.mcp.social_mode = "只逛逛"
        self.client.call_tool.return_value = {"items": []}
        for index, (text, topic) in enumerate((
                ("搜索“木卫二”时我室友叫周小宁", "周小宁"),
                ("搜索“木卫二”时我室友叫周小宁", "木卫二"),
                ("不要搜索木卫二，搜周小宁是我舍友说的", "周小宁"),
                ("“查木卫二”只是举例", "木卫二"),
                ("查木卫二的时候我室友周小宁来敲门", "周小宁"),
                ("查木卫二的时候我室友周小宁来敲门", "木卫二"),
                ("搜索“木卫二”只是举例", "木卫二"))):
            event = await self.capture(f"tail-{index}", text)
            for provider, tool in (("news", "web_search"), ("reference", "reference_search"),
                                   ("mastodon", "mastodon_search")):
                result = await self.call(event, provider, tool, {"query": topic})
                self.assertFalse(result["success"], (text, result))
        self.client.call_tool.assert_not_called()
        self.assertEqual(self.bot._ensure_tool_store().status()["call_budget"]["count"], 0)

        event = await self.capture("bounded-object", "请搜索“木卫二”的公开资料")
        self.assertTrue((await self.call(event, "reference", "reference_search", {"query": "木卫二"}))["success"])
        self.client.call_tool.assert_awaited_once_with("reference_search", {"query": "木卫二"})

    async def test_incomplete_quotes_and_code_never_authorize_hidden_requests(self):
        self.client.call_tool.return_value = {"items": []}
        for index, text in enumerate((
                "朋友说“明天看看，搜索木卫二",
                "朋友说「明天看看，搜索木卫二",
                '朋友说"明天看看，搜索木卫二',
                "朋友说‘明天看看，搜索木卫二",
                "朋友说'明天看看，搜索木卫二",
                "下面是一段示例\n`\n搜索木卫二",
                "下面是一段示例\n```text\n搜索木卫二\n```",
                "下面是一段示例\n```text\n搜索木卫二",
                "下面是一段示例\n> 搜索木卫二")):
            event = await self.capture(f"quote-{index}", text)
            result = await self.call(event, "reference", "reference_search", {"query": "木卫二"})
            self.assertFalse(result["success"], (text, result))
        self.client.call_tool.assert_not_called()
        for index, text in enumerate(("请搜索“木卫二”", "请搜索「木卫二」", "Please search for 'Europa'")):
            event = await self.capture(f"complete-{index}", text)
            topic = "Europa" if "Europa" in text else "木卫二"
            self.assertTrue((await self.call(event, "reference", "reference_search", {"query": topic}))["success"])

    async def test_reply_after_restart_cites_only_an_approved_persisted_public_source(self):
        from MaiLivingWorld.tests.test_unified_runtime import kwargs, response
        from MaiLivingWorld.unified_expression import expression_hint, parse_expression

        event = await self.capture("read-source", "帮我看这篇沙丘的报道")
        self.client.call_tool.return_value = NEWS
        self.assertTrue((await self.call(event, "news", "news_read", {"id": NEWS_ID}))["success"])
        await self.bot.on_unload()
        self.bot = self.make_bot()
        self.bot.config.unified_mind.min_interval_seconds = 0
        record, = self.bot._ensure_tool_store().observations()
        fake_url = "https://invented.example/not-the-source"
        for mid, decision, permitted in [("allowed", {"mode": "say"}, True), ("internal", {"mode": "internal"}, False),
                                        ("model-supplied", {"mode": "say", "source_url": fake_url}, False)]:
            await self.capture(mid, "沙丘报道是从哪里看到的？", group=GROUP, uid=OTHER, sid="different-group")
            self.bot.ctx.llm.generate.return_value = response([{"id": record["id"], **decision}])
            hint = await self.bot.brain_reply_hint(**kwargs("different-group", mid))
            reply = json.dumps(hint, ensure_ascii=False)
            self.assertEqual(NEWS_URL in reply, permitted)
            if permitted:
                self.assertIn("来自查到的公开资料", reply)
                self.assertIn(NEWS["title"], reply)
            self.assertNotIn(NEWS_URL, self.bot.ctx.llm.generate.await_args.args[0])
            self.assertNotIn(fake_url, reply)

        audience = {"audience_scope": "group:" + GROUP, "speaker_id": OTHER, "is_owner": False}
        ask = response([{"id": record["id"], "mode": "say"}])
        for changes in ({"url": fake_url}, {"url": NEWS_URL + "?token=test"}, {"url": NEWS_URL + "#fragment"},
                        {"url": NEWS_URL.replace("https://", "https://bob@")}, {"url": NEWS_URL + "x" * 1000},
                        {"source": "brain_event"}, {"evidence_status": "search_snippet"}, {"derived": True},
                        {"scope": "private:" + OWNER, "subject_id": OWNER, "sensitivity": "personal"}):
            plan = parse_expression(ask, [{**record, **changes}], **audience)
            self.assertNotIn("source_url", expression_hint(plan), changes)

    async def test_mismatched_article_and_search_snippet_cannot_become_article_evidence(self):
        for mid, change in [("wrong-resource", {"id": "ithome:0:111:222"}),
                            ("wrong-origin", {"url": "https://different.example/news"}),
                            ("not-article", {"basis": "search_snippet"}), ("blocked", {"basis": "unavailable"})]:
            event = await self.capture(mid, "请读一下那篇报道")
            self.client.call_tool.return_value = {**NEWS, **change}
            await self.call(event, "news", "news_read", {"id": NEWS_ID})
            self.assertEqual(self.bot._tool_store.observations(), [], mid)

        event = await self.capture("feed-summary", "可以看一下摘要吗")
        self.client.call_tool.return_value = {**NEWS, "basis": "feed_summary"}
        result = await self.call(event, "news", "news_read", {"id": NEWS_ID})
        self.assertTrue(result["success"], result)
        memory, = self.bot._tool_store.observations()
        self.assertEqual(memory["evidence_status"], "feed_summary")

        event = await self.capture("web-result", "看看刚才那个网页")
        ident = "web_" + "f" * 32
        url = "https://observatory.example/geology"
        self.client.call_tool.return_value = {"id": ident, "basis": "web_excerpt", "source": "observatory.example",
            "url": url, "title": "地质资料", "text": "这里列出了陨石坑的直径范围。"}
        result = await self.call(event, "news", "web_read", {"id": ident})
        self.assertTrue(result["success"], result)
        evidence = {m["url"]: m["evidence_status"] for m in self.bot._tool_store.observations()}
        self.assertEqual(evidence, {NEWS_URL: "feed_summary", url: "web_excerpt"})

    async def test_public_links_remain_clickable_while_credentials_and_paths_are_redacted(self):
        status_url = "https://social.example/@observatory/900"
        public_topic = "光谱"
        private_values = ("https://observatory.example/log?signature=fixture-signed-link",
                          "https://name:private-userinfo@observatory.example/log",
                          "https://name@observatory.example/log", r"C:\private\report.txt",
                          "/srv/hidden/report.txt", "file:///private/research/source.txt")
        self.client.call_tool.return_value = {"items": [
            {"title": public_topic, "url": url} for url in (status_url, NEWS_URL, *private_values)]}
        self.bot.config.mcp.allow_group_reads = True
        for audience in ({}, {"sid": "group", "group": GROUP}):
            event = await self.capture("links", "查查光谱", **audience)
            result = await self.call(event, "news", "web_search", {"query": public_topic})
            self.assertTrue(result["success"], result)
            self.assertIn(status_url, result["content"])
            self.assertIn(NEWS_URL, result["content"])
            for hidden in ("fixture-signed-link", "private-userinfo", "name@observatory", "report.txt", "source.txt"):
                self.assertNotIn(hidden, result["content"])
