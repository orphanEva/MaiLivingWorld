"""Anonymous, bounded reads of the Bilibili video part actually shared in chat."""
from __future__ import annotations

import asyncio
from io import BytesIO
import json
import re
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from .media_http import MediaFetchError, fetch_public, public_redirect


_SITE = "https://www.bilibili.com/"
_API = "api.bilibili.com"
_CID = re.compile(r"(?:BV[0-9A-Za-z]{10}|av[1-9][0-9]{0,15})")
_URL = re.compile(r"(?<![a-zA-Z0-9_./@%-])(?:https?://)?"
                  r"(?:(?:(?:www|m)\.)?bilibili\.com/video/|b23\.tv/)"
                  r"[A-Za-z0-9/?#&=_%+.~-]{1,2300}", re.I)
_STILLS = ("i0.hdslb.com", "i1.hdslb.com", "i2.hdslb.com")
_BODIES = ("aisubtitle.hdslb.com", "subtitle.bilibili.com", *_STILLS)
_CDN = (".bilivideo.com", ".bilivideo.cn")
_MAX_VIDEO = 32 * 1024 * 1024
_BROKEN = (MediaFetchError, ValueError, TypeError, KeyError, OSError, IndexError, RecursionError)


def _link(value):
    """Drop tracking parameters. A short code is resolved only via b23.tv."""
    if not isinstance(value, str) or not value.isascii() or len(value) > 2400 or "\\" in value:
        return ""
    try:
        p = urlsplit(value if "://" in value else "https://" + value)
        if p.scheme not in {"http", "https"} or p.port not in (None, 443) or p.username is not None:
            return ""
        pages = parse_qs(p.query, keep_blank_values=True, max_num_fields=64).get("p", ["1"])
        if len(pages) != 1 or not re.fullmatch("[1-9][0-9]{0,3}", pages[0]):
            return ""
    except ValueError:
        return ""
    suffix = "?p=" + pages[0] if pages[0] != "1" else ""
    if p.hostname == "b23.tv" and re.fullmatch(r"/[a-zA-Z0-9]{3,20}/?", p.path):
        return "https://b23.tv" + p.path.rstrip("/") + suffix
    if p.hostname in {"bilibili.com", "m.bilibili.com", "www.bilibili.com"}:
        m = re.fullmatch(r"/video/([^/]+)/?", p.path)
        if m and _CID.fullmatch(m[1]):
            return _SITE + "video/" + m[1] + suffix
    return ""


def _first_url(text):
    if not isinstance(text, str):
        return ""
    for match in _URL.finditer(text[:16384].replace("\\/", "/").replace("&amp;", "&")):
        url = _link(match.group().rstrip(".,"))
        if url:
            return url
    return ""


def _card(value, depth=0):
    if depth > 6:
        return ""
    if isinstance(value, str):
        return _first_url(value)
    if isinstance(value, dict):
        # OneBot miniapp cards: meta.detail_1.qqdocurl or meta.news.jumpUrl.
        for key in ("qqdocurl", "jumpUrl", "jump_url", "url", "meta", "detail_1", "news", "data", "text", "content"):
            if key in value and (link := _card(value[key], depth+1)):
                return link
    return ""


def extract_bilibili_link(message):
    """Read plaintext or rich JSON/XML/text segments of this message only."""
    if not isinstance(message, dict):
        return ""
    has_parts = False
    for key in ("raw_message", "message_segment", "message"):
        parts = message.get(key)
        has_parts = has_parts or bool(parts)
        if isinstance(parts, dict):
            parts = parts.get("data") if parts.get("type") == "seglist" else [parts]
        if isinstance(parts, str) and (link := _first_url(parts)):
            return link
        for part in parts[:32] if isinstance(parts, list) else ():
            if isinstance(part, dict) and part.get("type") in {"text", "json", "xml", "share", "app"}:
                data = part.get("data")
                if part["type"] == "json" and isinstance(data, str) and len(data) <= 32768:
                    try:
                        data = json.loads(data)
                    except (ValueError, RecursionError):
                        continue
                if link := _card(data):
                    return link
    info = message.get("message_info")
    config = info.get("additional_config") if isinstance(info, dict) else None
    cards = config.get("platform_card_payloads") if isinstance(config, dict) else None
    for card in cards[:8] if isinstance(cards, list) else ():
        if isinstance(card, dict) and (link := _card(card.get("payload"))):
            return link
    # The host's flattened text can contain quoted or forwarded content.
    return "" if has_parts else _first_url(message.get("processed_plain_text"))


