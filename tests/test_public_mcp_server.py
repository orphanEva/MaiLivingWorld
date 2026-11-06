"""Real stdio MCP exchange; use offline data handlers instead of external accounts or APIs."""
import importlib.util
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from MaiLivingWorld.mcp_client import MCPError, StdioMCPClient


SERVER = Path(__file__).resolve().parents[1] / "public_mcp_server.py"


@unittest.skipUnless(importlib.util.find_spec("mcp"), "MCP SDK required")
class PublicStdioServers(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / "renamed.plugin 安装"
        self.folder.mkdir()
        shutil.copyfile(SERVER, self.folder / SERVER.name)
        (self.folder / "public_news.py").write_text(
            "def news_search(query='', limit=5): return {'items': [], 'topic': query}\n"
            "def news_read(id): return {'id': id, 'basis': 'article'}\n", encoding="utf-8")
        (self.folder / "public_reference.py").write_text(
            "def reference_search(query, language='zh', limit=5): return {'items': []}\n"
            "def reference_read(page_id, language='zh', offset=0): return {'title':'词条','offset':offset}\n",
            encoding="utf-8")
        (self.folder / "public_web_search.py").write_text(
            "def web_search(query, limit=5): return {'items': [], 'topic': query}\n"
            "def web_read(id): return {'basis': 'web_excerpt', 'id': id}\n", encoding="utf-8")
        (self.folder / "mastodon_api.py").write_text(
            "def mastodon_identity(): return {'acct': 'offline'}\n"
            "def mastodon_search(query, limit=5): return {'items': []}\n"
            "def mastodon_read(id):\n"
            "    if id == '403': raise ValueError('mastodon_forbidden')\n"
            "    if id == '502': raise ValueError('private server response, auth=fixture')\n"
            "    return {'id': id}\n"
            "def mastodon_mentions(limit=5): return {'items': []}\n"
            "def mastodon_reply(id, text, idempotency_key):\n"
            "    return {'id': '998', 'in_reply_to_id': id, 'visibility':'public'}\n", encoding="utf-8")

    def client(self, provider, **env):
        import mcp
        inherited = {"PYTHONUTF8": "1", "PYTHONPATH": str(Path(mcp.__file__).resolve().parents[1])}
        client = StdioMCPClient(sys.executable, [str(self.folder / SERVER.name), provider],
                                str(SERVER.parent), {**inherited, **env}, timeout_seconds=20)
        self.addAsyncCleanup(client.close)
        return client

    async def test_separate_catalogs_valid_calls_and_rejected_arguments(self):
        news = self.client("news")
        catalog = await news.list_tools()
        self.assertEqual(set(catalog), {"news_search", "news_read"})
        self.assertEqual(catalog["news_search"]["input_schema"]["properties"]["limit"]["maximum"], 10)
        self.assertEqual(await news.call_tool("news_search", {"query": "天气"}), {"items": [], "topic": "天气"})
        for name, arguments in [("reference_read", {"page_id": 123}), ("news_read", {"id": "https://invalid.test"}),
                                ("news_search", {"query": "test", "token": "x"})]:
            with self.assertRaises(MCPError) as e:
                await news.call_tool(name, arguments)
            self.assertEqual(e.exception.code, "tool_error")
        reference = self.client("reference")
        self.assertEqual(set(await reference.list_tools()), {"reference_search", "reference_read"})
        self.assertEqual(await reference.call_tool("reference_read", {"page_id": 123, "offset": 200}),
                         {"title":"词条", "offset":200})

    async def test_web_key_enables_only_search_and_read_by_result_id(self):
        news = self.client("news", MLW_BRAVE_SEARCH_KEY="offline-fixture-key")
        self.assertEqual(set(await news.list_tools()), {"news_search", "news_read", "web_search", "web_read"})
        with self.assertRaisesRegex(MCPError, "tool_error"):
            await news.call_tool("web_read", {"id": "https://example.test/a"})
        ident = "web_" + "a" * 32
        self.assertEqual(await news.call_tool("web_read", {"id": ident}), {"id": ident, "basis": "web_excerpt"})

    async def test_social_catalog_and_offline_reply_receipt(self):
        client = self.client("mastodon")
        tools = await client.list_tools()
        self.assertEqual(set(tools), {"mastodon_identity", "mastodon_search", "mastodon_read",
                                      "mastodon_mentions", "mastodon_reply"})
        self.assertEqual(await client.call_tool("mastodon_identity", {}), {"acct": "offline"})
        args = {"id": "731", "text": "当地的观察很有意思", "idempotency_key": "approved-test-operation"}
        receipt = await client.call_tool("mastodon_reply", args)
        self.assertEqual(receipt["in_reply_to_id"], args["id"])
        for target, code in [("403", "mastodon_forbidden"), ("502", "tool_error")]:
            with self.assertRaises(MCPError) as caught:
                await client.call_tool("mastodon_read", {"id": target})
            self.assertEqual(caught.exception.code, code)
        for invalid in ({**args, "id": "https://example.test/a"}, {**args, "instance": "https://example.test"},
                        {**args, "idempotency_key": "x"}):
            with self.assertRaisesRegex(MCPError, "tool_error"):
                await client.call_tool("mastodon_reply", invalid)
