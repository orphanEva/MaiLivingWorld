"""Local MCP capability policy. Remote annotations/descriptions never grant access."""
from __future__ import annotations

import copy
import json
import math
import re


# kind: read, write, account (persistent/remote account changes), transfer.
CAP_TOOLS = {
    "jm_get_album": ("read", "按作品编号读取标题、作者、标签与页数"),
    "jm_get_cached_pdf": ("read", "查询作品是否已有本地 PDF 缓存"),
    "jm_start_download": ("write", "创建后台下载和 PDF 生成任务；受理不代表完成"),
    "jm_get_download_status": ("read", "查询本插件发起的下载或传输任务"),
    "jm_list_tasks": ("read", "查看本插件当前主人私聊的任务"),
    "jm_retry_task": ("write", "重试本插件失败任务；原操作的权限仍适用"),
    "jm_publish_pdf": ("transfer", "将已有 PDF 上传到管理员配置的文件服务"),
    "jm_receive_pdf": ("transfer", "从配置文件服务接收签名 PDF 链接"),
}
PIXIV_TOOLS = {
    "search_library": ("read", "在本地 Pixiv 资料库检索作品、作者、标签；不联网补库"),
    "get_library_work": ("read", "读取指定类型和编号的作品元数据及文件索引"),
    "get_library_history": ("read", "读取作品历史版本摘要"),
    "get_library_version": ("read", "读取作品指定版本的元数据和文件索引"),
    "read_novel_excerpt": ("read", "分页阅读已缓存小说，不能声称读过未返回的正文"),
    "check_library_integrity": ("read", "检查缓存文件完整性，不修复文件"),
    "list_library_authors": ("read", "检索缓存中的作者"),
    "get_library_author": ("read", "查看缓存作者统计"),
    "list_library_tags": ("read", "检索缓存标签"),
    "list_library_series": ("read", "检索缓存合集"),
    "get_library_series": ("read", "读取指定合集摘要"),
    "get_library_stats": ("read", "查看本地资料库统计"),
    "series_archive_status": ("read", "查询合集归档进展"),
    "list_jobs": ("read", "查询本插件发起的归档或媒体任务"),
    "get_job": ("read", "查询本插件任务；succeeded 才表示完成"),
    "list_sync_sources": ("read", "查看已配置的持续同步来源"),
    "get_sync_source": ("read", "查看一个同步来源及其进度"),
    "account_identity": ("read", "向 Pixiv 验证后端配置账户，不返回凭据"),
    "list_account_migrations": ("read", "查看账户迁移记录"),
    "get_account_migration": ("read", "查看账户迁移进度"),
    "list_account_migration_items": ("read", "查看账户迁移条目"),
    "ops_status": ("read", "查询后端运行状态"),
    "archive_work": ("write", "创建单作品归档任务，默认不展开合集、不新增收藏"),
    "archive_series": ("write", "明确归档一整个合集，默认不新增收藏"),
    "retry_job": ("write", "重试本插件任务，保留最初操作权限和队列"),
    "generate_ugoira_preview": ("write", "从本地 Ugoira 缓存生成 MP4 任务"),
    "upsert_author_source": ("account", "创建或更改持续作者订阅"),
    "upsert_bookmark_source": ("account", "创建或更改持续收藏订阅"),
    "upsert_series_source": ("account", "创建或更改持续合集订阅"),
    "upsert_followed_series_source": ("account", "同步配置账户关注的系列"),
    "patch_sync_source": ("account", "修改持续同步来源设置"),
    "run_sync_source": ("account", "执行一个已配置的同步来源"),
    "create_account_migration": ("account", "把本地库收藏及合集关注迁移到后端账户"),
    "retry_account_migration": ("account", "重试账户迁移，会新增远端收藏或关注"),
}
CATALOG = {"cap": CAP_TOOLS, "pixiv": PIXIV_TOOLS}
GROUP_READS = {("cap", "jm_get_album"), ("pixiv", "get_library_work")}
_HIDDEN = re.compile(r"(?i)(?:password|secret|token|cookie|authorization|api.?key|headers|env|"
                     r"path|paths|file_uri|download_url|storage_root|raw_meta|metadata_json|novel_content|error|.*_path|.*_paths)$")
_CREDENTIAL = re.compile(r"(?i)(?:bearer\s+\S+|(?:token|password|api.?key|secret|cookie|signature)\s*[:=]\s*\S+)")
_LOCAL_PATH = re.compile(r"(?i)(?:file://|[a-z]:[\\/]|\\\\|(?:^|\s)/(?!/)[^\s]+/)")


class PolicyError(ValueError):
    """Only a fixed error code, safe for logs and outward results."""


def permitted(provider: str, tool: str, config, context: dict, owner: str) -> bool:
    spec = CATALOG.get(provider, {}).get(tool)
    if not spec or not owner or not context:
        return False
    scope, speaker = context.get("scope", ""), context.get("speaker_id", "")
    if config.owner_only and speaker != owner:
        return False
    if scope.startswith("group:"):
        return bool(config.allow_group_reads and (provider, tool) in GROUP_READS)
    if scope != "private:" + speaker:
        return False
    if speaker != owner:
        return (provider, tool) in GROUP_READS
    kind = spec[0]
    return (kind == "read" or (config.allow_writes and
            (kind == "write" or (kind == "account" and config.allow_account_actions)
             or (kind == "transfer" and config.allow_file_transfer))))