def _clean(text, size):
    return " ".join(text.split())[:size] if isinstance(text, str) else ""


def _secure_url(value):
    if isinstance(value, str) and value.startswith("//"):
        return "https:" + value
    if isinstance(value, str) and value.startswith("http://"):
        return "https://" + value[7:]
    return value


def _empty():
    return {"kind": "bilibili", "status": "unavailable", "basis": "unavailable", "frames": [],
            "title": "", "description": "", "canonical_url": "", "page": 1, "subtitle_excerpt": ""}


def _read(link):
    deadline = time.monotonic() + 17

    def get(url, hosts, limit, per_request=5):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MediaFetchError("timeout")
        return fetch_public(url, allowed_hosts=hosts, max_bytes=limit, timeout=min(per_request, remaining), referer=_SITE)

    def api(path, args):
        raw = json.loads(get("https://" + _API + path + "?" + urlencode(args), (_API,), 768 * 1024))
        if not isinstance(raw, dict) or type(raw.get("code")) is not int or raw["code"] != 0 or not isinstance(raw.get("data"), dict):
            raise MediaFetchError("unavailable")
        return raw["data"]

    info = _empty()
    try:
        url = _link(link)
        if url.startswith("https://b23.tv/"):
            short_page = urlsplit(url).query
            url = _link(public_redirect(url, allowed_hosts=("b23.tv",), timeout=4))
            if url.startswith(_SITE) and short_page:
                url = url.split("?", 1)[0] + "?" + short_page
        if not url.startswith(_SITE):
            return info, None
        vid = urlsplit(url).path.rsplit("/", 1)[-1]
        page = int(parse_qs(urlsplit(url).query).get("p", ["1"])[0])
        args = {"bvid": vid} if vid.startswith("BV") else {"aid": vid[2:]}
        view = api("/x/web-interface/view", args)
        if not isinstance(view.get("bvid"), str) or not re.fullmatch(r"BV[0-9A-Za-z]{10}", view["bvid"]):
            return info, None
        if ("bvid" in args and args["bvid"] != view["bvid"]) or ("aid" in args and str(view.get("aid")) != args["aid"]):
            return info, None
        parts = view.get("pages")
        if not isinstance(parts, list) or not 1 <= page <= len(parts):
            return info, None
        part = parts[page - 1]
        if not isinstance(part, dict) or part.get("page") != page or type(part.get("cid")) is not int or part["cid"] <= 0:
            return info, None
        title = _clean(view.get("title"), 120)
        if not title:
            return info, None
        args = {"bvid": view["bvid"], "cid": part["cid"]}
        canonical = _SITE + "video/" + view["bvid"] + ("?p=" + str(page) if page > 1 else "")
        part_title = _clean(part.get("part"), 100)
        if len(parts) > 1:
            title += " · P" + str(page) + (" " + part_title if part_title else "")
        info.update(title=title, description=_clean(view.get("desc"), 240), canonical_url=canonical,
                    page=page, status="metadata", basis="metadata")
    except _BROKEN:
        return info, None

    # A playlist mismatch, membership gate or preview must not become a full-video read.
    rights = view.get("rights") or {}
    if not isinstance(rights, dict) or any(rights.get(key) for key in ("pay", "ugc_pay", "is_stein_gate")):
        return info, None
    try:
        shots = api("/x/player/videoshot", {**args, "index": 1})
        frames = _storyboard(shots, get)
        if frames:
            info.update(status="frames", basis="storyboard", frames=frames)
    except _BROKEN:
        pass
    video = None
    if not info["frames"]:
        try:
            play = api("/x/player/playurl", {**args, "qn": 16, "fnver": 0, "fnval": 1, "platform": "html5"})
            files = play.get("durl")
            if (not play.get("is_preview") and isinstance(files, list) and len(files) == 1 and isinstance(files[0], dict)
                    and type(files[0].get("size")) is int and 0 < files[0]["size"] <= _MAX_VIDEO):
                video = get(_secure_url(files[0]["url"]), _CDN, _MAX_VIDEO, 8)
        except _BROKEN:
            pass
    try:
        player = api("/x/player/v2", args)
        tracks = (player.get("subtitle") or {}).get("subtitles") if isinstance(player.get("subtitle"), dict) else []
        info["subtitle_excerpt"] = _subtitles(tracks, get)
    except _BROKEN:
        pass
    if info["subtitle_excerpt"] and info["status"] == "metadata":
        info.update(basis="subtitles", status="subtitles")
    return info, video


