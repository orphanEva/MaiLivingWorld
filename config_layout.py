"""Organize MaiBot's generated configuration form without changing its stored keys."""
from __future__ import annotations


_TABS = (
    ("quick", "快速开始", ("owner",)),
    ("companion", "日常相处", ("owner_advanced", "bond", "continuity", "attention", "habits",
                             "advice", "reference", "repair", "shared_plans", "followthrough")),
    ("learning", "记忆与成长", ("unified_mind", "brain_memory", "mind", "growth", "recollection",
                             "cognition", "experience", "projects")),
    ("resources", "阅读与工具", ("reading", "shared_reading", "explorer", "mcp", "mcp_cap", "mcp_pixiv")),
    ("advanced", "运行与存储", ("plugin", "general", "audience", "brain", "storage", "memory")),
)


def config_layout(schema: dict) -> dict:
    sections = schema.get("sections", {})
    owner = sections.get("owner", {})
    fields = owner.get("fields", {})
    if not {"setup_mode", "qq"} <= fields.keys() or "owner_advanced" in sections:
        return schema

    # SectionRenderer uses section.name as the actual read/write path. Both
    # views edit [owner] and each real field belongs to exactly one view.
    quick_fields = {
        key: {**fields[key], "order": index, "label": label}
        for index, (key, label) in enumerate((("setup_mode", "使用方式"), ("qq", "你的 QQ 号")))
    }
    sections = {**sections,
        "owner": {**owner, "name": "owner", "title": "开始使用",
                  "description": "首次使用推荐「日常陪伴」，填自己的 QQ 号后保存。要按其他标签中保存的开关逐项运行，选「详细配置」。",
                  "fields": quick_fields},
        "owner_advanced": {**owner, "name": "owner", "title": "主人识别与称呼",
                           "fields": {key: value for key, value in fields.items() if key not in quick_fields}},
    }
    for key, title in (("brain_memory", "聊天记忆与群名单"), ("audience", "主动联系的对象与时间")):
        if key in sections:
            sections[key] = {**sections[key], "title": title}

    # Keep future SDK sections reachable even before a dedicated tab is chosen.
    assigned = {name for _, _, names in _TABS for name in names}
    extra = [name for name in sections if name not in assigned]
    display_sections = {}
    tabs = []
    for order, (tab_id, title, names) in enumerate(_TABS):
        keys = [name for name in names if name in sections]
        if tab_id == "advanced":
            keys.extend(extra)
        if keys:
            tabs.append({"id": tab_id, "title": title, "sections": keys, "order": order})
            for name in keys:
                display_sections[name] = {**sections[name], "order": len(display_sections), "collapsed": name != "owner"}
    return {**schema, "sections": display_sections, "layout": {"type": "tabs", "tabs": tabs}}
