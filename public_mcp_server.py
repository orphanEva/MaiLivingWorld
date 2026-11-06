"""Built-in public information and Mastodon MCP servers on separate stdio connections."""
from __future__ import annotations

import asyncio
from importlib import import_module
import json
import os
from pathlib import Path
import sys
from types import ModuleType

from jsonschema import Draft202012Validator
import mcp
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


_SOCIAL_ERRORS = {
    "mastodon_unauthorized", "mastodon_forbidden", "mastodon_not_configured", "mastodon_reply_uncertain",
    "mastodon_not_public", "mastodon_self_reply", "mastodon_rate_limited", "mastodon_rejected",
    "mastodon_not_found", "mastodon_extra_recipients", "invalid_mastodon_text", "mastodon_invalid_config",
}


def _inputs(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


_LANGUAGE = {"type": "string", "enum": ["zh", "en"], "default": "zh"}
_PUBLIC_QUERY = {"type": "string", "minLength": 1, "maxLength": 80}
_STATUS_ID = {"type": "string", "pattern": "^[1-9][0-9]{0,29}$", "maxLength": 30}
_TOOLS = {
    "news": {
        "news_search": ("按关键词筛选已接入发布方的近期新闻，留空列出近期条目。", _inputs({
            "query": {"type": "string", "maxLength": 80, "default": ""},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
        })),
        "news_read": ("用新闻搜索返回的编号读取发布方报道，按返回标识区分正文和订阅摘要。", _inputs({
            "id": {"type": "string", "maxLength": 32,
                   "pattern": "^(?:un:[0-9]{4}:(?:0[1-9]|1[0-2]):[0-9]{5,10}|ithome:[0-9]{1,2}:[0-9]{3}:[0-9]{3})$"},
        }, ("id",))),
    },
    "reference": {
        "reference_search": ("用公开主题关键词搜索中文或英文维基百科，返回可读取的页面编号。", _inputs({
            "query": _PUBLIC_QUERY, "language": _LANGUAGE,
            "limit": {"type": "integer", "minimum": 1, "maximum": 5, "default": 5},
        }, ("query",))),
        "reference_read": ("按页面编号和语言读百科正文节选，后续段落用返回的偏移继续。", _inputs({
            "page_id": {"type": "integer", "minimum": 1, "maximum": 2147483647}, "language": _LANGUAGE,
            "offset": {"type": "integer", "minimum": 0, "maximum": 12000, "default": 0},
        }, ("page_id",))),
    },
    "mastodon": {
        "mastodon_identity": ("核验已配置的 Mastodon 账号。", _inputs({})),
        "mastodon_search": ("用公开关键词查找可读取的公开帖子。", _inputs({
            "query": _PUBLIC_QUERY, "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
        }, ("query",))),
        "mastodon_read": ("按实例内帖子编号读取公开帖子。", _inputs({"id": _STATUS_ID}, ("id",))),
        "mastodon_mentions": ("读取已配置账号收到的可用公开提及。", _inputs({
            "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
        })),
        "mastodon_reply": ("使用已配置的账号回复指定公开帖子，需要可信操作编号。", _inputs({
            "id": _STATUS_ID, "text": {"type": "string", "minLength": 1, "maxLength": 500},
            "idempotency_key": {"type": "string", "pattern": "^[A-Za-z0-9_-]{16,80}$", "maxLength": 80},
        }, ("id", "text", "idempotency_key"))),
    },
}
_WEB_TOOLS = {
    "web_search": ("用少量公开关键词搜索网页，返回搜索摘要和临时结果编号。", _inputs({
        "query": _PUBLIC_QUERY, "limit": {"type": "integer", "minimum": 1, "maximum": 5, "default": 5},
    }, ("query",))),
    "web_read": ("读取本服务搜索结果编号对应的公开网页正文节选。", _inputs({
        "id": {"type": "string", "pattern": "^web_[0-9a-f]{32}$", "maxLength": 36},
    }, ("id",))),
}


def _data_module(module_name):
    package = __package__
    if not package:
        # WebUI installs can have dots or spaces in their directory name. Resolve
        # sibling modules (and their relative imports) inside this fixed directory.
        package = "_mailivingworld_builtin_sources"
        namespace = ModuleType(package)
        namespace.__path__ = [str(Path(__file__).resolve().parent)]
        sys.modules.setdefault(package, namespace)
    return import_module("." + module_name, package=package)


def create_server(provider):
    if provider not in _TOOLS:
        raise ValueError("unknown_provider")
    definitions = dict(_TOOLS[provider])
    module = _data_module("mastodon_api" if provider == "mastodon" else "public_" + provider)
    handlers = {name: getattr(module, name) for name in definitions}
    if provider == "news" and os.environ.get("MLW_BRAVE_SEARCH_KEY", "").strip():
        web = _data_module("public_web_search")
        definitions.update(_WEB_TOOLS)
        handlers.update({name: getattr(web, name) for name in _WEB_TOOLS})
    validators = {name: Draft202012Validator(schema) for name, (_, schema) in definitions.items()}
    catalog = [types.Tool.model_validate({"name": name, "description": description, "inputSchema": schema})
               for name, (description, schema) in definitions.items()]
    lock = asyncio.Lock()

    def failed(code):
        return types.CallToolResult.model_validate({"isError": True, "content": [{"type": "text", "text": code}]})

    async def call(name, arguments):
        if name not in definitions:
            return failed("unknown_tool")
        args = {} if arguments is None else arguments
        if not validators[name].is_valid(args):
            return failed("invalid_arguments")
        try:
            async with lock:
                result = await asyncio.to_thread(handlers[name], **args)
            if not isinstance(result, dict):
                return failed("source_unavailable")
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            return types.CallToolResult.model_validate({"structuredContent": result, "content": [
                {"type": "text", "text": encoded}]})
        except Exception as exc:
            # Network/parse errors can contain queries and raw remote content.
            if (provider == "mastodon" and isinstance(exc, ValueError) and len(exc.args) == 1
                    and isinstance(exc.args[0], str) and exc.args[0] in _SOCIAL_ERRORS):
                return failed(exc.args[0])
            return failed("source_unavailable")

    if hasattr(mcp, "Client"):  # SDK 2 passes a request context and typed params.
        async def list2(_context, params):
            page = [] if params and params.cursor else catalog
            return types.ListToolsResult(tools=page)

        async def call2(_context, params):
            return await call(params.name, params.arguments)

        return Server("mailivingworld-" + provider, on_list_tools=list2, on_call_tool=call2)

    server = Server("mailivingworld-" + provider)

    @server.list_tools()
    async def list1():
        return catalog

    # Use our local validator for both SDK generations. Its fixed failure code
    # avoids reflecting the rejected input into the model's context.
    server.call_tool(validate_input=False)(call)
    return server


async def serve(provider):
    server = create_server(provider)
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in _TOOLS:
        raise SystemExit("usage: public_mcp_server.py {news|reference|mastodon}")
    asyncio.run(serve(sys.argv[1]))
