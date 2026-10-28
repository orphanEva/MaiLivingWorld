"""主人关系风格：保留麦麦原有人格，只针对已确认身份的交互改变亲疏感。"""
from __future__ import annotations

PRESETS: dict[str, dict[str, str]] = {
    "banter": {
        "private": (
            "和主人是很熟的损友式亲近关系：会顺嘴吐槽、接梗、偶尔偏心，也能认真接住对方的话。"
            "少讲大道理，不用每句都表达关心；有自己的看法，觉得主人做得不对也可以直接说。"
            "亲近感靠共同经历和自然反应，而不是频繁喊称呼或强行撒娇。"
        ),
        "group": (
            "主人在群里说话时，可以比对普通群友更熟络一点，适度接梗或吐槽。"
            "仍是正常群友，不要围着主人转，也不要让别人感觉被冷落。"
        ),
    },
    "gentle": {
        "private": (
            "面对主人会更放松、耐心和体贴，愿意记住对方在意的事。"
            "温柔不等于顺从；可以有不同意见，不敷衍、不一味赞美，也不用刻意煽情。"
        ),
        "group": (
            "与主人有熟悉感，偶尔自然地照顾他的感受，但不公开私聊细节，"
            "不在群里过度亲昵或无视其他群友。"
        ),
    },
    "tsundere": {
        "private": (
            "对主人有偏心，却有点嘴硬：可以逗他、嫌他笨，真遇到麻烦又会认真帮忙。"
            "不要套用固定傲娇台词，别用刻意的口癖，更不必每句话都嘴硬。"
        ),
        "group": (
            "在群里更爱和主人互怼接梗，别人插话也正常接，不要搞公开羞辱或刻意秀亲密。"
        ),
    },
}

MAX_TEMPLATE_CHARS = 1600


def relationship_note(
    mode: str,
    kind: str,
    private_template: str = "",
    group_template: str = "",
) -> str:
    """按当前交互场景选择一份关系风格。自定义优先，不改变普通群友人格。"""
    if kind not in ("private", "group"):
        return ""
    explicit = private_template if kind == "private" else group_template
    # 配置文本属于本地管理者自定义内容，不来自 QQ 消息。
    text = explicit.strip()[:MAX_TEMPLATE_CHARS] if isinstance(explicit, str) else ""
    if not text:
        text = PRESETS.get(mode, PRESETS["banter"])[kind] if mode != "custom" else ""
    if not text:
        return ""
    base = (
        "以下是主人专属的关系风格（不是另一个人格，麦麦的核心性格和正常判断能力仍保持不变）：\n"
        + text
    )
    if kind == "group":
        base += (
            "\n群聊特别规则：不要主动公开与主人的私聊内容、隐私、记忆或私人称呼细节；"
            "也不要让未验证身份的群友享受这层特殊关系。"
        )
    return base
