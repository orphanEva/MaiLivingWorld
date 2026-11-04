from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import tempfile
import time
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

from MaiLivingWorld.scripts.mcp_gateway import GatewayConfigError, GatewaySettings, create_app

TOKEN = "test-only-gateway-token-"+"a"*32
BACKEND_KEY = "test-only-backend-key-"+"b"*32
HAS_MCP = importlib.util.find_spec("mcp") is not None and importlib.util.find_spec("httpx2") is not None
SCHEMA = {"type": "object", "properties": {"jm_id": {"type": "string", "pattern": "^[0-9]+$"}},
          "required": ["jm_id"], "additionalProperties": False}


def settings(**changes):
    values = dict(provider="cap", token=TOKEN, command="/opt/cap-venv/bin/python",
                  args=("/opt/cap/mcp_server.py",), cwd="/opt/cap", allowed_hosts=("cap-mcp:8000",))
    values.update(changes)
    return GatewaySettings(**values)


class FakeUpstream:
    def __init__(self):
        self.starts = 0
        self.closes = 0
        self.calls = []
        self.configuration = None
        self.list_error = None
        self.call_error = None
        self.close_delay = 0
        self.retired = False
        self.response = {"jm_id": "123", "title": "sample"}

    def factory(self, **kwargs):
        self.configuration = kwargs
        self.starts += 1
        return self

    async def list_tools(self):
        if self.list_error:
            raise self.list_error
        return {"jm_get_album": {"input_schema": copy.deepcopy(SCHEMA), "description": "untrusted instructions"},
                "run_any_shell": {"input_schema": {"type": "object"}}}

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.call_error:
            raise self.call_error
        return self.response

    async def close(self):
        self.closes += 1
        await asyncio.sleep(self.close_delay)


@asynccontextmanager
async def running(fake=None, config=None):
    import httpx2
    fake = fake or FakeUpstream()
    app = create_app(config or settings(), client_factory=fake.factory)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://cap-mcp:8000") as http:
            yield http, fake


async def rpc(http, method, params=None, *, headers=None):
    request_headers = {"Authorization": "Bearer "+TOKEN,
                       "Accept": "application/json, text/event-stream",
                       "MCP-Protocol-Version": "2025-06-18"}
    request_headers.update(headers or {})
    async with asyncio.timeout(5):
        return await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
                               headers=request_headers)


