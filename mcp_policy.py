"""Local MCP capability policy. Remote annotations/descriptions never grant access."""
from __future__ import annotations

import copy
import json
import math
import re
import unicodedata
from urllib.parse import urlsplit


# kind: read, write, account (persistent/remote account changes), transfer.
CAP_TOOLS = {
    "living_read_pdf_page": ("read", "读取网关已有PDF的一页；请通过living_world_reading保存阅读进度"),
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
PUBLIC_PROVIDERS = ("news", "reference")
NEWS_TOOLS = {"news_search": ("read", "按主题筛选近期公开报道，可留空看最近条目"),
              "news_read": ("read", "用新闻编号读取出处、发表时间、正文节选或订阅摘要"),
              "web_search": ("read", "用 Brave 搜索当前发言者本人直接请求查找的公开关键词"),
              "web_read": ("read", "用刚取得的结果编号打开网页，阅读有限正文")}
REFERENCE_TOOLS = {"reference_search": ("read", "按当前发言者直接请求了解的公开词条检索中英文维基百科"),
                   "reference_read": ("read", "按词条编号和语言分页读取真实百科摘录与版本")}
MASTODON_TOOLS = {"mastodon_search": ("read", "按本轮直接请求的公开主题搜索本站讨论"),
                  "mastodon_read": ("read", "读取一条公开原帖"),
                  "mastodon_mentions": ("read", "读取该账号收到的公开提及"),
                  "mastodon_identity": ("read", "核对此账号身份"),
                  "mastodon_reply": ("write", "主人私聊亲自发送「公开回复 帖子编号 正文」后，按原文回复指定公开帖。")}
CATALOG = {"cap": CAP_TOOLS, "pixiv": PIXIV_TOOLS, "news": NEWS_TOOLS, "reference": REFERENCE_TOOLS,
           "mastodon": MASTODON_TOOLS}
GROUP_READS = {("cap", "jm_get_album"), ("pixiv", "get_library_work"),
               *((p, t) for p in PUBLIC_PROVIDERS for t in CATALOG[p])}
_HIDDEN = re.compile(r"(?i)(?:password|secret|token|cookie|authorization|api.?key|headers|env|"
                     r"path|paths|file_uri|download_url|storage_root|raw_meta|metadata_json|novel_content|error|.*_path|.*_paths)$")
_CREDENTIAL = re.compile(r"(?i)(?:bearer\s+\S+|(?:token|password|api.?key|secret|cookie|signature)\s*[:=]\s*\S+)")
_LOCAL_PATH = re.compile(r"(?i)(?:file://|(?<![a-z0-9])[a-z]:[\\/]|\\\\|(?:^|\s)/(?!/)[^\s]+/)")


class PolicyError(ValueError):
    """Only a fixed error code, safe for logs and outward results."""


def public_search_requests(message):
    """Find explicit search objects in this speaker's original plain text."""
    from .owner import authored_message_text
    text = authored_message_text(message)
    if not text:
        return []
    from .dialogue_policy import _plain, _REPORT, _HYPOTHETICAL
    from .public_reference import public_topic
    from .unified_expression import _SECRET, _INSTRUCTION
    text = unicodedata.normalize("NFKC", text)
    if _SECRET.search(text) or _INSTRUCTION.search(text):
        return []
    requests = []
    plain = _plain(text)
    # Preserve offsets while hiding complete quotations. Incomplete quotations
    # cannot establish which later text belongs to the speaker's own request.
    plain = re.sub(r"'[^'\n]*'|‘[^’]*’|\x60[^\x60\n]*\x60",
                   lambda match: " " * len(match[0]), plain)
    if re.search(r'["“”「」『』‘\x60]|(?<![A-Za-z])[\x27’]|[\x27’](?![A-Za-z])', plain):
        return []
    for span in re.finditer(r"[^，,。！？!?；;\n]+", plain):
        clause = text[span.start():span.end()]
        match = re.match(
            r"(?i)^\s*(?:(?:请你|请|麻烦|劳驾|帮我|帮忙|能不能|可不可以|能否|可以|你能|你可以)\s*)*"
            r"(?:查(?:一下|一查|查|询)?|搜(?:一下|一搜|索|搜)?|检索|(?:我)?想(?:要)?了解|"
            r"介绍(?:一下)?|科普(?:一下)?|讲讲|了解一下|"
            r"(?:please\s+)?(?:search(?:\s+for)?|look\s+up|find\s+information\s+on|tell\s+me\s+about)\b)",
            span[0])
        if match:
            requested = clause[match.end():].lstrip(":： \t")
            quotes = {'"': '"', "'": "'", "“": "”", "‘": "’", "「": "」", "『": "』"}
            if requested and requested[0] in quotes:
                end = requested.find(quotes[requested[0]], 1)
                tail = requested[end+1:].strip() if end > 0 else requested
                if end < 0 or not re.fullmatch(r"(?:的?(?:公开)?(?:资料|信息|背景)|相关资料|谢谢|好吗|可以吗|吧|吗|呀|呢)*", tail):
                    continue
                requested = requested[1:end].strip()
            if (_REPORT.search(requested) or _HYPOTHETICAL.search(requested)
                    or re.search(r"室友|舍友|(?:的)?时候|当时|只是|举例|例子|说的|讲的|来敲门|"
                                 r"(?i:\b(?:said|example|quote|when)\b)", requested)):
                continue
            try:
                public_topic(requested)
            except ValueError:
                continue
            requests.append(requested)
    return requests[:8]


def check_public_query(provider, tool, args, context):
    if (provider, tool) not in {("reference", "reference_search"), ("news", "web_search"),
                                ("mastodon", "mastodon_search")}:
        return
    from .public_reference import public_topic
    from .unified_expression import _SECRET, _IDENTIFIER, _INSTRUCTION
    def folded(value):
        return " ".join(unicodedata.normalize("NFKC", value).casefold().split()) if isinstance(value, str) else ""
    topic = folded(args.get("query"))
    original = folded(context.get("query"))
    requests = context.get("public_search_terms")
    permitted_terms = requests if isinstance(requests, list) and len(requests) <= 8 else []
    try:
        public_topic(topic)
    except ValueError:
        raise PolicyError("ask_for_public_search_terms") from None
    if (not topic or len(topic) > 80 or topic not in original or _SECRET.search(original)
            or not any(topic in folded(terms) for terms in permitted_terms)
            or _IDENTIFIER.search(topic) or _INSTRUCTION.search(topic)):
        raise PolicyError("ask_for_public_search_terms")


def permitted(provider: str, tool: str, config, context: dict, owner: str) -> bool:
    spec = CATALOG.get(provider, {}).get(tool)
    if not spec or not owner or not context:
        return False
    scope, speaker = context.get("scope", ""), context.get("speaker_id", "")
    if (provider, tool) == ("mastodon", "mastodon_reply"):
        return scope == "private:"+owner and speaker == owner and config.social_mode == "参与讨论"
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
        if re.search(r"https?://(?:[^/\s?#]*@|\S*\?)", value, re.I):
            return "[受保护链接]"
        return value[:4000]
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    return None


def observation(provider: str, tool: str, args: dict, result: dict) -> dict | None:
    """Persist only an actual source detail, never task status or a model's paraphrase."""
    if (provider, tool) in {("news", "news_read"), ("reference", "reference_read"), ("news", "web_read")}:
        from .unified_expression import _INSTRUCTION, _SECRET
        import hashlib
        url = result.get("url")
        if not isinstance(url, str) or not 1 <= len(url) <= 1000:
            return None
        try:
            parts = urlsplit(url)
            if (parts.scheme != "https" or not parts.hostname or parts.username is not None or parts.password is not None
                    or parts.port not in (None, 443) or parts.query or parts.fragment or "\\" in url):
                return None
        except ValueError:
            return None
        if tool == "reference_read":
            language = args.get("language", "zh")
            if (result.get("page_id") != args.get("page_id") or result.get("language") != language
                    or parts.hostname != language+".wikipedia.org" or not parts.path.startswith("/wiki/")
                    or result.get("basis") != "encyclopedia_excerpt" or result.get("offset") != args.get("offset", 0)):
                return None
            key = f"wiki:{language}:{args['page_id']}"
            resource = key + ":" + str(args.get("offset", 0))
            label = "百科节选"
        else:
            if not isinstance(args.get("id"), str) or result.get("id") != args["id"]:
                return None
            if tool == "news_read":
                from .public_news import _article_url
                try:
                    if _article_url(args["id"])[1] != url or result.get("basis") not in {"article", "feed_summary"}:
                        return None
                except ValueError:
                    return None
            elif result.get("basis") != "web_excerpt":
                return None
            key = "article:" + hashlib.sha256(url.encode()).hexdigest()
            resource = key
            label = "订阅摘要" if result.get("basis") == "feed_summary" else "网页节选" if tool == "web_read" else "报道节选"
        fields = [result.get(k, "") for k in ("source", "title", "text")]
        if not all(isinstance(value, str) and value.strip() for value in fields):
            return None
        stamp = result.get("published_at") or result.get("updated_at") or ""
        # Store the publisher's bounded excerpt and attribution only, never the search terms or chat context.
        text = unicodedata.normalize("NFKC", f"{label}（{fields[0][:70]} {str(stamp)[:28]}）《{fields[1][:110]}》 {fields[2]}")
        text = " ".join(text.split())[:575]
        if _INSTRUCTION.search(text) or _SECRET.search(text):
            return None
        return {"resource_key": resource, "origin_key": key, "text": text, "url": url}
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
