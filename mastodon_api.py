"""Account-scoped Mastodon client: public posts, explicit replies and verified receipts."""
from __future__ import annotations

import http.client
from html.parser import HTMLParser
import json
import os
import re
import threading
import time
import unicodedata
from urllib.parse import urlencode, urlsplit

from .media_http import _connect, _interrupt, _remaining


_ID = re.compile(r"[1-9][0-9]{0,29}")
_HANDLE = re.compile(r"[A-Za-z0-9_]{1,64}(?:@[A-Za-z0-9.-]{1,253})?")
_MAX_BYTES = 768 * 1024
_NO_BOT = re.compile(
    r"#nobots?(?![a-z0-9_])|"
    r"(?:请勿|請勿|不要|[谢謝][绝絕]|拒[绝絕]|禁止|不(?:接受|欢迎|歡迎|希望(?:收到)?))\s*"
    r"(?:让|讓|被)?\s*(?:AI\s*)?(?:机器|機器)人\s*(?:自动|自動)?\s*(?:回[复復覆]|留言|互[动動]|[评評][论論])|"
    r"(?:机器|機器)人\s*[请請]?\s*(?:勿|[别別]|不要)\s*(?:回[复復覆]|留言|互[动動]|[评評][论論])",
    re.I)


class MastodonError(ValueError):
    """Only the error code is exposed. Never include credentials or response bodies."""


def _config():
    origin = os.environ.get("MLW_MASTODON_INSTANCE", "")
    token = os.environ.get("MLW_MASTODON_TOKEN", "")
    if not origin or not token:
        raise MastodonError("mastodon_not_configured")
    try:
        parsed = urlsplit(origin)
        host = parsed.hostname
        if (not host or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", host)
                or "." not in host or ".." in host or parsed.netloc.lower() not in (host, host + ":443")
                or parsed.scheme != "https" or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or len(origin) > 280 or any(ord(ch) <= 32 or ord(ch) > 126 for ch in origin)
                or not re.fullmatch("[!-~]{1,2048}", token)):
            raise ValueError()
    except ValueError:
        raise MastodonError("mastodon_invalid_config") from None
    return host, token


def _response_error(status, writing):
    if status == 401:
        return "mastodon_unauthorized"
    if status == 403:
        return "mastodon_forbidden"
    if status == 429:
        return "mastodon_rate_limited"
    if status in (404, 410):
        return "mastodon_not_found"
    if status in (400, 413, 422):
        return "mastodon_rejected"
    return "mastodon_reply_uncertain" if writing else "mastodon_unavailable"


def _request(config, path, *, fields=None, key=None):
    """One request to the configured origin, no redirects or proxy credentials."""
    host, token = config
    writing = fields is not None
    sent = False
    deadline = time.monotonic() + 6
    headers = {"Host": host, "Authorization": "Bearer " + token, "Accept": "application/json",
               "Accept-Encoding": "identity", "Connection": "close", "User-Agent": "MaiLivingWorld social reader"}
    body = None
    if writing:
        headers.update({"Content-Type": "application/x-www-form-urlencoded", "Idempotency-Key": key})
        body = urlencode(fields).encode("ascii")
    try:
        with _connect(host, deadline) as sock:
            conn = http.client.HTTPConnection(host, 443, timeout=_remaining(deadline))
            conn.sock = sock
            watchdog = threading.Timer(_remaining(deadline), _interrupt, args=(sock,))
            watchdog.daemon = True
            watchdog.start()
            try:
                sent = writing
                conn.request("POST" if writing else "GET", path, body=body, headers=headers)
                with conn.getresponse() as response:
                    if response.status != 200 and not (writing and response.status == 201):
                        raise MastodonError(_response_error(response.status, writing))
                    length = response.getheader("Content-Length")
                    if (response.getheader("Content-Encoding", "identity").lower() != "identity"
                            or length is not None and (not length.isdecimal() or len(length) > 8 or int(length) > _MAX_BYTES)):
                        raise ValueError()
                    raw = response.read(_MAX_BYTES + 1)
                    if len(raw) > _MAX_BYTES or length is not None and len(raw) != int(length):
                        raise ValueError()
                    return json.loads(raw)
            finally:
                watchdog.cancel()
                conn.close()
    except MastodonError:
        raise
    except (OSError, ValueError, RecursionError, http.client.HTTPException):
        raise MastodonError("mastodon_reply_uncertain" if sent else "mastodon_unavailable") from None


def _require_id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise MastodonError("invalid_mastodon_id")
    return value


def _limit(value):
    if type(value) is not int or not 1 <= value <= 10:
        raise MastodonError("invalid_mastodon_limit")


