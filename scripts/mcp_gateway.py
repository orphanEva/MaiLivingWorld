"""Authenticated internal Streamable HTTP gateway for one fixed stdio provider.

Run one process per provider. The gateway never accepts commands or environment
settings through MCP. Application authorization remains in MaiLivingWorld.
"""
from __future__ import annotations

import asyncio
import copy
import hmac
import json
import logging
import math
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from MaiLivingWorld.mcp_client import MCPError, StdioMCPClient
from MaiLivingWorld.mcp_policy import CATALOG
from MaiLivingWorld.document_reader import (DocumentError, PDF_DESCRIPTION, PDF_SCHEMA, PDF_TOOL,
                                           pdf_arguments, read_cached_pdf_page)

_CHILD_NAMES = {
    "cap": {"JM_CONFIG_PATH", "JM_TASK_DB_PATH", "JM_MAX_CONCURRENT_DOWNLOADS",
            "JM_TASK_MAX_ATTEMPTS", "JM_TASK_RETRY_BASE_SECONDS", "JM_FILE_SERVER_URL",
            "JM_FILE_API_TOKEN", "JM_FILE_RECEIVE_DIR", "JM_FILE_DOWNLOAD_BASE_URL",
            "JM_FILE_MAX_BYTES", "JM_FILE_TRANSFER_TIMEOUT_SECONDS", "JM_FILE_MAX_CONCURRENT_TRANSFERS"},
    "pixiv": {"PIXIV_MCP_BACKEND_URL", "PIXIV_MCP_TIMEOUT_SECONDS",
              "PIXIV_MCP_READ_ONLY", "PIXIV_MCP_MAX_RESPONSE_BYTES"},
}
_PRIVATE_FIELD = re.compile(r"(?i)^(?:api_?key|authorization|password|cookie|secret|token|"
                            r"access_token|refresh_token|error|last_error|traceback|exception|stderr|stdout)$")
_LOG = logging.getLogger("mailivingworld.mcp_gateway")


class GatewayConfigError(ValueError):
    def __init__(self):
        super().__init__("invalid_gateway_configuration")


def _json_list(raw):
    try:
        value = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        raise GatewayConfigError() from None
    if not isinstance(value, list) or len(value) > 64 or any(not isinstance(v, str) or "\0" in v for v in value):
        raise GatewayConfigError()
    return value


