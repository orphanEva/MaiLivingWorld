"""Lazy MCP stdio/HTTP boundaries for administrator-configured servers.

One worker owns the SDK context for its entire lifetime. It never retries a tool
request: after a transport failure the caller must construct a new client and
decide what to do with the operation whose outcome may be unknown.

``max_response_bytes`` bounds parsed results. The MCP 1.x HTTP client also
limits the whole uncompressed response with an extra 8 KiB for its envelope.
MCP 2.x limits HTTP SSE events. Its JSON transport and both stdio transports
buffer messages before parsing, so endpoints and executables remain trusted.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import math
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit


_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,127}\Z")
_QUEUE_LIMIT = 8
_MAX_TOOLS = 128
_MAX_PAGES = 4
_SCHEMA_BYTES = 32768
_CLOSE_SECONDS = 10.0
_QUIET = contextvars.ContextVar("mailivingworld_mcp_quiet", default=False)
_CODES = frozenset({
    "invalid_config", "invalid_request", "sdk_unavailable", "queue_full",
    "closed", "timeout", "disconnected", "tool_error", "invalid_response",
    "response_too_large", "close_timeout", "client_error",
})


class MCPError(RuntimeError):
    """Only a fixed code crosses the boundary, never server/configuration text."""

    def __init__(self, code: str):
        self.code = code if code in _CODES else "client_error"
        super().__init__(self.code)


class _QuietSDK(logging.Filter):
    def filter(self, record):
        return not _QUIET.get()


_QUIET_FILTER = _QuietSDK()


def _quiet_sdk_logs():
    # SDK parse exceptions can contain raw provider payloads. The filter only
    # suppresses this worker's context and inherited SDK tasks; other MCP users
    # in the same host keep their normal logging behavior.
    for name, logger in tuple(logging.Logger.manager.loggerDict.items()):
        if (any(name == prefix or name.startswith(prefix + ".")
                for prefix in ("mcp", "mcp_types", "httpx", "httpcore", "httpx2", "httpcore2"))
                and isinstance(logger, logging.Logger)):
            if _QUIET_FILTER not in logger.filters:
                logger.addFilter(_QUIET_FILTER)


@asynccontextmanager
async def _session(sdk, transport, timeout_seconds):
    if hasattr(sdk, "Client"):
        async with sdk.Client(transport, mode="legacy", read_timeout_seconds=timeout_seconds,
                              input_required_max_rounds=0, cache=None) as client:
            yield client
    else:
        async with transport as streams:
            async with sdk.ClientSession(streams[0], streams[1],
                                         read_timeout_seconds=timedelta(seconds=timeout_seconds)) as client:
                await client.initialize()
                yield client


@asynccontextmanager
async def _sdk_context(*, command, args, cwd, env, timeout_seconds):
    try:
        import mcp
        from mcp.client.stdio import StdioServerParameters, stdio_client
    except (ImportError, AttributeError):
        raise MCPError("sdk_unavailable") from None
    _quiet_sdk_logs()
    parameters = StdioServerParameters(command=command, args=args, cwd=cwd, env=env)
    # Both official transports use explicit argv, a minimal inherited environment
    # and child-process cleanup. No sampling, elicitation or roots are registered.
    with open(os.devnull, "w", encoding="utf-8") as errors:
        async with _session(mcp, stdio_client(parameters, errlog=errors), timeout_seconds) as client:
            yield client


@asynccontextmanager
async def _http_sdk_context(*, url, bearer_token, timeout_seconds, max_response_bytes):
    try:
        import mcp
        from mcp.client.streamable_http import streamable_http_client
        if hasattr(mcp, "Client"):
            import httpx2 as http
        else:
            import httpx as http
    except (ImportError, AttributeError):
        raise MCPError("sdk_unavailable") from None
    endpoint = http.URL(url)

    async def fixed_endpoint(request):
        # No redirect/OAuth destination, server-supplied URL or proxy inherits
        # this administrator token. A new endpoint requires new configuration.
        if request.url != endpoint:
            raise MCPError("disconnected")
        request.headers["Authorization"] = "Bearer " + bearer_token
        request.headers.pop("Cookie", None)

    hooks = {"request": [fixed_endpoint]}
    headers = {"Authorization": "Bearer " + bearer_token}
    transport_options = {}
    if hasattr(mcp, "Client"):
        transport_options["max_sse_event_size"] = max_response_bytes
    else:
        # v1 buffers JSON responses and does not bound SSE messages. Bound the
        # complete response before the SDK reads it, even for chunked transfers.
        # Our gateways use one JSON response per request.
        headers["Accept-Encoding"] = "identity"

        class LimitedStream(http.AsyncByteStream):
            def __init__(self, upstream):
                self.upstream = upstream

            async def __aiter__(self):
                size = 0
                async for chunk in self.upstream:
                    size += len(chunk)
                    if size > max_response_bytes + 8192:  # includes the JSON-RPC envelope
                        raise MCPError("response_too_large")
                    yield chunk

            async def aclose(self):
                await self.upstream.aclose()

        async def bound_response(response):
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                await response.aclose()
                raise MCPError("invalid_response")
            response.stream = LimitedStream(response.stream)

        hooks["response"] = [bound_response]

    # The v2 SDK follows same-origin redirects itself, independently of
    # follow_redirects. Its loop consults max_redirects, so zero disables ALL
    # redirects, including a trailing-slash normalization of the endpoint.
    async with http.AsyncClient(
        headers=headers, timeout=http.Timeout(timeout_seconds), follow_redirects=False,
        max_redirects=0, trust_env=False, event_hooks=hooks,
    ) as http_client:
        _quiet_sdk_logs()
        transport = streamable_http_client(url, http_client=http_client, **transport_options)
        async with _session(mcp, transport, timeout_seconds) as client:
            yield client


def _json_copy(value, limit: int, code="invalid_response"):
    """Check JSON shape/depth and finite numbers before making a bounded copy."""
    nodes = 0

    def check(item, depth=0):
        nonlocal nodes
        nodes += 1
        if depth > 24 or nodes > 8192:
            raise MCPError(code)
        if item is None or type(item) is bool:
            return
        if type(item) is str:
            if len(item) > limit:
                raise MCPError("response_too_large" if code == "invalid_response" else code)
            return
        if type(item) is int:
            if item.bit_length() > 1024:
                raise MCPError(code)
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise MCPError(code)
            return
        if type(item) is list:
            for child in item:
                check(child, depth + 1)
            return
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise MCPError(code)
                check(key, depth + 1)
                check(child, depth + 1)
            return
        raise MCPError(code)

    check(value)
    try:
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(payload.encode("utf-8")) > limit:
            raise MCPError("response_too_large" if code == "invalid_response" else code)
        return json.loads(payload)
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError):
        raise MCPError(code) from None


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result


def _invalid_constant(_):
    raise ValueError("nonfinite")


def _value(obj, name, default=None):
    if type(obj) is dict or hasattr(obj, name):
        return obj.get(name, default) if type(obj) is dict else getattr(obj, name)
    # v1 exposes wire field names directly; v2 exposes Python-style names.
    wire_name = {"is_error": "isError", "structured_content": "structuredContent",
                 "input_schema": "inputSchema", "next_cursor": "nextCursor"}.get(name, name)
    return getattr(obj, wire_name, default)


def _result(result, limit):
    error = _value(result, "is_error")
    if type(error) is not bool:
        raise MCPError("invalid_response")
    if error:
        raise MCPError("tool_error")
    content = _value(result, "content")
    if type(content) is not list or len(content) > 32:
        raise MCPError("invalid_response")
    blocks = []
    for block in content:
        if type(block) is dict:
            plain = block
        elif callable(getattr(block, "model_dump", None)):
            plain = block.model_dump(mode="json", by_alias=True)
        else:
            plain = {"type": _value(block, "type"), "text": _value(block, "text")}
        blocks.append(plain)
    structured = _value(result, "structured_content")
    # Count content even when structured output wins; never pass images/resources
    # or duplicate textual output back to the caller.
    _json_copy({"content": blocks, "structured_content": structured}, limit)
    if structured is not None:
        if type(structured) is not dict:
            raise MCPError("invalid_response")
        return _json_copy(structured, limit)
    if len(blocks) != 1 or blocks[0].get("type") != "text" or type(blocks[0].get("text")) is not str:
        raise MCPError("invalid_response")
    try:
        parsed = json.loads(blocks[0]["text"], object_pairs_hook=_strict_object,
                            parse_constant=_invalid_constant)
    except (ValueError, TypeError, RecursionError):
        raise MCPError("invalid_response") from None
    if type(parsed) is not dict:
        raise MCPError("invalid_response")
    return _json_copy(parsed, limit)


@dataclass
class _Request:
    operation: str
    name: str | None
    arguments: dict | None
    result: asyncio.Future
    deadline: float


class StdioMCPClient:
    """Serialized stdio connection; close/failure permanently retires this object.

    A test ``client_factory`` is called with keyword arguments ``command``,
    ``args``, ``cwd``, ``env``, ``timeout_seconds`` and returns an async context
    manager exposing the SDK ``list_tools`` and ``call_tool`` methods.
    Cancelling a queued caller never cancels somebody else's active operation.
    Cancelling an active caller discards its outcome, then retires the session
    after that operation settles or reaches its deadline; it is never replayed.
    """

    def __init__(self, command: str, args: list[str], cwd: str, env: dict[str, str],
                 timeout_seconds=30, max_response_bytes=262144, client_factory=None):
        def valid_string(value, maximum):
            return type(value) is str and len(value) <= maximum and "\0" not in value

        if (not valid_string(command, 4096) or not command.strip()
                or not valid_string(cwd, 4096) or not cwd.strip()
                or type(args) is not list or len(args) > 64
                or any(not valid_string(arg, 8192) for arg in args)
                or type(env) is not dict or len(env) > 128
                or any(not valid_string(k, 256) or not k or "=" in k
                       or not valid_string(v, 32768) for k, v in env.items())):
            raise MCPError("invalid_config")
        self._initialize(dict(command=command, args=list(args), cwd=cwd, env=dict(env),
                              timeout_seconds=timeout_seconds),
                         timeout_seconds, max_response_bytes, client_factory or _sdk_context)

    def _initialize(self, configuration, timeout_seconds, max_response_bytes, client_factory):
        if (type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 300
                or type(max_response_bytes) is not int or not 256 <= max_response_bytes <= 4 * 1024 * 1024):
            raise MCPError("invalid_config")
        self._configuration = configuration
        self._configuration["timeout_seconds"] = float(timeout_seconds)
        self.timeout_seconds = float(timeout_seconds)
        self.max_response_bytes = max_response_bytes
        self._factory = client_factory
        self._queue = asyncio.Queue(maxsize=_QUEUE_LIMIT)
        self._worker = None
        self._scope = None
        self._active = None
        self._closed = False
        self._failure = "closed"

    async def list_tools(self) -> dict[str, dict[str, Any]]:
        return await self._request("list", None, None)

    @property
    def retired(self) -> bool:
        """A later independent request may create a new client; never replay this one."""
        return self._closed

    async def call_tool(self, name, arguments) -> dict:
        if type(name) is not str or not _NAME.fullmatch(name) or type(arguments) is not dict:
            raise MCPError("invalid_request")
        arguments = _json_copy(arguments, min(self.max_response_bytes, 65536), "invalid_request")
        return await self._request("call", name, arguments)

    async def _request(self, operation, name, arguments):
        if self._closed:
            raise MCPError(self._failure)
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        request = _Request(operation, name, arguments, future, loop.time() + self.timeout_seconds)
        try:
            self._queue.put_nowait(request)
        except asyncio.QueueFull:
            raise MCPError("queue_full") from None
        if self._worker is None:
            self._worker = asyncio.create_task(self._run(), name="mailivingworld-mcp")
        try:
            # shield keeps caller cancellation from accidentally cancelling a
            # worker's future belonging to another queued/active request.
            async with asyncio.timeout(self.timeout_seconds):
                return await asyncio.shield(future)
        except TimeoutError:
            future.cancel()
            raise MCPError("timeout") from None
        except asyncio.CancelledError:
            future.cancel()
            raise

    async def _tools(self, client):
        tools, cursors = {}, set()
        cursor = None
        for _ in range(_MAX_PAGES):
            page = await client.list_tools(cursor=cursor)
            entries = _value(page, "tools")
            if type(entries) is not list or len(entries) + len(tools) > _MAX_TOOLS:
                raise MCPError("invalid_response")
            for entry in entries:
                name, schema = _value(entry, "name"), _value(entry, "input_schema")
                if (type(name) is not str or not _NAME.fullmatch(name) or name in tools
                        or type(schema) is not dict):
                    raise MCPError("invalid_response")
                tools[name] = {"input_schema": _json_copy(schema, min(_SCHEMA_BYTES, self.max_response_bytes))}
            _json_copy(tools, self.max_response_bytes)
            cursor = _value(page, "next_cursor")
            if cursor is None:
                return tools
            if type(cursor) is not str or not cursor or len(cursor) > 512 or cursor in cursors:
                raise MCPError("invalid_response")
            cursors.add(cursor)
        raise MCPError("invalid_response")

    @staticmethod
    def _fail(request, code):
        if request is not None and not request.result.done():
            request.result.set_exception(MCPError(code))

    async def _run(self):
        # anyio cancellation scopes are essential: native task.cancel() can
        # interrupt the SDK's shielded child cleanup. Enter/exit both SDK and
        # cancellation scopes in THIS worker, never in wait_for-created tasks.
        token = _QUIET.set(True)
        timer = None
        failure = "disconnected"
        try:
            try:
                import anyio
            except ImportError:
                raise MCPError("sdk_unavailable") from None
            with anyio.CancelScope() as scope:
                self._scope = scope
                if self._closed:
                    return

                def startup_timeout():
                    nonlocal failure
                    failure = "timeout"
                    scope.cancel()

                timer = asyncio.get_running_loop().call_later(self.timeout_seconds, startup_timeout)
                async with self._factory(**self._configuration) as client:
                    timer.cancel()
                    timer = None
                    while not self._closed:
                        request = await self._queue.get()
                        if request.result.cancelled():
                            continue
                        self._active = request
                        remaining = request.deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            self._fail(request, "timeout")
                            self._active = None
                            continue
                        try:
                            with anyio.fail_after(remaining):
                                if request.operation == "list":
                                    result = await self._tools(client)
                                else:
                                    raw = await client.call_tool(request.name, request.arguments)
                                    result = _result(raw, self.max_response_bytes)
                        except MCPError as exc:
                            self._fail(request, exc.code)
                            if exc.code != "tool_error":
                                failure = exc.code
                                break
                        except TimeoutError:
                            failure = "timeout"
                            self._fail(request, failure)
                            break
                        except Exception:
                            self._fail(request, "disconnected")
                            break
                        else:
                            if not request.result.done():
                                request.result.set_result(result)
                        if request.result.cancelled():
                            # Its caller no longer owns the uncertain outcome.
                            # Close before any queued operation gets transmitted.
                            break
                        self._active = None
        except MCPError as exc:
            failure = exc.code
        except BaseException:
            # Includes SDK ExceptionGroups/cancellation; never expose details.
            pass
        finally:
            if timer is not None:
                timer.cancel()
            self._closed = True
            if self._failure != "closed" or failure != "disconnected":
                self._failure = failure
            elif self._scope is not None and not self._scope.cancel_called:
                self._failure = failure
            self._scope = None
            self._fail(self._active, self._failure)
            self._active = None
            while not self._queue.empty():
                self._fail(self._queue.get_nowait(), self._failure)
            _QUIET.reset(token)

    async def close(self):
        self._closed = True
        self._failure = "closed"
        if self._scope is not None:
            self._scope.cancel()
        worker = self._worker
        if worker is None or worker.done():
            return
        # Do not use wait_for(worker): native cancellation defeats SDK cleanup.
        done, _ = await asyncio.wait({worker}, timeout=_CLOSE_SECONDS)
        if not done:
            raise MCPError("close_timeout")


class HTTPMCPClient(StdioMCPClient):
    """Authenticated fixed HTTP(S) endpoint using the same serialized worker.

    Test factories receive ``url``, ``bearer_token``, ``timeout_seconds`` and
    ``max_response_bytes``. No callback is registered, no redirect is followed,
    and no host proxy/authentication environment is inherited. HTTP on a Docker
    private network is supported; administrators choose the trusted endpoint.
    """

    def __init__(self, url, bearer_token, timeout_seconds=30, max_response_bytes=262144,
                 client_factory=None):
        if (type(url) is not str or not url or len(url) > 2048
                or any(char.isspace() or ord(char) < 32 for char in url) or "\\" in url
                or type(bearer_token) is not str or not 32 <= len(bearer_token) <= 8192
                or not bearer_token.isascii()
                or any(not 33 <= ord(char) <= 126 for char in bearer_token)):
            raise MCPError("invalid_config")
        try:
            parsed = urlsplit(url)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.query or parsed.fragment or "?" in url or "#" in url
                    or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
                raise ValueError
        except (ValueError, UnicodeError):
            raise MCPError("invalid_config") from None
        self._initialize(dict(url=url, bearer_token=bearer_token, timeout_seconds=timeout_seconds,
                              max_response_bytes=max_response_bytes),
                         timeout_seconds, max_response_bytes, client_factory or _http_sdk_context)