class _Plain(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        elif tag in {"p", "br", "li"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _plain(value, *, for_reply=False):
    if not isinstance(value, str) or len(value) > 65536:
        if for_reply:
            raise MastodonError("mastodon_rejected")
        return ""
    parser = _Plain()
    try:
        parser.feed(value)
        parser.close()
    except (ValueError, AssertionError):
        if for_reply:
            raise MastodonError("mastodon_rejected") from None
        return ""
    text = " ".join("".join(parser.parts).split())
    return text if for_reply else text[:4000]


def _account(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("acct"), str) or not _HANDLE.fullmatch(raw["acct"]):
        raise MastodonError("mastodon_invalid_response")
    try:
        return _require_id(raw.get("id")), raw["acct"]
    except MastodonError:
        raise MastodonError("mastodon_invalid_response") from None


def _post(raw, host, *, for_reply=False):
    if not isinstance(raw, dict):
        raise MastodonError("mastodon_invalid_response")
    if raw.get("visibility") != "public" or raw.get("reblog") is not None or raw.get("filtered"):
        raise MastodonError("mastodon_not_public")
    author, acct = _account(raw.get("account"))
    try:
        ident = _require_id(raw.get("id"))
        parent = None if raw.get("in_reply_to_id") is None else _require_id(raw["in_reply_to_id"])
    except MastodonError:
        raise MastodonError("mastodon_invalid_response") from None
    text = _plain(raw.get("content"), for_reply=for_reply)
    spoiler = _plain(raw.get("spoiler_text", ""), for_reply=for_reply)
    if for_reply:
        # Check the untruncated, freshly fetched status and public profile before the POST.
        # The account biography stays here and is never returned as model input.
        note = _plain(raw["account"].get("note"), for_reply=True)
        if any(_NO_BOT.search(unicodedata.normalize("NFKC", value)) for value in (text, spoiler, note)):
            raise MastodonError("mastodon_rejected")
    # Use a fixed link to the known local representation of the status. No arbitrary URL from the post is followed.
    return {"id": ident, "account_id": author, "acct": acct,
            "url": "https://" + host + "/@" + acct + "/" + ident, "text": text[:4000],
            "in_reply_to_id": parent, "visibility": "public",
            "created_at": raw.get("created_at", "")[:48] if isinstance(raw.get("created_at"), str) else "",
            "spoiler_text": spoiler[:300]}


def _identity(config):
    raw = _request(config, "/api/v1/accounts/verify_credentials")
    account, acct = _account(raw)
    return {"account_id": account, "acct": acct, "instance": "https://" + config[0]}


def mastodon_identity() -> dict:
    return _identity(_config())


def _read(config, ident, *, for_reply=False):
    result = _post(_request(config, "/api/v1/statuses/" + ident), config[0], for_reply=for_reply)
    if result["id"] != ident:
        raise MastodonError("mastodon_invalid_response")
    return result


def mastodon_read(id: str) -> dict:
    ident = _require_id(id)
    return _read(_config(), ident)


def mastodon_search(query: str, limit: int = 5) -> dict:
    _limit(limit)
    if not isinstance(query, str) or not query.strip() or len(query) > 80 or any(ord(ch) < 32 for ch in query):
        raise MastodonError("invalid_mastodon_query")
    config = _config()
    params = urlencode({"q": query.strip(), "type": "statuses", "resolve": "false", "limit": limit})
    result = _request(config, "/api/v2/search?" + params)
    if not isinstance(result, dict) or not isinstance(result.get("statuses"), list):
        raise MastodonError("mastodon_invalid_response")
    items = []
    for raw in result["statuses"][:limit]:
        try:
            items.append(_post(raw, config[0]))
        except MastodonError:
            continue
    return {"items": items, "instance": "https://" + config[0]}


def mastodon_mentions(limit: int = 5) -> dict:
    _limit(limit)
    config = _config()
    me = _identity(config)["account_id"]
    raw = _request(config, "/api/v1/notifications?" + urlencode({"types[]": "mention", "limit": limit}))
    if not isinstance(raw, list):
        raise MastodonError("mastodon_invalid_response")
    items = []
    for notice in raw[:limit]:
        if not isinstance(notice, dict) or notice.get("type") != "mention":
            continue
        try:
            notification_id = _require_id(notice.get("id"))
            status = notice["status"]
            post = _post(status, config[0])
            sender, _ = _account(notice.get("account"))
        except (MastodonError, KeyError):
            continue
        mentions = status.get("mentions")
        addressed = isinstance(mentions, list) and any(isinstance(m, dict) and m.get("id") == me for m in mentions)
        if (sender == post["account_id"] and sender != me
                and (addressed or status.get("in_reply_to_account_id") == me)):
            items.append({**post, "notification_id": notification_id})
    return {"items": items, "instance": "https://" + config[0]}


def mastodon_reply(id: str, text: str, idempotency_key: str) -> dict:
    ident = _require_id(id)
    if (not isinstance(text, str) or not text.strip() or len(text) > 500
            or any(ord(ch) < 32 and ch != "\n" for ch in text)):
        raise MastodonError("invalid_mastodon_text")
    if not isinstance(idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", idempotency_key):
        raise MastodonError("invalid_mastodon_key")
    if "@" in text or "＠" in text:
        raise MastodonError("mastodon_extra_recipients")
    config = _config()
    me = _identity(config)["account_id"]
    target = _read(config, ident, for_reply=True)
    if target["account_id"] == me:
        raise MastodonError("mastodon_self_reply")
    body = "@" + target["acct"] + " " + text.strip()
    if len(body) > 500:
        raise MastodonError("invalid_mastodon_text")
    fields = {"status": body, "in_reply_to_id": ident, "visibility": "public"}
    reply = _request(config, "/api/v1/statuses", fields=fields, key=idempotency_key)
    try:
        result = _post(reply, config[0])
        if result["account_id"] != me or result["in_reply_to_id"] != ident or result["id"] == ident:
            raise ValueError()
    except ValueError:
        raise MastodonError("mastodon_reply_uncertain") from None
    return {k: result[k] for k in ("id", "url", "account_id", "in_reply_to_id", "visibility")}
