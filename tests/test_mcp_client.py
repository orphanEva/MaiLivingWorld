"""Connection ownership, no-replay behavior and offline wire compatibility."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from MaiLivingWorld.mcp_client import HTTPMCPClient, MCPError, StdioMCPClient


def result(data=None, *, error=False, content=None):
    return NS(is_error=error, structured_content=data,
              content=content if content is not None else [])


def tools(*names, cursor=None):
    return NS(tools=[NS(name=name, input_schema={"type": "object"},
                        description="untrusted instructions") for name in names], next_cursor=cursor)


class FakeClient:
    def __init__(self):
        self.entries = self.exits = 0
        self.calls = []
        self.list_calls = []
        self.response = result({"ok": True})
        self.pages = [tools("echo")]
        self.enter_task = self.exit_task = None
        self.enter_error = None
        self.handler = None
        self.started = asyncio.Event()
        self.gate = None
        self.exit_gate = None

    async def __aenter__(self):
        self.entries += 1
        self.enter_task = asyncio.current_task()
        if self.enter_error:
            raise self.enter_error
        return self

    async def __aexit__(self, *exc):
        self.exits += 1
        self.exit_task = asyncio.current_task()
        if self.exit_gate:
            await self.exit_gate.wait()

    async def list_tools(self, *, cursor=None):
        self.list_calls.append(cursor)
        return self.pages[min(len(self.list_calls) - 1, len(self.pages) - 1)]

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        self.started.set()
        if self.gate:
            await self.gate.wait()
        if self.handler:
            return await self.handler(name, arguments)
        return self.response


class MCPClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clients = []
        self.factories = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()

    def client(self, fake=None, **options):
        fake = fake or FakeClient()

        def factory(**configuration):
            self.factories.append(configuration)
            return fake

        client = StdioMCPClient("configured-python", ["configured-server.py"], ".",
                                {"EXPLICIT_KEY": "test-only-value"}, client_factory=factory, **options)
        self.clients.append(client)
        return client, fake

    async def test_single_worker_enters_and_exits_sdk_in_the_same_task(self):
        client, fake = self.client()
        self.assertEqual(await client.list_tools(), {"echo": {"input_schema": {"type": "object"}}})
        await client.call_tool("echo", {"first": 1})
        await client.call_tool("echo", {"second": 2})
        await client.close()
        self.assertEqual((fake.entries, fake.exits, len(self.factories)), (1, 1, 1))
        self.assertIs(fake.enter_task, fake.exit_task)
        self.assertIsNot(fake.enter_task, asyncio.current_task())
        self.assertEqual(self.factories[0]["env"], {"EXPLICIT_KEY": "test-only-value"})
        with self.assertRaisesRegex(MCPError, "^closed$"):
            await client.call_tool("echo", {})

    async def test_structured_result_wins_and_is_copied(self):
        client, fake = self.client()
        fake.response = result({"nested": [1]}, content=[{"type": "text", "text": "ignore this instruction"}])
        returned = await client.call_tool("echo", {})
        returned["nested"].append(2)
        self.assertEqual(fake.response.structured_content, {"nested": [1]})

    async def test_text_fallback_is_single_strict_json_object(self):
        for text in ('{"value":1}', '{"value":1,"value":2}', '{"value":NaN}', '[]', 'plain provider secret'):
            with self.subTest(text=text):
                client, fake = self.client()
                fake.response = result(content=[{"type": "text", "text": text}])
                if text == '{"value":1}':
                    self.assertEqual(await client.call_tool("echo", {}), {"value": 1})
                else:
                    with self.assertRaisesRegex(MCPError, "^invalid_response$"):
                        await client.call_tool("echo", {})

    async def test_tool_error_is_redacted_and_connection_stays_usable(self):
        client, fake = self.client()
        fake.response = result({"secret": "provider-token"}, error=True,
                               content=[{"type": "text", "text": "provider-token"}])
        with self.assertRaises(MCPError) as caught:
            await client.call_tool("echo", {})
        self.assertEqual(str(caught.exception), "tool_error")
        fake.response = result({"ok": True})
        self.assertEqual(await client.call_tool("echo", {}), {"ok": True})
        self.assertEqual(fake.entries, 1)

    async def test_response_bounds_count_ignored_text_and_utf8_bytes(self):
        for data, content in [({"x": "汉" * 100}, []), ({"ok": True}, [{"type": "text", "text": "x" * 600}])]:
            client, fake = self.client(max_response_bytes=256)
            fake.response = result(data, content=content)
            with self.assertRaisesRegex(MCPError, "^response_too_large$"):
                await client.call_tool("echo", {})

    async def test_invalid_types_do_not_cross_boundary(self):
        for response in (NS(is_error=1, content=[], structured_content={}),
                         result([]), result({"x": float("inf")}),
                         result(content=[{"type": "image", "data": "abc"}]),
                         result(content=[{"type": "text", "text": "{}"}] * 2)):
            client, fake = self.client()
            fake.response = response
            with self.assertRaisesRegex(MCPError, "^invalid_response$"):
                await client.call_tool("echo", {})

    async def test_list_pages_are_bounded_and_metadata_is_not_exposed(self):
        client, fake = self.client()
        fake.pages = [tools("first", cursor="next"), tools("second")]
        self.assertEqual(set(await client.list_tools()), {"first", "second"})
        self.assertEqual(fake.list_calls, [None, "next"])
        for pages in ([tools("first", cursor="loop"), tools("second", cursor="loop")],
                      [tools("duplicate", "duplicate")], [tools(*["x" + str(i) for i in range(129)])]):
            client, fake = self.client()
            fake.pages = pages
            with self.assertRaisesRegex(MCPError, "^invalid_response$"):
                await client.list_tools()

    async def test_disconnect_does_not_replay_or_reconnect(self):
        client, fake = self.client()

        async def broken(*_):
            raise RuntimeError("provider-secret environment-token")

        fake.handler = broken
        with self.assertRaisesRegex(MCPError, "^disconnected$"):
            await client.call_tool("echo", {})
        with self.assertRaises(MCPError):
            await client.call_tool("echo", {})
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(len(self.factories), 1)

    async def test_timeout_does_not_replay_and_retires_connection(self):
        client, fake = self.client(timeout_seconds=0.04)
        fake.gate = asyncio.Event()
        with self.assertRaisesRegex(MCPError, "^timeout$"):
            await client.call_tool("echo", {})
        await client.close()
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.exits, 1)

    async def test_cancel_queued_caller_preserves_active_operation(self):
        client, fake = self.client()
        fake.gate = asyncio.Event()
        first = asyncio.create_task(client.call_tool("echo", {"id": 1}))
        await fake.started.wait()
        second = asyncio.create_task(client.call_tool("echo", {"id": 2}))
        await asyncio.sleep(0)
        second.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await second
        fake.gate.set()
        self.assertEqual(await first, {"ok": True})
        await client.call_tool("echo", {"id": 3})
        self.assertEqual([args["id"] for _, args in fake.calls], [1, 3])
        self.assertEqual(fake.exits, 0)

    async def test_cancel_active_discards_outcome_and_never_sends_queued_call(self):
        client, fake = self.client()
        fake.gate = asyncio.Event()
        first = asyncio.create_task(client.call_tool("echo", {"id": 1}))
        await fake.started.wait()
        second = asyncio.create_task(client.call_tool("echo", {"id": 2}))
        await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        fake.gate.set()
        with self.assertRaisesRegex(MCPError, "^disconnected$"):
            await second
        self.assertEqual(len(fake.calls), 1)

    async def test_queue_is_bounded_without_disrupting_running_call(self):
        client, fake = self.client()
        fake.gate = asyncio.Event()
        first = asyncio.create_task(client.call_tool("echo", {}))
        await fake.started.wait()
        waiting = [asyncio.create_task(client.call_tool("echo", {})) for _ in range(8)]
        await asyncio.sleep(0)
        with self.assertRaisesRegex(MCPError, "^queue_full$"):
            await client.call_tool("echo", {})
        fake.gate.set()
        await asyncio.gather(first, *waiting)
        self.assertEqual(len(fake.calls), 9)

    async def test_close_aborts_worker_in_its_scope_and_fails_pending_requests(self):
        client, fake = self.client()
        fake.gate = asyncio.Event()
        first = asyncio.create_task(client.call_tool("echo", {}))
        await fake.started.wait()
        second = asyncio.create_task(client.call_tool("echo", {}))
        await asyncio.sleep(0)
        await client.close()
        for pending in (first, second):
            with self.assertRaisesRegex(MCPError, "^closed$"):
                await pending
        self.assertIs(fake.enter_task, fake.exit_task)
        self.assertEqual(len(fake.calls), 1)

    async def test_close_timeout_does_not_native_cancel_sdk_cleanup(self):
        client, fake = self.client()
        # Fake a cancellation-shielded SDK cleanup step.
        import anyio
        release = asyncio.Event()
        original_exit = fake.__class__.__aexit__

        async def shielded_exit(instance, *exc):
            with anyio.CancelScope(shield=True):
                await release.wait()
                await original_exit(instance, *exc)

        with patch.object(FakeClient, "__aexit__", shielded_exit), patch("MaiLivingWorld.mcp_client._CLOSE_SECONDS", .02):
            await client.call_tool("echo", {})
            with self.assertRaisesRegex(MCPError, "^close_timeout$"):
                await client.close()
            self.assertFalse(client._worker.done())
            release.set()
            await client.close()
        self.assertEqual(fake.exits, 1)

    async def test_invalid_request_never_starts_process(self):
        client, fake = self.client()
        for name, args in (("bad name", {}), ("echo", []), ("echo", {"x": float("nan")})):
            with self.assertRaisesRegex(MCPError, "^invalid_request$"):
                await client.call_tool(name, args)
        self.assertEqual(fake.entries, 0)

    async def test_startup_errors_are_safe(self):
        client, fake = self.client()
        fake.enter_error = RuntimeError("secret executable / private key")
        with self.assertRaisesRegex(MCPError, "^disconnected$"):
            await client.list_tools()
        self.assertEqual(fake.entries, 1)


def has_sdk_v2():
    if importlib.util.find_spec("mcp") is None:
        return False
    import mcp
    return hasattr(mcp, "Client")


@unittest.skipUnless(has_sdk_v2(), "optional MCP v2 dependency is not installed")
class RealStdioTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.base = Path(self.directory.name)
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()
        self.directory.cleanup()

    def client(self, protocol="2025-11-25", timeout=5):
        client = StdioMCPClient(
            sys.executable, ["-u", str(Path(__file__).with_name("mcp_stdio_fixture.py"))],
            str(self.base), {"MCP_FIXTURE_PROTOCOL": protocol, "MCP_FIXTURE_EXPLICIT": "allowed",
                             "MCP_FIXTURE_CALL_LOG": str(self.base / "calls.txt"), "PYTHONUTF8": "1"},
            timeout_seconds=timeout)
        self.clients.append(client)
        return client

    async def test_real_legacy_generations_reuse_pid_and_advertise_no_callbacks(self):
        for protocol in ("2025-03-26", "2025-11-25"):
            with self.subTest(protocol=protocol), patch.dict(os.environ, {"MAILIVINGWORLD_HOST_SECRET": "must-not-inherit"}):
                client = self.client(protocol)
                self.assertIn("echo", await client.list_tools())
                first = await client.call_tool("echo", {})
                second = await client.call_tool("echo", {})
                self.assertEqual(first["pid"], second["pid"])
                self.assertEqual(second["calls"], 2)
                self.assertEqual(second["capabilities"], {})
                self.assertIsNone(second["host_secret"])
                self.assertEqual(second["explicit"], "allowed")
                await client.close()

    async def test_real_shutdown_requires_new_instance_and_new_process(self):
        old = self.client()
        first = await old.call_tool("echo", {})
        await old.close()
        with self.assertRaisesRegex(MCPError, "^closed$"):
            await old.list_tools()
        replacement = self.client()
        second = await replacement.call_tool("echo", {})
        self.assertNotEqual(first["pid"], second["pid"])
        self.assertEqual(second["calls"], 1)

    async def test_real_timeout_is_not_replayed(self):
        client = self.client(timeout=.5)
        await client.list_tools()
        with self.assertRaisesRegex(MCPError, "^timeout$"):
            await client.call_tool("hang", {})
        await client.close()
        self.assertEqual((self.base / "calls.txt").read_text().splitlines(), ["hang"])

    async def test_real_disconnect_is_not_replayed_and_tool_errors_are_redacted(self):
        client = self.client()
        with self.assertRaisesRegex(MCPError, "^tool_error$"):
            await client.call_tool("error", {})
        with self.assertRaisesRegex(MCPError, "^disconnected$"):
            await client.call_tool("disconnect", {})
        with self.assertRaises(MCPError):
            await client.call_tool("echo", {})
        self.assertEqual((self.base / "calls.txt").read_text().splitlines(), ["error", "disconnect"])


TOKEN = "offline-gateway-bearer-token-0123456789"


class HTTPConfigurationTests(unittest.TestCase):
    def test_fixed_internal_urls_are_allowed_and_credentials_in_urls_are_rejected(self):
        for url in ("http://cap-mcp:8000/mcp", "https://pixiv.internal/mcp", "http://[::1]:8000/mcp"):
            HTTPMCPClient(url, TOKEN)
        for url in ("file:///local", "http://user:password@server/mcp", "https://server/mcp?token=value",
                    "https://server/mcp#fragment", "https://server/mcp?", "https://server/mcp#",
                    "http://server:99999/mcp", "http://server:0/mcp", "http://server/\\other", "http://server/ white"):
            with self.subTest(url=url), self.assertRaisesRegex(MCPError, "^invalid_config$"):
                HTTPMCPClient(url, TOKEN)

    def test_bearer_must_be_bounded_printable_ascii_without_whitespace(self):
        for token in (None, "short", "a" * 32 + "\n", "a" * 32 + " ", "密" * 32, "a" * 8193, "a" * 32 + "\x7f"):
            with self.subTest(token_type=type(token)), self.assertRaisesRegex(MCPError, "^invalid_config$"):
                HTTPMCPClient("http://cap-mcp:8000/mcp", token)


class HTTPWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_uses_same_worker_context_ownership_and_configuration_contract(self):
        fake, captured = FakeClient(), []

        def factory(**kwargs):
            captured.append(kwargs)
            return fake

        client = HTTPMCPClient("http://cap-mcp:8000/mcp", TOKEN, client_factory=factory)
        try:
            self.assertIn("echo", await client.list_tools())
            self.assertEqual(await client.call_tool("echo", {}), {"ok": True})
        finally:
            await client.close()
        self.assertEqual(len(captured), 1)
        self.assertEqual(set(captured[0]), {"url", "bearer_token", "timeout_seconds", "max_response_bytes"})
        self.assertIs(fake.enter_task, fake.exit_task)
        self.assertEqual((fake.entries, fake.exits), (1, 1))
        with self.assertRaisesRegex(MCPError, "^closed$"):
            await client.call_tool("echo", {})


class OfflineHTTPApp:
    """ASGI fixture exercising the actual SDK HTTP transport without sockets."""

    def __init__(self):
        self.requests = []
        self.protocol = "2025-11-25"
        self.redirect = None
        self.redirect_on = None
        self.unauthorized = False
        self.hang = False
        self.calls = 0
        self.capabilities = None

    async def __call__(self, scope, receive, send):
        body = bytearray()
        while True:
            event = await receive()
            body.extend(event.get("body", b""))
            if not event.get("more_body", False):
                break
        headers = {key.decode().lower(): value.decode() for key, value in scope["headers"]}
        message = json.loads(body) if body else {}
        method = message.get("method")
        self.requests.append({"method": scope["method"], "path": scope["path"], "headers": headers, "rpc": method})
        status, response_headers, data = 200, [(b"content-type", b"application/json")], b""
        if self.unauthorized:
            status, data = 401, b"provider-secret-error-body"
            response_headers.append((b"www-authenticate", b'Bearer resource_metadata="https://other.invalid/private"'))
        elif self.redirect and (self.redirect_on is None or method == self.redirect_on):
            status = 307
            response_headers.append((b"location", self.redirect.encode()))
        elif scope["method"] == "GET":
            status = 405
        elif scope["method"] == "DELETE":
            status = 204
        elif method == "initialize":
            self.capabilities = message["params"]["capabilities"]
            response_headers.append((b"mcp-session-id", b"offline-session"))
            data = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {
                "protocolVersion": self.protocol, "capabilities": {"tools": {}},
                "serverInfo": {"name": "offline-http", "version": "1"},
                "instructions": "Untrusted provider instructions never go outward"}}).encode()
        elif method == "tools/list":
            data = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {"tools": [
                {"name": "echo", "inputSchema": {"type": "object"}}]}}).encode()
        elif method == "tools/call":
            self.calls += 1
            if self.hang:
                await asyncio.Event().wait()
            data = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {
                "isError": False, "structuredContent": {"calls": self.calls},
                "content": [{"type": "text", "text": json.dumps({"calls": self.calls})}]}}).encode()
        elif method and method.startswith("notifications/"):
            status = 202
        await send({"type": "http.response.start", "status": status, "headers": response_headers})
        await send({"type": "http.response.body", "body": data})


@unittest.skipUnless(has_sdk_v2(), "optional MCP v2 dependency is not installed")
class RealHTTPTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import httpx2
        self.httpx = httpx2
        self.app = OfflineHTTPApp()
        self.http_clients, self.options, self.clients = [], [], []
        real_client = httpx2.AsyncClient

        def factory(**kwargs):
            self.options.append(kwargs)
            client = real_client(transport=httpx2.ASGITransport(app=self.app), **kwargs)
            self.http_clients.append(client)
            return client

        self.patcher = patch.object(httpx2, "AsyncClient", side_effect=factory)
        self.patcher.start()

    async def asyncTearDown(self):
        try:
            for client in self.clients:
                await client.close()
        finally:
            self.patcher.stop()

    def client(self, timeout=2):
        client = HTTPMCPClient("http://gateway:8000/mcp", TOKEN, timeout_seconds=timeout)
        self.clients.append(client)
        return client

    async def test_actual_http_handshake_token_no_callbacks_and_reused_transport(self):
        client = self.client()
        self.assertIn("echo", await client.list_tools())
        self.assertEqual(await client.call_tool("echo", {}), {"calls": 1})
        self.assertEqual(await client.call_tool("echo", {}), {"calls": 2})
        await client.close()
        self.assertEqual(len(self.http_clients), 1)
        self.assertTrue(self.http_clients[0].is_closed)
        self.assertEqual(self.app.capabilities, {})
        self.assertTrue(self.app.requests)
        for request in self.app.requests:
            self.assertEqual(request["headers"].get("authorization"), "Bearer " + TOKEN)
            self.assertNotIn("cookie", request["headers"])
            self.assertEqual(request["path"], "/mcp")
        options = self.options[0]
        self.assertFalse(options["trust_env"])
        self.assertFalse(options["follow_redirects"])
        self.assertEqual(options["max_redirects"], 0)

    async def test_redirects_are_not_followed_even_on_the_same_origin(self):
        for location in ("http://other:8000/stolen?token=secret", "http://gateway:8000/other", "/mcp/"):
            with self.subTest(location=location):
                self.app = OfflineHTTPApp()
                self.app.redirect = location
                client = self.client()
                with self.assertRaises(MCPError) as caught:
                    await client.list_tools()
                await client.close()
                self.assertEqual(str(caught.exception), "disconnected")
                self.assertEqual(len(self.app.requests), 1)
                self.assertEqual(self.app.requests[0]["path"], "/mcp")

    async def test_redirected_tool_post_is_never_replayed(self):
        client = self.client()
        await client.list_tools()
        self.app.redirect = "/mcp/"
        self.app.redirect_on = "tools/call"
        with self.assertRaisesRegex(MCPError, "^disconnected$"):
            await client.call_tool("echo", {})
        await client.close()
        sent = [request for request in self.app.requests if request["rpc"] == "tools/call"]
        self.assertEqual(len(sent), 1)

    async def test_authentication_failure_is_safe_and_never_discovers_oauth_endpoint(self):
        self.app.unauthorized = True
        client = self.client()
        with self.assertNoLogs("httpx2", level="INFO"), self.assertRaisesRegex(MCPError, "^disconnected$"):
            await client.list_tools()
        self.assertEqual(len(self.app.requests), 1)

    async def test_http_timeout_does_not_replay_tools_call(self):
        client = self.client(timeout=.1)
        await client.list_tools()
        self.app.hang = True
        with self.assertRaisesRegex(MCPError, "^timeout$"):
            await client.call_tool("echo", {})
        await client.close()
        self.assertEqual(self.app.calls, 1)
        self.assertEqual(sum(request["rpc"] == "tools/call" for request in self.app.requests), 1)


if __name__ == "__main__":
    unittest.main()