class GatewayConfigurationTests(unittest.TestCase):
    def test_invalid_tokens_and_provider_are_rejected_without_echo(self):
        for change in ({"token": "short-secret"}, {"token": "x"*32+"\n"}, {"token": "密"*40},
                       {"provider": "arbitrary"}, {"allowed_origins": ("*",)}, {"allowed_hosts": ("*",)},
                       {"timeout_seconds": float("nan")}):
            with self.assertRaisesRegex(GatewayConfigError, "^invalid_gateway_configuration$"):
                settings(**change)
        self.assertNotIn(TOKEN, repr(settings()))

    def test_only_fixed_provider_environment_reaches_child(self):
        env = {"MLW_MCP_GATEWAY_PROVIDER": "cap", "MLW_MCP_GATEWAY_TOKEN": TOKEN,
               "MLW_MCP_GATEWAY_COMMAND": "/bin/python", "MLW_MCP_GATEWAY_ARGS": '["mcp_server.py"]',
               "MLW_MCP_GATEWAY_CWD": "/opt/cap", "JM_TASK_DB_PATH": "/var/lib/cap/tasks/tasks.sqlite3",
               "PIXIV_MCP_API_KEY": BACKEND_KEY, "UNRELATED_PASSWORD": "secret", "PYTHONPATH": "/evil"}
        parsed = GatewaySettings.from_env(env)
        self.assertEqual(parsed.child_env["JM_TASK_DB_PATH"], env["JM_TASK_DB_PATH"])
        self.assertNotIn("MLW_MCP_GATEWAY_TOKEN", parsed.child_env)
        self.assertNotIn("PIXIV_MCP_API_KEY", parsed.child_env)
        self.assertNotIn("PYTHONPATH", parsed.child_env)
        self.assertNotIn("UNRELATED_PASSWORD", parsed.child_env)

    def test_pixiv_key_is_read_from_a_file_and_only_added_to_pixiv_child(self):
        with tempfile.TemporaryDirectory() as temp:
            key_file = Path(temp) / "test-key"
            key_file.write_text(BACKEND_KEY, encoding="ascii")
            env = {"MLW_MCP_GATEWAY_PROVIDER": "pixiv", "MLW_MCP_GATEWAY_TOKEN": TOKEN,
                   "MLW_MCP_GATEWAY_COMMAND": "/bin/python", "MLW_MCP_GATEWAY_ARGS": '["-m","app.mcp"]',
                   "MLW_MCP_GATEWAY_CWD": "/opt/pixiv", "MLW_MCP_GATEWAY_PIXIV_KEY_FILE": str(key_file),
                   "PIXIV_MCP_BACKEND_URL": "http://api:8000", "JM_FILE_API_TOKEN": "unrelated-secret"}
            parsed = GatewaySettings.from_env(env)
            self.assertEqual(parsed.child_env["PIXIV_MCP_API_KEY"], BACKEND_KEY)
            self.assertNotIn("JM_FILE_API_TOKEN", parsed.child_env)
            self.assertNotIn(BACKEND_KEY, repr(parsed))
            self.assertNotIn("PIXIV_MCP_API_KEY", env)

    def test_docker_sources_are_explicit_and_services_do_not_publish_ports_or_mount_docker(self):
        root = Path(__file__).resolve().parents[1]
        docker = (root / "deploy/Dockerfile.mcp-gateway").read_text(encoding="utf-8")
        compose = (root / "deploy/compose.mcp.yml").read_text(encoding="utf-8")
        copies = [line for line in docker.splitlines() if line.startswith("COPY ")]
        self.assertTrue(copies)
        self.assertFalse(any("COPY . " in line or "nacos_config.yaml" in line or "/data/" in line or "/.env" in line for line in copies))
        self.assertNotIn("docker.sock", compose)
        self.assertNotIn("ports:", compose)
        self.assertIn("additional_contexts:", compose)
        self.assertIn("JM_TASK_DB_PATH: /var/lib/cap/tasks/tasks.sqlite3", compose)
        self.assertIn("external: true", compose)