@dataclass(frozen=True)
class GatewaySettings:
    provider: str
    token: str = field(repr=False)
    command: str
    args: tuple[str, ...]
    cwd: str
    child_env: dict[str, str] = field(default_factory=dict, repr=False)
    allowed_origins: tuple[str, ...] = ()
    allowed_hosts: tuple[str, ...] = ()
    timeout_seconds: float = 60
    max_response_bytes: int = 1024 * 1024
    close_seconds: float = 12
    pdf_read_enabled: bool = False
    pdf_root: str = field(default="/var/lib/cap/downloads", repr=False)

    def __post_init__(self):
        if (self.provider not in _CHILD_NAMES or not isinstance(self.token, str)
                or not 32 <= len(self.token) <= 4096 or any(not 33 <= ord(c) <= 126 for c in self.token)
                or not isinstance(self.command, str) or not self.command.strip() or "\0" in self.command
                or not isinstance(self.cwd, str) or not self.cwd.strip() or "\0" in self.cwd
                or not isinstance(self.args, tuple) or len(self.args) > 64
                or any(not isinstance(a, str) or len(a) > 8192 or "\0" in a for a in self.args)
                or type(self.timeout_seconds) not in (int, float) or not math.isfinite(self.timeout_seconds)
                or not 1 <= self.timeout_seconds <= 300
                or type(self.close_seconds) not in (int, float) or not math.isfinite(self.close_seconds)
                or not 0 < self.close_seconds <= 30
                or type(self.max_response_bytes) is not int or not 1024 <= self.max_response_bytes <= 4*1024*1024):
            raise GatewayConfigError()
        if (type(self.pdf_read_enabled) is not bool or self.pdf_read_enabled and self.provider != "cap"
                or not isinstance(self.pdf_root, str) or not self.pdf_root or len(self.pdf_root) > 4096
                or "\0" in self.pdf_root or ".." in Path(self.pdf_root).parts
                or not (Path(self.pdf_root).is_absolute() or PurePosixPath(self.pdf_root).is_absolute())):
            raise GatewayConfigError()
        for origin in self.allowed_origins:
            try:
                parts = urlsplit(origin)
                if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.path
                        or parts.username is not None or parts.password is not None or parts.query or parts.fragment
                        or any(ord(c) <= 32 or ord(c) > 126 for c in origin) or "\\" in origin):
                    raise GatewayConfigError()
                _ = parts.port
            except (TypeError, ValueError):
                raise GatewayConfigError() from None
        if (not self.allowed_hosts or len(self.allowed_hosts) > 32
                or any(not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,253}", host)
                       for host in self.allowed_hosts)):
            raise GatewayConfigError()
        names = _CHILD_NAMES[self.provider] | {"PYTHONUTF8", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED"}
        if self.provider == "pixiv":
            names.add("PIXIV_MCP_API_KEY")
        if (not isinstance(self.child_env, dict) or set(self.child_env)-names
                or any(not isinstance(value, str) or len(value) > 32768 or "\0" in value
                       for value in self.child_env.values())):
            raise GatewayConfigError()

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        try:
            provider = env["MLW_MCP_GATEWAY_PROVIDER"]
            pdf_enabled = env.get("MLW_CAP_PDF_READ_ENABLED", "false").lower()
            if pdf_enabled not in {"true", "false", "1", "0"}:
                raise GatewayConfigError()
            child = {key: env[key] for key in _CHILD_NAMES[provider] if env.get(key)}
            child.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1")
            if provider == "pixiv":
                # The backend key is mounted only in the Pixiv service and is
                # passed only to its child; never inherit the gateway's env.
                secret_path = Path(env["MLW_MCP_GATEWAY_PIXIV_KEY_FILE"])
                with secret_path.open("rb") as stream:
                    key = stream.read(4097).decode("ascii").strip()
                if not 32 <= len(key) <= 4096 or any(not 33 <= ord(c) <= 126 for c in key):
                    raise GatewayConfigError()
                child["PIXIV_MCP_API_KEY"] = key
            return cls(
                provider=provider, token=env["MLW_MCP_GATEWAY_TOKEN"],
                command=env["MLW_MCP_GATEWAY_COMMAND"], args=tuple(_json_list(env.get("MLW_MCP_GATEWAY_ARGS", "[]"))),
                cwd=env["MLW_MCP_GATEWAY_CWD"], child_env=child,
                allowed_origins=tuple(_json_list(env.get("MLW_MCP_GATEWAY_ALLOWED_ORIGINS", "[]"))),
                allowed_hosts=tuple(_json_list(env.get("MLW_MCP_GATEWAY_ALLOWED_HOSTS", json.dumps([provider+"-mcp:8000"])))),
                timeout_seconds=float(env.get("MLW_MCP_GATEWAY_TIMEOUT_SECONDS", "60")),
                max_response_bytes=int(env.get("MLW_MCP_GATEWAY_MAX_RESPONSE_BYTES", "1048576")),
                pdf_read_enabled=pdf_enabled in {"true", "1"},
                pdf_root=env.get("MLW_CAP_PDF_ROOT", "/var/lib/cap/downloads"),
            )
        except (KeyError, TypeError, ValueError, OSError, UnicodeError):
            raise GatewayConfigError() from None


def _safe_result(value, secrets, depth=0):
    if depth > 24:
        raise ValueError("invalid_result")
    if isinstance(value, dict):
        return {key: ("[redacted]" if _PRIVATE_FIELD.fullmatch(key) else _safe_result(child, secrets, depth+1))
                for key, child in value.items()}
    if isinstance(value, list):
        return [_safe_result(child, secrets, depth+1) for child in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret in value:
                value = value.replace(secret, "[redacted]")
    return value


def create_app(settings: GatewaySettings, *, client_factory=None):
    """Create an ASGI app; a test factory may supply list_tools/call_tool/close."""
    import anyio
    from mcp import types
    from mcp.server.lowlevel import Server
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.server.transport_security import TransportSecuritySettings
    from mcp.shared.exceptions import MCPError as ProtocolError
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    state = {"client": None, "tools": {}, "ready": False, "running": False, "restart_needed": False}
    catalog = dict(CATALOG[settings.provider])
    # The gateway owns this schema and implementation. Never proxy a provider's
    # similarly named tool, even when its name appears in the plugin catalog.
    catalog.pop(PDF_TOOL, None)
    if settings.provider == "cap" and settings.pdf_read_enabled:
        catalog[PDF_TOOL] = ("read", PDF_DESCRIPTION)
    operation_lock = asyncio.Lock()
    secrets = [settings.token] + [v for k, v in settings.child_env.items()
                                  if len(v) >= 16 and re.search(r"KEY|TOKEN|PASSWORD|COOKIE", k)]

    def failed(code="upstream_tool_failed"):
        return types.CallToolResult(is_error=True, content=[types.TextContent(text=code)])

    async def ensure_client():
        # Caller owns operation_lock through its operation. Closing the old
        # child must finish before another cap writer can acquire the DB lease.
        current = state["client"]
        if current is not None and not state["restart_needed"] and not getattr(current, "retired", False):
            return
        state["ready"] = False
        if current is not None:
            with anyio.fail_after(settings.close_seconds):
                await current.close()
        state["client"] = None
        state["tools"] = {}
        state["restart_needed"] = False
        client = (client_factory or StdioMCPClient)(
            command=settings.command, args=list(settings.args), cwd=settings.cwd,
            env=dict(settings.child_env), timeout_seconds=settings.timeout_seconds,
            max_response_bytes=settings.max_response_bytes)
        state["client"] = client
        try:
            with anyio.fail_after(settings.timeout_seconds):
                discovered = await client.list_tools()
            selected = {}
            for name, info in discovered.items():
                if name not in catalog or name == PDF_TOOL:
                    continue
                schema = info.get("input_schema")
                encoded = json.dumps(schema, ensure_ascii=False, allow_nan=False)
                if (not isinstance(schema, dict) or schema.get("type") != "object"
                        or any(secret in encoded for secret in secrets)):
                    raise ValueError("invalid_schema")
                selected[name] = copy.deepcopy(schema)
            if PDF_TOOL in catalog and "jm_get_cached_pdf" in selected:
                selected[PDF_TOOL] = copy.deepcopy(PDF_SCHEMA)
            state["tools"] = selected
            state["ready"] = True
        except BaseException:
            state["restart_needed"] = True
            raise

    async def list_tools(_context, params):
        if params is not None and params.cursor:
            return types.ListToolsResult(tools=[])
        try:
            async with operation_lock:
                await ensure_client()
                return types.ListToolsResult(tools=[types.Tool(
                    name=name, description=catalog[name][1],
                    input_schema=copy.deepcopy(schema)) for name, schema in sorted(state["tools"].items())])
        except Exception:
            raise ProtocolError(-32603, "upstream_unavailable") from None

    async def call_tool(_context, params):
        if params.name not in catalog:
            return failed("tool_not_available")
        try:
            async with operation_lock:
                await ensure_client()
                if params.name not in state["tools"]:
                    return failed("tool_not_available")
                try:
                    if params.name == PDF_TOOL:
                        ident, index = pdf_arguments(params.arguments or {})
                        cached = await state["client"].call_tool("jm_get_cached_pdf", {"jm_id": ident})
                        result = await read_cached_pdf_page(cached, jm_id=ident, page_index=index, root=settings.pdf_root)
                    else:
                        result = await state["client"].call_tool(params.name, params.arguments or {})
                except MCPError as exc:
                    if exc.code not in {"tool_error", "invalid_request", "queue_full"}:
                        state["restart_needed"], state["ready"] = True, False
                    raise
            if not isinstance(result, dict):
                return failed()
            result = _safe_result(result, secrets)
            raw = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            if len(raw.encode("utf-8")) > settings.max_response_bytes:
                return failed()
            # Duplicating image base64 in text would exceed the client's default
            # 256 KiB aggregate result limit and turn visual input into raw text.
            content = "已读取一页缓存 PDF；文字和图片位于结构化结果，尚未进行视觉理解。" if params.name == PDF_TOOL else raw
            return types.CallToolResult(content=[types.TextContent(text=content)], structured_content=result)
        except DocumentError as exc:
            if params.name == PDF_TOOL and exc.code == "pdf_not_cached":
                # A successful cache lookup established absence. Keep this
                # distinguishable from a transport/parser failure so consumers
                # can withdraw dependent observations without inspecting errors.
                unavailable = {"provider": "cap", "kind": "pdf_page", "jm_id": ident,
                               "available": False, "reason": "source_unavailable"}
                return types.CallToolResult(content=[types.TextContent(text="当前没有这部作品的可读 PDF 缓存。")],
                                            structured_content=unavailable)
            return failed(exc.code)
        except Exception:
            # Never include provider errors, argv, environment or stack traces.
            return failed()

    server = Server("mailivingworld-"+settings.provider, version="1.0.0",
                    on_list_tools=list_tools, on_call_tool=call_tool)
    manager = StreamableHTTPSessionManager(
        server, stateless=True, json_response=True, max_request_body_size=65536,
        security_settings=TransportSecuritySettings(
            allowed_hosts=list(settings.allowed_hosts), allowed_origins=list(settings.allowed_origins)))

    @asynccontextmanager
    async def lifespan(_app):
        try:
            try:
                async with operation_lock:
                    await ensure_client()
            except Exception:
                raise RuntimeError("upstream_unavailable") from None
            async with manager.run():
                state["running"] = True
                yield
        finally:
            state["ready"], state["running"] = False, False
            with anyio.move_on_after(settings.close_seconds, shield=True):
                try:
                    if state["client"] is not None:
                        await state["client"].close()
                except Exception:
                    _LOG.warning("mcp_gateway_child_close_failed")

    class Endpoint:
        async def __call__(self, scope, receive, send):
            headers = {}
            for key, value in scope.get("headers", []):
                headers.setdefault(key.lower(), []).append(value)
            auth = headers.get(b"authorization", [])
            expected = ("Bearer "+settings.token).encode("ascii")
            if len(auth) != 1 or len(auth[0]) > 4103 or not hmac.compare_digest(auth[0], expected):
                return await JSONResponse({"error": "unauthorized"}, 401,
                    headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
            origin = headers.get(b"origin", [])
            if origin and (len(origin) != 1 or origin[0].decode("latin-1") not in settings.allowed_origins):
                return await JSONResponse({"error": "origin_denied"}, 403)(scope, receive, send)
            host = headers.get(b"host", [])
            if len(host) != 1 or host[0].decode("latin-1") not in settings.allowed_hosts:
                return await JSONResponse({"error": "host_denied"}, 421)(scope, receive, send)
            if not state["running"]:
                return await JSONResponse({"error": "gateway_unavailable"}, 503)(scope, receive, send)
            started = False

            async def guarded_send(message):
                nonlocal started
                if message["type"] == "http.response.start":
                    started = True
                await send(message)

            try:
                with anyio.fail_after(settings.timeout_seconds+5):
                    await manager.handle_request(scope, receive, guarded_send)
            except Exception:
                if not started:
                    await JSONResponse({"error": "gateway_unavailable"}, 503)(scope, receive, send)
                else:
                    await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def health(_request):
        ready = state["ready"] and not state["restart_needed"] and not getattr(state["client"], "retired", False)
        return JSONResponse({"status": "ok" if ready else "unavailable"}, 200 if ready else 503)

    return Starlette(routes=[Route("/mcp", Endpoint(), methods=["GET", "POST", "DELETE"]),
                             Route("/health", health)], lifespan=lifespan, debug=False)


def main():
    import uvicorn
    try:
        app = create_app(GatewaySettings.from_env())
    except GatewayConfigError:
        raise SystemExit("invalid_gateway_configuration") from None
    # One worker owns one child process and, for cap, one task-database lease.
    uvicorn.run(app, host="0.0.0.0", port=8000, workers=1, access_log=False,
                log_level="warning", proxy_headers=False, timeout_graceful_shutdown=15)


if __name__ == "__main__":
    main()
