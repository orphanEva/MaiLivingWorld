"""Read bounded QQ message media. No local paths, forwarded nodes or arbitrary URLs."""
from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import dataclass, field
import io
import math
from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import urlsplit

from .media_http import fetch_public
from .owner import extract_sender, normalize_qq

IMAGE_LIMIT = 8 * 1024 * 1024
VIDEO_LIMIT = 32 * 1024 * 1024
_JPEG_LIMIT = 512 * 1024
_QQ_HOSTS = (".qpic.cn", "multimedia.nt.qq.com.cn", "multimedia.nt.qq.com", "grouptalk.c2c.qq.com",
             ".groupfile.qq.com", ".video.qq.com", ".ftn.qq.com", "gxh.vip.qq.com")
_VIDEO_MARKER = re.compile(r"\[视频\](?: 文件: [^\r\n]{1,1024}(?:，大小: \d{1,16})?| 大小: \d{1,16})?")


class ChatMediaError(ValueError):
    """Short failure code, excluding the message, URL and media body."""


@dataclass(frozen=True)
class Attachment:
    kind: str
    source: str
    value: str = field(repr=False)
    # For a normalized NapCat placeholder only. Never accept a quoted/forwarded message ID.
    sender: str = field(default="", repr=False)
    group: str = field(default="", repr=False)
    account: str = field(default="", repr=False)
    ordinal: int = 0


def _cdn_url(value):
    if (not isinstance(value, str) or len(value) > 8192 or not value.isascii()
            or "\\" in value or any(ord(c) <= 32 or ord(c) == 127 for c in value)):
        return ""
    try:
        parts = urlsplit(value)
        if (parts.scheme not in {"https", "http"} or parts.username is not None or parts.password is not None
                or parts.port not in (None, 443 if parts.scheme == "https" else 80)
                or not parts.hostname or not parts.path.startswith("/")
                or not any(parts.hostname == host or host.startswith(".") and parts.hostname.endswith(host)
                           for host in _QQ_HOSTS)):
            return ""
    except ValueError:
        return ""
    # Some adapters distribute HTTP links. Contact the verified CDN using TLS only.
    return parts._replace(scheme="https", netloc=parts.hostname, fragment="").geturl()


