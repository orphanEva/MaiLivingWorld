"""Bounded image reads from an administrator's Pixiv cache backend.

Only current files returned by the trusted get_library_work tool can select a
resource. This module does not discover credentials, follow redirects, use
environment proxies, write files, or send messages. The caller must authenticate
the audience and revalidate that audience after awaiting the result.
"""
from __future__ import annotations

import asyncio
import base64
import math
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


_MAX_BYTES = 32 * 1024 * 1024
_MAX_ID = 9223372036854775807
_MIMES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
          ".gif": "image/gif", ".webp": "image/webp"}
_CODES = frozenset({"invalid_config", "invalid_details", "unavailable", "file_not_found",
                    "invalid_media_path", "too_large", "timeout", "redirect", "http_error",
                    "invalid_image", "fetch_failed"})


class MediaError(RuntimeError):
    """Only a fixed code is exposed, never an endpoint, credential or response body."""

    def __init__(self, code: str):
        self.code = code if isinstance(code, str) and code in _CODES else "fetch_failed"
        super().__init__(self.code)


def _integer(value, low=1, high=_MAX_ID):
    return type(value) is int and low <= value <= high


def _backend(value):
    if (not isinstance(value, str) or not value or len(value) > 2048
            or not value.isascii() or any(ord(char) <= 32 or ord(char) == 127 for char in value)
            or any(char in value for char in "\\%?#")):
        raise MediaError("invalid_config")
    try:
        parts = urlsplit(value)
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.password is not None
                or (parts.port is not None and not 1 <= parts.port <= 65535)
                or not re.fullmatch(r"[A-Za-z0-9._~/-]*", parts.path)
                or "//" in parts.path or any(segment in {".", ".."} for segment in parts.path.split("/"))):
            raise ValueError()
        # DNS names, local service names and bracketed IPv6 are all valid fixed
        # administrator destinations; no public-IP restriction is imposed.
        if not re.fullmatch(r"[A-Za-z0-9._:\[\]-]+", parts.netloc):
            raise ValueError()
    except (TypeError, ValueError):
        raise MediaError("invalid_config") from None
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def _namespace(kind):
    return "illust" if kind in {"illust", "manga", "ugoira"} else "novel" if kind == "novel" else ""


def _select(details, pixiv_id, page_index, max_bytes):
    if (not isinstance(details, dict) or not _integer(pixiv_id)
            or not _integer(page_index, 0, 1000000000)
            or type(details.get("pixiv_id")) is not int or details["pixiv_id"] != pixiv_id
            or not _integer(details.get("current_version"))):
        raise MediaError("invalid_details")
    if (("accessible" in details and type(details["accessible"]) is not bool)
            or ("status" in details and not isinstance(details["status"], str))):
        raise MediaError("invalid_details")
    if details.get("accessible") is False or details.get("status") in {"private", "deleted", "auth_required"}:
        raise MediaError("unavailable")
    kind = details.get("type")
    if not isinstance(kind, str) or not _namespace(kind):
        raise MediaError("invalid_details")
    if "kind" in details and details["kind"] != _namespace(kind):
        raise MediaError("invalid_details")
    files = details.get("current_files")
    if not isinstance(files, list) or len(files) > 100:
        raise MediaError("invalid_details")
    matches = [item for item in files if isinstance(item, dict)
               and type(item.get("page_index")) is int and item["page_index"] == page_index
               and isinstance(item.get("file_type"), str) and item["file_type"] in {"image", "cover"}]
    if not matches:
        raise MediaError("file_not_found")
    # Do not guess which of conflicting entries is authoritative.
    if len(matches) != 1:
        raise MediaError("invalid_details")
    item = matches[0]
    size = item.get("size_bytes")
    if size is not None and not _integer(size, 0):
        raise MediaError("invalid_details")
    if size is not None and size > max_bytes:
        raise MediaError("too_large")
    path, filename = item.get("download_url"), item.get("filename")
    if (not isinstance(path, str) or not path.isascii() or len(path) > 2048
            or any(ord(char) <= 32 or ord(char) == 127 for char in path)
            or any(char in path for char in "\\?#")
            or re.search(r"%(?:2[fFeE]|5[cC]|25)", path)
            or re.search(r"%(?![0-9A-Fa-f]{2})", path)):
        raise MediaError("invalid_media_path")
    match = re.fullmatch(r"/media/(illust|manga|ugoira|novel)/([1-9][0-9]*)/versions/([1-9][0-9]*)/([^/]+)", path)
    if (not match or _namespace(match[1]) != _namespace(kind)
            or match[2] != str(pixiv_id) or match[3] != str(details["current_version"])):
        raise MediaError("invalid_media_path")
    try:
        decoded = unquote(match[4], encoding="utf-8", errors="strict")
    except UnicodeError:
        raise MediaError("invalid_media_path") from None
    if (not isinstance(filename, str) or decoded != filename or not decoded
            or len(decoded) > 255 or decoded in {".", ".."}
            or any(char in decoded for char in "/\\%:?#")
            or any(ord(char) < 32 or ord(char) == 127 for char in decoded)
            or decoded != decoded.strip()):
        raise MediaError("invalid_media_path")
    extension = "." + decoded.rsplit(".", 1)[-1].lower() if "." in decoded else ""
    if extension not in _MIMES:
        raise MediaError("invalid_image")
    return path, extension


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        fp.close()
        raise MediaError("redirect")