def normalize_arguments(provider: str, tool: str, arguments, config) -> dict:
    if tool not in CATALOG.get(provider, {}) or not isinstance(arguments, dict):
        raise PolicyError("invalid_tool_arguments")
    try:
        raw = json.dumps(arguments, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise PolicyError("invalid_tool_arguments") from None
    if len(raw.encode()) > 12000:
        raise PolicyError("invalid_tool_arguments")
    args = copy.deepcopy(arguments)
    # Upstream defaults intentionally have broader effects; set these explicitly.
    if provider == "pixiv" and tool in {"archive_work", "archive_series"}:
        args.setdefault("bookmark_if_missing", False)
        args.setdefault("force_refresh", False)
        if tool == "archive_work":
            args.setdefault("expand_series", False)
        if args.get("bookmark_if_missing") is not False and not config.allow_account_actions:
            raise PolicyError("account_actions_disabled")
    if provider == "pixiv" and tool == "search_library":
        args.setdefault("page_size", 10)
    if provider == "pixiv" and tool == "read_novel_excerpt":
        args.setdefault("length", 2000)
    if provider == "cap" and "jm_id" in args:
        value = args["jm_id"]
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,18}", value) or int(value) <= 0:
            raise PolicyError("invalid_tool_arguments")
        args["jm_id"] = str(int(value))
    return args


def validate_arguments(provider: str, tool: str, arguments, schema: dict, config) -> dict:
    args = normalize_arguments(provider, tool, arguments, config)
    if not isinstance(schema, dict):
        raise PolicyError("unsupported_tool_schema")

    def check_schema(value, depth=0):
        if depth > 30:
            raise PolicyError("unsupported_tool_schema")
        if isinstance(value, dict):
            for name in ("$ref", "$dynamicRef", "$recursiveRef"):
                if name in value and (not isinstance(value[name], str) or not value[name].startswith("#/")):
                    raise PolicyError("unsupported_tool_schema")
            for child in value.values():
                check_schema(child, depth+1)
        elif isinstance(value, list):
            for child in value:
                check_schema(child, depth+1)
    check_schema(schema)
    properties = schema.get("properties", {})
    if not isinstance(properties, dict) or set(args) - set(properties):
        raise PolicyError("invalid_tool_arguments")
    try:
        from jsonschema import Draft202012Validator
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(args)
    except ImportError:
        raise PolicyError("mcp_dependencies_missing") from None
    except Exception:
        raise PolicyError("invalid_tool_arguments") from None
    return args


def clean_result(value, *, depth=0):
    """Bounded data only. Never pass paths, credentials or error bodies to the model."""
    if depth > 10:
        return None
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:80]:
            if not isinstance(key, str) or _HIDDEN.fullmatch(key):
                continue
            result[key[:80]] = clean_result(item, depth=depth+1)
        return result
    if isinstance(value, list):
        return [clean_result(item, depth=depth+1) for item in value[:50]]
    if isinstance(value, str):
        if _CREDENTIAL.search(value) or _LOCAL_PATH.search(value):
            return "[受保护字段]"
        # Signed/credential-bearing links never leave even under an unexpected key.
        if re.search(r"https?://\S*[?@]", value, re.I):
            return "[受保护链接]"
        return value[:4000]
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    return None


def observation(provider: str, tool: str, args: dict, result: dict) -> dict | None:
    """Persist only an actual source detail, never task status or a model's paraphrase."""
    if provider == "cap" and tool == "jm_get_album":
        if str(result.get("jm_id")) != args["jm_id"] or not isinstance(result.get("title"), str):
            return None
        fields = {key: clean_result(result[key]) for key in ("title", "authors", "tags", "page_count", "chapter_count") if key in result}
        return {"resource_key": "album:"+args["jm_id"], "origin_key": "album:"+args["jm_id"],
                "text": "作品元数据（未阅读正文）："+json.dumps(fields, ensure_ascii=False)[:540]}
    if provider == "pixiv" and tool == "get_library_work":
        if (result.get("pixiv_id") != args["pixiv_id"] or result.get("accessible") is False
                or result.get("status") in {"private", "deleted", "auth_required"}):
            return None
        fields = {key: clean_result(result[key]) for key in ("title", "author", "author_name", "tags", "type") if key in result}
        if not fields.get("title"):
            return None
        key = "work:"+args["kind"]+":"+str(args["pixiv_id"])
        return {"resource_key": key, "origin_key": key,
                "text": "Pixiv 作品元数据（未阅读或观看正文）："+json.dumps(fields, ensure_ascii=False)[:530]}
    if provider == "pixiv" and tool == "read_novel_excerpt":
        if (result.get("pixiv_id") != args["pixiv_id"] or result.get("kind") != "novel"
                or result.get("content_available") is not True or not isinstance(result.get("text"), str)):
            return None
        origin = "work:novel:"+str(args["pixiv_id"])
        return {"resource_key": origin+":excerpt:"+str(result.get("version"))+":"+str(result.get("offset", 0)),
                "origin_key": origin, "text": "小说片段（虚构文本，不是现实事实）："+
                " ".join(str(clean_result(result["text"])).split())[:560]}
    return None