def _storyboard(shots, get):
    from PIL import Image
    cols, rows, w, h = [shots.get(k) for k in ("img_x_len", "img_y_len", "img_x_size", "img_y_size")]
    imgs, stamps = shots.get("image"), shots.get("index")
    if (any(type(n) is not int or n <= 0 for n in (cols, rows, w, h))
            or w > 640 or h > 480 or cols * rows * w * h > 8_000_000
            or not isinstance(imgs, list) or not 1 <= len(imgs) <= 512
            or not isinstance(stamps, list) or not 2 <= len(stamps) <= min(50000, cols * rows * len(imgs) + 1)):
        return []
    count = len(stamps) - 1
    picks = sorted({min(count-1, count*n//6) for n in (1, 3, 5)})
    frames = []
    for sheet in sorted({n // (cols * rows) for n in picks}):
        raw = get(_secure_url(imgs[sheet]), _STILLS, 2 * 1024 * 1024)
        try:
            opened = Image.open(BytesIO(raw), formats=("JPEG", "PNG", "WEBP"))
        except Image.DecompressionBombError:
            return []
        with opened as image:
            if image.size != (cols*w, rows*h):
                return []
            for index in picks:
                if index // (cols * rows) == sheet:
                    y, x = divmod(index % (cols * rows), cols)
                    with image.crop((x*w, y*h, (x+1)*w, (y+1)*h)).convert("RGB") as crop:
                        encoded = BytesIO()
                        crop.save(encoded, format="JPEG", quality=82)
                        frames.append(encoded.getvalue())
    return frames


def _subtitles(tracks, get):
    if not isinstance(tracks, list):
        return ""
    tracks = [t for t in tracks[:32] if isinstance(t, dict) and isinstance(t.get("subtitle_url"), str) and t["subtitle_url"]]
    if not tracks:
        return ""
    track = next((t for t in tracks if str(t.get("lan")).startswith(("zh", "ai-zh"))), tracks[0])
    raw = json.loads(get(_secure_url(track["subtitle_url"]), _BODIES, 512 * 1024, 3))
    body = raw.get("body") if isinstance(raw, dict) else None
    if not isinstance(body, list):
        return ""
    lines = [_clean(line.get("content"), 100) for line in body[:4000] if isinstance(line, dict)]
    lines = [line for line in lines if line]
    return " … ".join(lines[i] for i in sorted({0, len(lines)//2, len(lines)-1})) if lines else ""


async def read_bilibili_link(link):
    """Return only viewer evidence and its basis. Raw streams and signed URLs stay transient."""
    try:
        info, video = await asyncio.wait_for(asyncio.to_thread(_read, link), timeout=20)
    except TimeoutError:
        return _empty()
    if video:
        from .chat_media import sample_video
        try:
            frames = await sample_video(video)
            if frames:
                info.update(status="frames", basis="sampled_video", frames=frames)
        except (ValueError, OSError, RuntimeError, TimeoutError):
            pass
    return info