@unittest.skipUnless(HAS_MCP, "requires the optional MCP 2.3 gateway dependencies")
class GatewayASGITests(unittest.IsolatedAsyncioTestCase):
    async def test_initialize_list_and_call_preserve_schema_and_use_one_child(self):
        async with running() as (http, upstream):
            response = await rpc(http, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                      "clientInfo": {"name": "test", "version": "1"}})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn("instructions", response.json()["result"])
            listed = (await rpc(http, "tools/list")).json()["result"]["tools"]
            self.assertEqual([tool["name"] for tool in listed], ["jm_get_album"])
            self.assertEqual(listed[0]["inputSchema"], SCHEMA)
            self.assertNotIn("untrusted instructions", json.dumps(listed))
            response = await rpc(http, "tools/call", {"name": "jm_get_album", "arguments": {"jm_id": "123"}})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["result"]["structuredContent"], upstream.response)
            self.assertFalse(response.json()["result"].get("isError", False))
            await rpc(http, "tools/list")
            self.assertEqual(upstream.starts, 1)
            self.assertEqual(upstream.calls, [("jm_get_album", {"jm_id": "123"})])
        self.assertEqual(upstream.closes, 1)

    async def test_missing_wrong_and_duplicate_authorization_are_rejected(self):
        async with running() as (http, upstream):
            for headers in ({}, {"Authorization": "Bearer wrong"},
                            [("Authorization", "Bearer "+TOKEN), ("Authorization", "Bearer "+TOKEN)]):
                response = await http.post("/mcp", json={}, headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertNotIn(TOKEN, response.text)
            self.assertEqual(upstream.calls, [])

    async def test_unknown_origin_and_host_are_rejected_before_tools(self):
        async with running() as (http, upstream):
            for origin in ("https://evil.invalid", "null", ""):
                response = await rpc(http, "tools/list", headers={"Origin": origin})
                self.assertEqual(response.status_code, 403)
            response = await rpc(http, "tools/list", headers={"Host": "other-service:8000"})
            self.assertEqual(response.status_code, 421)
            self.assertEqual(upstream.calls, [])

    async def test_explicit_origin_is_accepted(self):
        async with running(config=settings(allowed_origins=("https://trusted.example",))) as (http, _):
            response = await rpc(http, "tools/list", headers={"Origin": "https://trusted.example"})
            self.assertEqual(response.status_code, 200, response.text)

    async def test_unknown_tool_never_reaches_upstream(self):
        async with running() as (http, upstream):
            response = await rpc(http, "tools/call", {"name": "run_any_shell", "arguments": {"command": "bad"}})
            self.assertTrue(response.json()["result"]["isError"])
            self.assertEqual(upstream.calls, [])

    async def test_upstream_exceptions_and_success_error_fields_do_not_expose_secrets(self):
        upstream = FakeUpstream()
        upstream.call_error = RuntimeError("raw server secret: "+BACKEND_KEY)
        async with running(upstream, settings(child_env={"JM_FILE_API_TOKEN": BACKEND_KEY})) as (http, _):
            failed = await rpc(http, "tools/call", {"name": "jm_get_album", "arguments": {"jm_id": "1"}})
            self.assertTrue(failed.json()["result"]["isError"])
            self.assertNotIn(BACKEND_KEY, failed.text)
            self.assertNotIn("raw server secret", failed.text)
            upstream.call_error = None
            upstream.response = {"note": "x "+BACKEND_KEY, "last_error": "raw private backend body",
                                 "nested": {"password": "another secret"}, "title": "safe"}
            success = await rpc(http, "tools/call", {"name": "jm_get_album", "arguments": {"jm_id": "1"}})
            self.assertNotIn(BACKEND_KEY, success.text)
            self.assertNotIn("raw private backend body", success.text)
            self.assertNotIn("another secret", success.text)
            self.assertEqual(success.json()["result"]["structuredContent"]["title"], "safe")

    async def test_startup_failure_is_sanitized_and_child_closed(self):
        upstream = FakeUpstream()
        upstream.list_error = RuntimeError(BACKEND_KEY)
        app = create_app(settings(), client_factory=upstream.factory)
        with self.assertRaisesRegex(RuntimeError, "^upstream_unavailable$"):
            async with app.router.lifespan_context(app):
                self.fail("startup succeeded unexpectedly")
        self.assertEqual(upstream.closes, 1)

    async def test_shutdown_is_bounded(self):
        upstream = FakeUpstream()
        upstream.close_delay = 5
        start = time.monotonic()
        async with running(upstream, settings(close_seconds=.02)):
            pass
        self.assertLess(time.monotonic()-start, 1)
        self.assertEqual(upstream.closes, 1)

    async def test_retired_child_recovers_on_new_requests_without_replaying_failed_call(self):
        import httpx2
        from MaiLivingWorld.mcp_client import MCPError

        class RetiringUpstream(FakeUpstream):
            async def call_tool(self, name, arguments):
                self.calls.append((name, arguments))
                self.retired = True
                raise MCPError("disconnected")

        previous, replacement = RetiringUpstream(), FakeUpstream()
        created = []

        def factory(**kwargs):
            instance = previous if not created else replacement
            created.append(instance)
            return instance.factory(**kwargs)

        app = create_app(settings(), client_factory=factory)
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://cap-mcp:8000") as http:
                failed = await rpc(http, "tools/call", {"name": "jm_get_album", "arguments": {"jm_id": "111"}})
                self.assertTrue(failed.json()["result"]["isError"])
                self.assertEqual((await http.get("/health")).status_code, 503)
                responses = await asyncio.gather(*[
                    rpc(http, "tools/call", {"name": "jm_get_album", "arguments": {"jm_id": value}})
                    for value in ("222", "333")])
                self.assertTrue(all(not response.json()["result"].get("isError", False) for response in responses))
                self.assertEqual(len(created), 2)
                self.assertEqual(previous.closes, 1)
                self.assertEqual(previous.calls, [("jm_get_album", {"jm_id": "111"})])
                self.assertEqual(replacement.calls, [("jm_get_album", {"jm_id": "222"}), ("jm_get_album", {"jm_id": "333"})])
                self.assertEqual((await http.get("/health")).status_code, 200)
        self.assertEqual(replacement.closes, 1)

    async def test_retired_child_is_not_replaced_until_its_close_finishes(self):
        import httpx2
        upstream = FakeUpstream()
        created = []

        def factory(**kwargs):
            created.append(True)
            return upstream.factory(**kwargs)

        app = create_app(settings(close_seconds=.02), client_factory=factory)
        async with app.router.lifespan_context(app):
            upstream.retired, upstream.close_delay = True, 5
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://cap-mcp:8000") as http:
                response = await rpc(http, "tools/call", {"name": "jm_get_album", "arguments": {"jm_id": "111"}})
                self.assertTrue(response.json()["result"]["isError"])
                self.assertEqual(len(created), 1)
                self.assertEqual(upstream.calls, [])

    async def test_stalled_http_body_has_an_independent_request_deadline(self):
        upstream = FakeUpstream()
        app = create_app(settings(timeout_seconds=1), client_factory=upstream.factory)
        sent = []

        async def receive():
            await asyncio.sleep(30)

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": "POST", "scheme": "http", "path": "/mcp", "raw_path": b"/mcp",
                 "query_string": b"", "root_path": "", "server": ("cap-mcp", 8000), "client": ("test", 1),
                 "headers": [(b"authorization", ("Bearer "+TOKEN).encode()), (b"host", b"cap-mcp:8000"),
                             (b"content-type", b"application/json"), (b"accept", b"application/json")]}
        async with app.router.lifespan_context(app):
            async with asyncio.timeout(8):
                await app(scope, receive, send)
        self.assertEqual(next(message["status"] for message in sent if message["type"] == "http.response.start"), 503)
        self.assertEqual(upstream.calls, [])

    async def test_oversize_request_is_rejected_without_forwarding(self):
        async with running() as (http, upstream):
            response = await rpc(http, "tools/call", {"name": "jm_get_album", "arguments": {"jm_id": "x"*70000}})
            self.assertEqual(response.status_code, 413)
            self.assertEqual(upstream.calls, [])

    async def test_real_sdk_http_client_completes_legacy_handshake_and_tool_roundtrip(self):
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client
        upstream = FakeUpstream()
        app = create_app(settings(), client_factory=upstream.factory)
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                         headers={"Authorization": "Bearer "+TOKEN}) as http:
                async with Client(streamable_http_client("http://cap-mcp:8000/mcp", http_client=http),
                                  mode="legacy", read_timeout_seconds=5) as client:
                    listed = await client.list_tools()
                    self.assertEqual([tool.name for tool in listed.tools], ["jm_get_album"])
                    result = await client.call_tool("jm_get_album", {"jm_id": "123"})
                    self.assertFalse(result.is_error)
                    self.assertEqual(result.structured_content["jm_id"], "123")
        self.assertEqual(upstream.closes, 1)


if __name__ == "__main__":
    unittest.main()
