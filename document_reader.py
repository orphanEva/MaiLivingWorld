"""Bounded PDF reading and provenance-carrying visual input.

Only the gateway supplies cached paths. Parsing runs in a short-lived child;
this is a resource boundary, not a sandbox against native-library exploits.
Linux uses directory descriptors with O_NOFOLLOW. Windows also verifies the
opened handle's final path and rejects reparse components.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

PDF_TOOL = "living_read_pdf_page"
PDF_SCHEMA = {"type": "object", "properties": {
    "jm_id": {"type": "string", "pattern": "^[0-9]{1,18}$"},
    "page_index": {"type": "integer", "minimum": 0, "maximum": 1499, "default": 0}},
    "required": ["jm_id"], "additionalProperties": False}
PDF_DESCRIPTION = "读取已缓存作品 PDF 的一页文字和图片，不下载；页码从 0 开始。"
_ERRORS = {"invalid_reading_request", "invalid_reader_config", "pdf_not_cached", "pdf_path_denied",
    "pdf_too_large", "pdf_changed", "invalid_pdf", "encrypted_pdf", "pdf_page_limit", "page_out_of_range",
    "page_image_too_large", "reader_unavailable", "reader_timeout", "reader_failed", "invalid_reading_result"}
MAX_RESULT_BYTES = 230 * 1024


class DocumentError(RuntimeError):
    def __init__(self, code):
        self.code = code if code in _ERRORS else "reader_failed"
        super().__init__(self.code)


@dataclass(frozen=True)
class PDFLimits:
    max_file_bytes: int = 64 * 1024 * 1024
    max_pages: int = 1500
    max_pixels: int = 2_000_000
    max_image_bytes: int = 96 * 1024
    max_text_chars: int = 6000
    timeout_seconds: float = 20

    def __post_init__(self):
        for value, maximum in ((self.max_file_bytes, 64*1024*1024), (self.max_pages, 1500),
                (self.max_pixels, 2_000_000), (self.max_image_bytes, 96*1024), (self.max_text_chars, 6000)):
            if type(value) is not int or not 1 <= value <= maximum:
                raise DocumentError("invalid_reader_config")
        if (type(self.timeout_seconds) not in (int, float) or not math.isfinite(self.timeout_seconds)
                or not 0 < self.timeout_seconds <= 30):
            raise DocumentError("invalid_reader_config")


def pdf_arguments(arguments):
    if not isinstance(arguments, dict) or set(arguments)-{"jm_id", "page_index"}:
        raise DocumentError("invalid_reading_request")
    ident, index = arguments.get("jm_id"), arguments.get("page_index", 0)
    if (not isinstance(ident, str) or not re.fullmatch(r"[0-9]{1,18}", ident) or int(ident) <= 0
            or type(index) is not int or not 0 <= index < 1500):
        raise DocumentError("invalid_reading_request")
    return str(int(ident)), index


def _lexical_paths(root, filename):
    if (not isinstance(root, str) or not isinstance(filename, str) or not root or not filename
            or len(root) > 4096 or len(filename) > 4096 or "\0" in root+filename):
        raise DocumentError("pdf_path_denied")
    base, candidate = Path(root), Path(filename)
    if not base.is_absolute() or not candidate.is_absolute() or ".." in base.parts+candidate.parts:
        raise DocumentError("pdf_path_denied")
    try:
        relative = candidate.relative_to(base)
    except ValueError:
        raise DocumentError("pdf_path_denied") from None
    if not relative.parts or candidate.suffix.lower() != ".pdf":
        raise DocumentError("pdf_path_denied")
    return base, candidate, relative


def _open_checked(root, filename):
    base, candidate, relative = _lexical_paths(root, filename)
    if os.name == "posix":
        # Anchor every directory, including the configured root, without links.
        directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in base.parts[1:]+relative.parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            return os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        finally:
            os.close(directory)
    if os.name != "nt":
        raise DocumentError("reader_unavailable")
    for component in (candidate, *candidate.parents):
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise DocumentError("pdf_path_denied")
    fd = os.open(candidate, os.O_RDONLY | os.O_BINARY)
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes
        get_path = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
        get_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        get_path.restype = wintypes.DWORD
        buf = ctypes.create_unicode_buffer(32768)
        size = get_path(msvcrt.get_osfhandle(fd), buf, len(buf), 0)
        final = buf.value
        if final.startswith("\\\\?\\UNC\\"):
            final = "\\\\" + final[8:]
        elif final.startswith("\\\\?\\"):
            final = final[4:]
        if not size or size >= len(buf) or os.path.normcase(final) != os.path.normcase(str(candidate)):
            raise DocumentError("pdf_path_denied")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _snapshot(root, filename, limits):
    fd = None
    try:
        fd = _open_checked(root, filename)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise DocumentError("pdf_path_denied")
        if not 1 <= before.st_size <= limits.max_file_bytes:
            raise DocumentError("pdf_too_large")
        chunks, size = [], 0
        while True:
            chunk = os.read(fd, min(65536, limits.max_file_bytes+1-size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > limits.max_file_bytes:
                raise DocumentError("pdf_too_large")
        after = os.fstat(fd)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if size != before.st_size or any(getattr(before, key) != getattr(after, key) for key in fields):
            raise DocumentError("pdf_changed")
        data = b"".join(chunks)
        if not data.startswith(b"%PDF-"):
            raise DocumentError("invalid_pdf")
        return data
    except DocumentError:
        raise
    except (OSError, ValueError):
        raise DocumentError("pdf_path_denied") from None
    finally:
        if fd is not None:
            os.close(fd)


def _render(data, ident, index, limits):
    try:
        import pymupdf
    except ImportError:
        raise DocumentError("reader_unavailable") from None
    try:
        pymupdf.TOOLS.mupdf_display_errors(False)
        pymupdf.TOOLS.mupdf_display_warnings(False)
        with pymupdf.open(stream=data, filetype="pdf") as document:
            if document.needs_pass or document.is_encrypted:
                raise DocumentError("encrypted_pdf")
            count = document.page_count
            if not 1 <= count <= limits.max_pages:
                raise DocumentError("pdf_page_limit")
            if not 0 <= index < count:
                raise DocumentError("page_out_of_range")
            page = document.load_page(index)
            width, height = float(page.rect.width), float(page.rect.height)
            if not all(math.isfinite(v) and 1 <= v <= 100000 for v in (width, height)):
                raise DocumentError("invalid_pdf")
            raw_text = page.get_text("text", sort=True)
            text = raw_text[:limits.max_text_chars]
            scale = min(2.0, 1600/max(width, height), math.sqrt(limits.max_pixels/(width*height))*.99)
            image = None
            for _ in range(7):
                pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csRGB,
                                        alpha=False, annots=False)
                if pixmap.width*pixmap.height > limits.max_pixels:
                    raise DocumentError("page_image_too_large")
                for quality in (75, 55, 35):
                    candidate = pixmap.tobytes("jpeg", jpg_quality=quality)
                    if len(candidate) <= limits.max_image_bytes:
                        image = candidate
                        break
                if image is not None:
                    break
                scale *= .7
            if image is None:
                raise DocumentError("page_image_too_large")
            return {"provider": "cap", "kind": "pdf_page", "jm_id": ident,
                "source_id": "cap:album:"+ident, "origin_key": "album:"+ident,
                "source_version": hashlib.sha256(data).hexdigest(), "page_index": index, "total_pages": count,
                "text": text, "text_truncated": len(raw_text) > len(text),
                "image_base64": base64.b64encode(image).decode("ascii"), "mime_type": "image/jpeg",
                "width": pixmap.width, "height": pixmap.height}
    except DocumentError:
        raise
    except Exception:
        raise DocumentError("invalid_pdf") from None


def _worker(request):
    limits = PDFLimits(**request["limits"])
    ident, index = pdf_arguments({"jm_id": request["jm_id"], "page_index": request["page_index"]})
    if os.name == "posix":
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (max(1, math.ceil(limits.timeout_seconds)),)*2)
        resource.setrlimit(resource.RLIMIT_AS, (768*1024*1024,)*2)
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_RESULT_BYTES+1024,)*2)
    return _render(_snapshot(request["root"], request["path"], limits), ident, index, limits)


async def _run_worker(request, *, process_factory=None):
    try:
        spec = importlib.util.find_spec("pymupdf")
        if spec is None or not spec.origin:
            raise DocumentError("reader_unavailable")
        env = {key: value for key, value in os.environ.items()
               if key.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "HOME", "LANG"}}
        env.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1",
                   PYTHONPATH=str(Path(spec.origin).resolve().parent.parent))
        request_bytes = json.dumps(request, ensure_ascii=True).encode("ascii")
        if len(request_bytes) > 16384:
            raise DocumentError("invalid_reading_request")
        process = None
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as request_file:
            try:
                request_file.write(request_bytes)
                request_file.seek(0)
                process = (process_factory or subprocess.Popen)(
                    [sys.executable, "-u", str(Path(__file__).resolve()), "--pdf-worker"],
                    stdin=request_file, stdout=output, stderr=subprocess.DEVNULL, shell=False,
                    env=env, cwd=str(Path(__file__).resolve().parent),
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                deadline = time.monotonic()+request["limits"]["timeout_seconds"]
                while process.poll() is None:
                    if time.monotonic() >= deadline:
                        raise DocumentError("reader_timeout")
                    await asyncio.sleep(.02)
                if process.returncode != 0:
                    raise DocumentError("reader_failed")
                output.seek(0)
                raw = output.read(MAX_RESULT_BYTES+1)
                if len(raw) > MAX_RESULT_BYTES:
                    raise DocumentError("invalid_reading_result")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise DocumentError("invalid_reading_result")
                if "error" in result:
                    raise DocumentError(result["error"])
                return result
            finally:
                if process is not None:
                    if process.poll() is None:
                        process.kill()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
    except (DocumentError, asyncio.CancelledError):
        raise
    except Exception:
        raise DocumentError("reader_failed") from None


async def read_cached_pdf_page(cached: dict, *, jm_id: str, page_index: int = 0,
                               root: str = "/var/lib/cap/downloads", limits: PDFLimits | None = None,
                               process_factory=None) -> dict:
    ident, index = pdf_arguments({"jm_id": jm_id, "page_index": page_index})
    limits = limits or PDFLimits()
    if not isinstance(limits, PDFLimits):
        raise DocumentError("invalid_reader_config")
    if (not isinstance(cached, dict) or cached.get("jm_id") != ident or type(cached.get("cached")) is not bool):
        raise DocumentError("invalid_reading_result")
    if cached["cached"] is False:
        raise DocumentError("pdf_not_cached")
    if not isinstance(cached.get("pdf"), dict):
        raise DocumentError("invalid_reading_result")
    filename = cached["pdf"].get("file_path")
    _lexical_paths(root, filename)
    result = await _run_worker({"root": root, "path": filename, "jm_id": ident, "page_index": index,
                               "limits": asdict(limits)}, process_factory=process_factory)
    normalize_reading_page("cap", result, resource_id=ident, page_index=index)
    return result


def normalize_reading_page(provider, payload, *, resource_id, page_index=0, version=None,
                           total_units=None, max_image_bytes=8*1024*1024):
    """Create common visual input without provider paths, URLs or metadata.

    Pixiv caller supplies verified current-version metadata from get_library_work;
    payload is the existing fetch_pixiv_image result, not arbitrary model data.
    Producing this input does not claim that visual comprehension has occurred.
    """
    if (not isinstance(payload, dict) or type(page_index) is not int or page_index < 0
            or type(max_image_bytes) is not int or not 1 <= max_image_bytes <= 32*1024*1024):
        raise DocumentError("invalid_reading_result")
    text, truncated = "", False
    if provider == "cap":
        ident, _ = pdf_arguments({"jm_id": resource_id, "page_index": page_index})
        version = payload.get("source_version")
        total_units = payload.get("total_pages")
        if (payload.get("provider") != "cap" or payload.get("kind") != "pdf_page"
                or payload.get("jm_id") != ident or payload.get("source_id") != "cap:album:"+ident
                or payload.get("origin_key") != "album:"+ident or type(payload.get("page_index")) is not int
                or payload["page_index"] != page_index or not isinstance(version, str)
                or not re.fullmatch(r"[0-9a-f]{64}", version)
                or type(total_units) is not int or not 1 <= total_units <= 1500
                or type(payload.get("width")) is not int or type(payload.get("height")) is not int
                or not 1 <= payload["width"] <= 2000 or not 1 <= payload["height"] <= 2000
                or payload["width"]*payload["height"] > 2_000_000):
            raise DocumentError("invalid_reading_result")
        text, truncated = payload.get("text"), payload.get("text_truncated")
        if not isinstance(text, str) or len(text) > 6000 or type(truncated) is not bool:
            raise DocumentError("invalid_reading_result")
        encoded, mime = payload.get("image_base64"), payload.get("mime_type")
        limit = min(max_image_bytes, 96*1024)
        source_id, origin_key, kind = "cap:album:"+ident, "album:"+ident, "pdf"
    elif provider == "pixiv":
        if (type(resource_id) is not int or resource_id <= 0 or type(version) is not int or version <= 0
                or type(total_units) is not int or not 1 <= total_units <= 10000):
            raise DocumentError("invalid_reading_result")
        items = payload.get("content_items")
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict) or items[0].get("type") != "image":
            raise DocumentError("invalid_reading_result")
        encoded, mime = items[0].get("data"), items[0].get("mime_type")
        ident, version, limit = str(resource_id), str(version), max_image_bytes
        source_id, origin_key, kind = "pixiv:illust:"+ident, "work:illust:"+ident, "illust"
    else:
        raise DocumentError("invalid_reading_result")
    if page_index >= total_units or not isinstance(encoded, str) or len(encoded) > 4*((limit+2)//3):
        raise DocumentError("invalid_reading_result")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise DocumentError("invalid_reading_result") from None
    magic = ("image/jpeg" if data.startswith(b"\xff\xd8\xff") else
             "image/png" if data.startswith(b"\x89PNG\r\n\x1a\n") else
             "image/gif" if data.startswith((b"GIF87a", b"GIF89a")) else
             "image/webp" if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP" else "")
    if not data or len(data) > limit or mime != magic or not magic or provider == "cap" and mime != "image/jpeg":
        raise DocumentError("invalid_reading_result")
    return {"provider": provider, "resource_type": kind, "resource_id": ident, "version": version,
        "index": page_index, "total_units": total_units, "source_id": source_id, "origin_key": origin_key,
        "unit_id": source_id+":"+version+":page:"+str(page_index), "excerpt": text,
        "text_truncated": truncated, "image_base64": encoded, "mime_type": mime,
        "image_sha256": hashlib.sha256(data).hexdigest()}


if __name__ == "__main__":
    try:
        if sys.argv[1:] != ["--pdf-worker"]:
            raise DocumentError("invalid_reading_request")
        raw = sys.stdin.buffer.read(16385)
        if len(raw) > 16384:
            raise DocumentError("invalid_reading_request")
        result = _worker(json.loads(raw))
    except DocumentError as exc:
        result = {"error": exc.code}
    except BaseException:
        result = {"error": "reader_failed"}
    sys.stdout.write(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