def _fetch(endpoint, headers, timeout, max_bytes):
    """Synchronous I/O boundary, also replaceable by a deterministic test fake."""
    deadline = time.monotonic() + timeout
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    request = Request(endpoint, headers=headers, method="GET")
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.geturl() != endpoint:
                raise MediaError("redirect")
            if response.status != 200:
                raise MediaError("http_error")
            encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
            if encoding not in {"", "identity"}:
                raise MediaError("invalid_image")
            length = response.headers.get("Content-Length")
            if length is not None:
                if not re.fullmatch(r"[0-9]{1,20}", length.strip()):
                    raise MediaError("http_error")
                if int(length) > max_bytes:
                    raise MediaError("too_large")
            content_type = response.headers.get("Content-Type", "")
            if content_type.split(";", 1)[0].strip().lower() not in set(_MIMES.values()):
                raise MediaError("invalid_image")
            data = bytearray()
            read = getattr(response, "read1", None) or response.read
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MediaError("timeout")
                # urllib's HTTPResponse exposes the connection socket here. A
                # remaining-time timeout prevents slow chunk delivery extending
                # the per-read socket timeout indefinitely.
                socket = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                if socket is not None:
                    socket.settimeout(remaining)
                chunk = read(min(65536, max_bytes + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise MediaError("too_large")
            if length is not None and int(length) != len(data):
                raise MediaError("invalid_image")
            return content_type, bytes(data)
    except MediaError:
        raise
    except HTTPError as error:
        error.close()
        raise MediaError("redirect" if 300 <= error.code < 400 else "http_error") from None
    except (TimeoutError, asyncio.TimeoutError):
        raise MediaError("timeout") from None
    except URLError as error:
        raise MediaError("timeout" if isinstance(error.reason, TimeoutError) else "fetch_failed") from None
    except Exception:
        raise MediaError("fetch_failed") from None


def _magic(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


async def fetch_pixiv_image(*, backend_url, api_key, details: dict, pixiv_id: int,
                            page_index: int = 0, max_bytes: int = 8 * 1024 * 1024,
                            timeout_seconds=15, fetch=None) -> dict:
    """Read one registered current image; never return backend links or metadata.

    ``fetch`` is a synchronous low-level replacement accepting
    (endpoint, headers, timeout, max_bytes) and returning (content_type, bytes).
    Awaiting is bounded. On cancellation the worker can finish its current
    socket operation; the underlying reader also has socket and elapsed limits.
    """
    base = _backend(backend_url)
    if (not isinstance(api_key, str) or not 32 <= len(api_key) <= 4096
            or any(not 33 <= ord(char) <= 126 for char in api_key)
            or not _integer(max_bytes, 1, _MAX_BYTES)
            or type(timeout_seconds) not in {int, float} or not 0 < timeout_seconds <= 60
            or not math.isfinite(timeout_seconds) or fetch is not None and not callable(fetch)):
        raise MediaError("invalid_config")
    path, extension = _select(details, pixiv_id, page_index, max_bytes)
    headers = {"X-API-Key": api_key, "Accept": "image/jpeg,image/png,image/webp,image/gif",
               "Accept-Encoding": "identity", "User-Agent": "MaiLivingWorld"}
    try:
        result = await asyncio.wait_for(asyncio.to_thread(fetch or _fetch, base + path, headers,
                                                         float(timeout_seconds), max_bytes),
                                        timeout=float(timeout_seconds))
        if not isinstance(result, tuple) or len(result) != 2:
            raise MediaError("invalid_image")
        content_type, data = result
        if not isinstance(content_type, str) or not isinstance(data, bytes):
            raise MediaError("invalid_image")
        if len(data) > max_bytes:
            raise MediaError("too_large")
        mime = content_type.split(";", 1)[0].strip().lower()
        if mime != _MIMES[extension] or _magic(data) != mime:
            raise MediaError("invalid_image")
    except MediaError:
        raise
    except (TimeoutError, asyncio.TimeoutError):
        raise MediaError("timeout") from None
    except Exception:
        raise MediaError("fetch_failed") from None
    return {"content": f"已读取 Pixiv 作品 {pixiv_id} 第 {page_index + 1} 张缓存图片；不代表读取其他页。",
            "content_items": [{"type": "image", "data": base64.b64encode(data).decode("ascii"),
                               "mime_type": mime, "name": f"pixiv_{pixiv_id}_p{page_index}{extension}"}]}
