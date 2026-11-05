"""Verify an actual outbound owner-private receipt without storing message bodies."""
from __future__ import annotations

import asyncio
import hashlib

from .mcp_runtime import digest
from .owner import normalize_qq


async def verify_owner_delivery(bot, kwargs):
    def identity():
        owner = normalize_qq(bot.config.owner.qq) if bot.config.owner.enabled else ""
        state = getattr(bot, "_self_state", None)
        return owner if (bot.config.plugin.enabled and owner and state
                         and state.state()["owner_id"] == owner) else ""

    owner = identity()
    if not owner or not isinstance(kwargs, dict) or kwargs.get("sent") is not True:
        return None
    message = kwargs.get("message")
    if not isinstance(message, dict) or message.get("platform") != "qq":
        return None
    info = message.get("message_info")
    if not isinstance(info, dict) or info.get("group_info"):
        return None
    user = info.get("user_info")
    account = normalize_qq(message.get("account_id"))
    if not account or account == owner or not isinstance(user, dict) or normalize_qq(user.get("user_id")) != account:
        return None
    sid, mid, text = message.get("session_id"), message.get("message_id"), message.get("processed_plain_text")
    if (not isinstance(sid, str) or not 1 <= len(sid) <= 200 or not sid.strip()
            or not isinstance(mid, str) or not 1 <= len(mid) <= 200 or not mid.strip()
            or not isinstance(text, str) or not 1 <= len(text) <= 4000):
        return None
    # An embedded quote/forward/media caption is not an independent promise.
    parts = message.get("raw_message")
    if ((isinstance(parts, list) and any(not isinstance(p, dict) or p.get("type") != "text" for p in parts))
            or any(message.get(k) for k in ("is_picture", "is_voice", "is_emoji"))):
        return None
    signature = digest([bot.config.owner.model_dump(), bot.config.plugin.enabled,
                        getattr(bot, "_mcp_generation", 0)])
    try:
        stream = await asyncio.wait_for(bot.ctx.chat.get_stream_by_user_id(owner, platform="qq"), 3)
    except asyncio.CancelledError:
        raise
    except Exception:
        return None
    found = (str(stream.get("session_id") or stream.get("stream_id") or stream.get("id") or "") if isinstance(stream, dict)
             else str(getattr(stream, "session_id", None) or getattr(stream, "stream_id", None) or getattr(stream, "id", None) or ""))
    if (identity() != owner or found != sid or signature != digest([
            bot.config.owner.model_dump(), bot.config.plugin.enabled, getattr(bot, "_mcp_generation", 0)])):
        return None
    return {"owner": owner, "sid": sid, "mid": mid, "text": text,
            "receipt_id": digest([sid, mid]), "text_digest": hashlib.sha256(text.encode()).hexdigest()}