def _direct(segment, kind):
    limit = IMAGE_LIMIT if kind == "image" else VIDEO_LIMIT
    data = segment.get("data")
    fields = data if isinstance(data, dict) else {}
    size = str(fields.get("file_size") or fields.get("size") or "")
    if size.isascii() and size.isdecimal() and (len(size) > 12 or int(size) > limit):
        return None
    encoded = segment.get("binary_data_base64") or fields.get("base64")
    file = fields.get("file")
    if not encoded and isinstance(file, str) and file.startswith("base64://"):
        encoded = file[9:] if len(file) <= 9 + 4*((limit+2)//3) else ""
    if isinstance(encoded, str) and 0 < len(encoded) <= 4*((limit+2)//3):
        return Attachment(kind, "base64", encoded)
    url = _cdn_url(fields.get("url")) or _cdn_url(file)
    return Attachment(kind, "cdn", url) if url else None


def extract_attachments(message) -> list[Attachment]:
    sender = extract_sender(message)
    parts = message.get("raw_message") if sender else None
    if not sender or not sender.message_id or not isinstance(parts, list) or len(parts) > 64:
        return []
    account = str(message.get("account_id") or "")
    if account == sender.user_id:
        return []
    result = []
    seen = {"image": 0, "video": 0}
    for segment in parts:
        if not isinstance(segment, dict):
            continue
        kind = segment.get("type")
        if kind == "text":
            value = segment.get("data")
            if isinstance(value, dict):
                value = value.get("text")
            if not isinstance(value, str) or len(value) > 1100:
                continue
            if value in ("[image]", "[emoji]"):
                kind = "image"
            elif _VIDEO_MARKER.fullmatch(value):
                kind = "video"
            else:
                continue
            direct = None
        elif kind in ("image", "emoji", "video"):
            kind = "image" if kind == "emoji" else kind
            direct = _direct(segment, kind)
        elif kind == "file" and isinstance(segment.get("data"), dict):
            payload = segment["data"]
            mime = str(payload.get("mime_type") or "").partition(";")[0].lower()
            if mime in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
                kind = "image"
            elif mime in {"video/mp4", "video/quicktime", "video/webm", "video/x-matroska", "video/x-msvideo"}:
                kind = "video"
            else:
                continue
            direct = _direct(segment, kind)
            if not direct:
                continue
        else:
            continue
        if direct:
            result.append(direct)
        elif re.fullmatch(r"-?[1-9][0-9]{0,17}", sender.message_id):
            result.append(Attachment(kind, "adapter", sender.message_id, sender.user_id,
                                     sender.target_id if sender.kind == "group" else "", account, seen[kind]))
        if segment.get("type") != "file":
            seen[kind] += 1
        if len(result) == 3:
            break
    return result


async def _resolve(item, api_call):
    if not callable(api_call):
        raise ChatMediaError("adapter_unavailable")
    mid = int(item.value)
    # The typed get_msg helper rejects negative IDs. NapCat's action forwards
    # signed OneBot IDs unchanged. Both branches request only this inbound message.
    if mid > 0:
        request = api_call("adapter.napcat.message.get_msg", message_id=mid)
    else:
        request = api_call("adapter.napcat.action.call_data", action_name="get_msg", params={"message_id": mid})
    reply = await asyncio.wait_for(request, 4)
    detail = reply.get("result") if isinstance(reply, dict) and reply.get("success") is True else None
    if not isinstance(detail, dict) or str(detail.get("message_id")) != item.value:
        raise ChatMediaError("attachment_unavailable")
    sender = detail.get("sender")
    if (not isinstance(sender, dict) or str(sender.get("user_id")) != item.sender
            or str(detail.get("user_id", item.sender)) != item.sender
            or detail.get("message_type") != ("group" if item.group else "private")
            or str(detail.get("group_id") or "") != item.group
            or item.account and str(detail.get("self_id") or "") != item.account):
        raise ChatMediaError("attachment_unavailable")
    parts = detail.get("message")
    if isinstance(parts, list) and len(parts) <= 64:
        candidates = [p for p in parts if isinstance(p, dict) and p.get("type") == item.kind]
        if len(candidates) > item.ordinal:
            direct = _direct(candidates[item.ordinal], item.kind)
            if direct:
                return direct
    raise ChatMediaError("attachment_unavailable")


def _jpeg(data):
    from PIL import Image, ImageOps

    if len(data) > IMAGE_LIMIT:
        raise ChatMediaError("image_too_large")
    try:
        with Image.open(io.BytesIO(data), formats=("JPEG", "PNG", "WEBP", "GIF")) as source:
            if source.width * source.height > 20_000_000 or max(source.size) > 16384:
                raise ChatMediaError("image_too_large")
            source.thumbnail((768, 768))
            with ImageOps.exif_transpose(source).convert("RGB") as frame:
                out = io.BytesIO()
                frame.save(out, "JPEG", quality=82)
                result = out.getvalue()
    except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ChatMediaError("invalid_image") from exc
    if len(result) > _JPEG_LIMIT:
        raise ChatMediaError("image_too_large")
    return result


def _video_format(data):
    if not isinstance(data, bytes) or not 12 <= len(data) <= VIDEO_LIMIT:
        raise ChatMediaError("invalid_video_size")
    if data[4:8] == b"ftyp":
        return "mov"
    if data.startswith(b"\x1aE\xdf\xa3"):
        return "matroska"
    if data.startswith(b"RIFF") and data[8:12] == b"AVI ":
        return "avi"
    if data.startswith(b"FLV\x01"):
        return "flv"
    raise ChatMediaError("unsupported_video")


def _ffmpeg():
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        from imageio_ffmpeg import get_ffmpeg_exe
        return get_ffmpeg_exe()
    except (ImportError, RuntimeError, OSError) as exc:
        raise ChatMediaError("video_decoder_unavailable") from exc


async def _ffmpeg_run(args, *, probe=False):
    """Limit diagnostics even for containers with hostile metadata. Always reap the decoder."""
    proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE if probe else asyncio.subprocess.DEVNULL)
    try:
        if probe:
            diagnostics = bytearray()
            while len(diagnostics) < 32768:
                block = await proc.stderr.read(4096)
                if not block:
                    break
                diagnostics.extend(block)
            match = re.search(rb"(?m)^  Duration: (\d{2,4}):(\d\d):(\d\d\.\d+),", diagnostics)
            if not match:
                return 0.0
            hours, minutes, seconds = (float(n) for n in match.groups())
            return hours*3600 + minutes*60 + seconds
        return await proc.wait()
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        # Drain a still-open pipe before waiting on transports (also on cancellation).
        await proc.communicate()


async def sample_video(data: bytes) -> list[bytes]:
    """Render up to three distributed frames from a local, bounded byte buffer."""
    fmt = _video_format(data)
    try:
        binary = _ffmpeg()
        with tempfile.TemporaryDirectory(prefix="mlw-frames-") as directory:
            source, output = Path(directory)/"clip", Path(directory)/"frame.jpg"
            source.write_bytes(data)
            base = [binary, "-nostdin", "-hide_banner", "-max_alloc", "67108864", "-protocol_whitelist", "file",
                    "-probesize", "4194304", "-analyzeduration", "2000000", "-threads", "1", "-max_pixels", "9437184",
                    "-max_streams", "16", "-f", fmt]
            if fmt == "mov":
                base += ["-enable_drefs", "0", "-use_absolute_path", "0"]
            async with asyncio.timeout(15):
                duration = await _ffmpeg_run([*base, "-i", str(source)], probe=True)
                positions = [0.0]
                if math.isfinite(duration) and .6 <= duration <= 86400:
                    positions += [duration*.5, duration*.9]
                frames = []
                for position in positions:
                    output.unlink(missing_ok=True)
                    status = await _ffmpeg_run([*base, "-loglevel", "error", "-ss", f"{position:.3f}",
                        "-i", str(source), "-map", "0:V:0", "-an", "-sn", "-dn", "-frames:v", "1",
                        "-filter_threads", "1", "-vf", "scale=640:640:force_original_aspect_ratio=decrease",
                        "-threads", "1", "-c:v", "mjpeg", "-q:v", "4", "-fs", str(_JPEG_LIMIT), "-f", "image2",
                        "-update", "1", "-y", str(output)])
                    if status == 0 and output.is_file() and 0 < output.stat().st_size <= _JPEG_LIMIT:
                        image = output.read_bytes()
                        if image.startswith(b"\xff\xd8") and image.endswith(b"\xff\xd9"):
                            frames.append(image)
                if frames:
                    return frames
    except (TimeoutError, OSError) as exc:
        raise ChatMediaError("video_decoder_failed") from exc
    raise ChatMediaError("video_decoder_failed")


async def load_attachment(attachment: Attachment, *, api_call=None) -> list[bytes]:
    item = attachment
    if item.source == "adapter":
        item = await _resolve(item, api_call)
    limit = IMAGE_LIMIT if item.kind == "image" else VIDEO_LIMIT
    if item.source == "base64" and 0 < len(item.value) <= 4*((limit+2)//3):
        try:
            data = await asyncio.to_thread(base64.b64decode, item.value, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ChatMediaError("invalid_payload") from exc
    elif item.source == "cdn" and _cdn_url(item.value):
        data = await asyncio.to_thread(fetch_public, item.value, allowed_hosts=_QQ_HOSTS, max_bytes=limit)
    else:
        raise ChatMediaError("attachment_unavailable")
    if item.kind == "image":
        return [await asyncio.to_thread(_jpeg, data)]
    return await sample_video(data)


def quoted_media_target(message) -> tuple[str, str] | None:
    """Return the Host's direct reply target for a scoped local lookup only."""
    sender = extract_sender(message)
    if not sender or not sender.message_id:
        return None
    parts = message.get("raw_message")
    if not isinstance(parts, list) or len(parts) > 64:
        return None
    replies = [part for part in parts if isinstance(part, dict) and part.get("type") == "reply"]
    if len(replies) != 1 or not isinstance(replies[0].get("data"), dict):
        return None
    data = replies[0]["data"]
    target = data.get("target_message_id")
    if not isinstance(target, str) or target == sender.message_id or not re.fullmatch(r"-?[1-9][0-9]{0,17}", target):
        return None
    return target, normalize_qq(data.get("target_message_sender_id"))


def recalled_media_target(message):
    """Validate a delivered NapCat recall notice and return the original media message's identity."""
    if not isinstance(message, dict) or message.get("is_notify") is not True:
        return None
    actor = extract_sender(message)
    if not actor or not actor.stream_id:
        return None
    extras = message["message_info"].get("additional_config")
    payload = extras.get("napcat_notice_payload") if isinstance(extras, dict) else None
    if not isinstance(payload, dict) or payload.get("post_type") != "notice":
        return None
    expected = "group_recall" if actor.kind == "group" else "friend_recall"
    if payload.get("notice_type") != expected or extras.get("napcat_notice_type") != expected:
        return None
    account = normalize_qq(message.get("account_id"))
    author = normalize_qq(payload.get("user_id"))
    mid = str(payload.get("message_id") or "")
    if (not account or account != normalize_qq(extras.get("self_id")) or account != normalize_qq(payload.get("self_id"))
            or not author or not re.fullmatch(r"-?[1-9][0-9]{0,17}", mid)):
        return None
    if actor.kind == "group":
        if (normalize_qq(payload.get("group_id")) != actor.target_id
                or normalize_qq(payload.get("operator_id") or author) != actor.user_id):
            return None
    elif actor.user_id != author or payload.get("group_id") not in (None, "", 0, "0"):
        return None
    return actor.stream_id, account, actor.kind + ":" + actor.target_id, author, mid
