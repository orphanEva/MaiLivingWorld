"""Choose a foreground response without changing facts, permissions or tasks.

The caller supplies an authenticated current owner-private context and already
authorized hint strings. This module consumes the existing dialogue enums; it
does not reinterpret normalized user text, call a model, authorize a recipient,
or persist a reasoning trace. Privacy/source/clarification constraints are never
optional. Only the named, known conversational add-ons may be deferred.
"""
from __future__ import annotations

from collections.abc import Mapping


MARKER = "[MaiLivingWorld:reply-coordination]"
_MODES = {"normal", "listen", "advice", "repair", "light"}
_OPTIONAL = frozenset({"habits", "advice", "followthrough"})
_OPTIONAL_ORDER = {
    "normal": ("habits", "followthrough"),
    "listen": ("habits",),
    "advice": ("advice", "habits"),
    "repair": ("habits",),
    "light": ("habits",),
    "celebrate": ("habits",),
    "progress": ("followthrough", "habits"),
}
_PURPOSE = {
    "normal": "本轮先回应主人正在说的内容和明确问题；相关任务结果可以在自然停顿处简短接上，不把交流变成逐项状态播报。",
    "listen": "本轮以倾听和陪伴为主，先接住眼前的叙述，给对方说完的空间；暂不展开建议、自己的成果或任务回告。",
    "advice": "本轮以协助解决当前问题为主，先回应具体困难，再给少量适合的办法；已有失败反馈应参与取舍，最后由主人决定。",
    "repair": "本轮先处理主人指出的具体分歧；核对哪里理解偏了，必要时简短澄清，回应修正后的意思，避免泛泛道歉或反复解释自己。",
    "light": "本轮以主人希望的轻松交流为主，接住当前话题；不突然展开教学、任务汇报或翻出旧事要求讨论。",
    "celebrate": "本轮先回应主人明确分享的好消息，围绕对方提到的具体事情自然分享喜悦；不立即转成建议、追问或自己的成果展示。",
    "progress": "本轮先回答主人对近期活动的明确询问，只用已核验的活动或任务结果；资料不足就说明不确定，不把排队、尝试或工具返回当成完成及送达。",
}
_BOUNDARIES = (
    "这是本轮表达重点，当前明确问题、必要澄清、事实纠正、身份与相处边界、来源表达限制和共读保护仍优先。"
    "其它已获准资料可以帮助理解，不要求逐项说出；自己的兴趣、情绪余韵和性格不应抢走当前话题。"
    "旧修复只在相关处纠正事实依据，不把倾听或普通聊天变成道歉独白；未经核验的分歧不能冒称已修复。"
    "暂不提及不等于删除记忆、放弃任务、已经回告或改变权限，也不自动安排下一次发送。"
    "不要说出内部模式、筛选过程或字段名。"
)


def _intent(mode, reaction, activity_requested):
    # The two current-turn signals already override older conversation modes in
    # AttentionRuntime. Preserve that contract without guessing from raw text.
    if activity_requested is True:
        return "progress"
    if reaction == "celebrate":
        return "celebrate"
    return mode if isinstance(mode, str) and mode in _MODES else "normal"


def coordinate_reply(*, base: str, hints: Mapping[str, str], mode=None,
                     reaction=None, activity_requested=False, enabled=True) -> dict:
    """Return a composed prompt and bounded, content-free selection metadata.

    ``base`` is retained byte-for-byte. Hint order is retained, too. In enabled
    mode at most two known optional categories are selected; every other key is
    retained, including future keys, so a new boundary cannot silently disappear.
    Empty/non-text entries cannot create an otherwise absent prompt. The caller
    must authenticate context before calling: this function is not an ACL.
    """
    base = base if isinstance(base, str) else ""
    supplied = [(key, value) for key, value in hints.items()
                if isinstance(key, str) and isinstance(value, str) and value.strip()] if isinstance(hints, Mapping) else []
    if enabled is not True:
        return {"prompt": "\n\n".join(value for value in (base, *(value for _, value in supplied)) if value),
                "intent": "legacy", "selected": tuple(key for key, _ in supplied), "deferred": ()}
    intent = _intent(mode, reaction, activity_requested)
    optional = frozenset(_OPTIONAL_ORDER[intent][:2])
    selected = [(key, value) for key, value in supplied if key not in _OPTIONAL or key in optional]
    deferred = tuple(key for key, _ in supplied if key in _OPTIONAL and key not in optional)
    parts = [value for value in (base, *(value for _, value in selected)) if value]
    if parts:
        parts.append(MARKER+"\n"+_PURPOSE[intent]+_BOUNDARIES)
    return {"prompt": "\n\n".join(parts), "intent": intent,
            "selected": tuple(key for key, _ in selected), "deferred": deferred}
