"""回归：标准配置模型必须包含 MaiBot Host 要求的 [plugin] 和版本。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from MaiLivingWorld.tests.test_runtime import plugin

ROOT = Path(__file__).resolve().parents[1]


class ConfigSectionTests(unittest.TestCase):
    def test_defaults_generate_plugin_section(self):
        defaults = plugin.Config().model_dump()
        self.assertIn("plugin", defaults)
        self.assertTrue(defaults["plugin"]["enabled"])
        self.assertEqual(defaults["plugin"]["config_version"], "0.33.0")
        self.assertFalse(defaults["mcp"]["public_sources_enabled"])
        self.assertEqual(defaults["mcp"]["social_mode"], "关闭")
        self.assertFalse(defaults["general"]["enabled"])
        self.assertTrue(defaults["general"]["dry_run"])

    def test_minimal_toml_style_config_can_validate(self):
        cfg = plugin.Config.model_validate({"plugin": {"enabled": True, "config_version": "0.10.0"}})
        self.assertTrue(cfg.plugin.enabled)
        self.assertFalse(cfg.general.enabled)
        self.assertEqual(cfg.storage.backend, "sqlite")

    def test_comment_example_has_every_config_field_and_same_defaults(self):
        import tomllib
        example_path = ROOT / "config.example.toml"
        content = example_path.read_text(encoding="utf-8")
        parsed = tomllib.loads(content)
        defaults = plugin.Config().model_dump()
        self.assertEqual(set(parsed), set(defaults))
        self.assertEqual(parsed, defaults)
        total_fields = sum(len(fields) for fields in defaults.values())
        self.assertEqual(len(defaults), 30)
        self.assertEqual(total_fields, 201)
        import re
        comments = re.findall(r"^# [^\n]+\n[a-z_][a-z_0-9]*\s*=", content, flags=re.M)
        self.assertEqual(len(comments), total_fields, "每个字段必须紧跟一条中文注释")

    def test_manifest_version_consistent(self):
        manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], plugin.Config().plugin.config_version)

    def test_quick_setup_keeps_the_public_tools_and_qq_controls_independent(self):
        model = plugin.Config.model_json_schema()
        def fields(section):
            name = model["properties"][section]["$ref"].rsplit("/", 1)[1]
            return model["$defs"][name]["properties"]

        self.assertEqual(fields("general")["enabled"]["label"], "主动 QQ 联系")
        self.assertEqual(fields("general")["dry_run"]["label"], "安全演练")
        public = fields("mcp")
        self.assertEqual(public["enabled"]["label"], "启用 MCP 工具")
        self.assertEqual(public["public_sources_enabled"]["label"], "查新闻和百科")
        self.assertEqual(public["social_mode"]["enum"], ["关闭", "只逛逛", "参与讨论"])
        for mode in ("日常陪伴", "陪伴与成长"):
            with self.subTest(mode=mode):
                instance = plugin.MaiLivingWorld()
                instance.set_plugin_config({"plugin": {"config_version": plugin.Config().plugin.config_version},
                                            "owner": {"setup_mode": mode, "qq": "12345678"}})
                self.assertFalse(instance.config.general.enabled)
                self.assertTrue(instance.config.general.dry_run)
                saved = instance.get_plugin_config_data()
                saved["general"]["dry_run"] = False
                saved["mcp"].update(enabled=True, public_sources_enabled=True, social_mode="参与讨论")
                reloaded = plugin.MaiLivingWorld()
                reloaded.set_plugin_config(saved)
                self.assertFalse(reloaded.config.general.enabled)
                self.assertFalse(reloaded.config.general.dry_run)
                self.assertTrue(reloaded.config.mcp.enabled and reloaded.config.mcp.public_sources_enabled)
                self.assertEqual(reloaded.config.mcp.social_mode, "参与讨论")
                self.assertEqual(reloaded.config.unified_mind.mode, "active")

    def test_belief_review_requires_explicit_opt_in_and_has_webui_description(self):
        self.assertFalse(plugin.Config().growth.review_beliefs)
        config = plugin.Config.model_validate({"growth": {"enabled": True, "mode": "active"}})
        self.assertFalse(config.growth.review_beliefs)
        schema = plugin.Config.model_json_schema()
        ref = schema["properties"]["growth"]["$ref"].rsplit("/", 1)[1]
        field = schema["$defs"][ref]["properties"]["review_beliefs"]
        self.assertEqual(field["type"], "boolean")
        self.assertFalse(field["default"])
        self.assertTrue(field["description"])

    def test_dynamic_webui_config_schema_exposes_recollection_and_reply_reserve(self):
        schema = plugin.Config.model_json_schema()
        def section(name):
            ref = schema["properties"][name]["$ref"].rsplit("/", 1)[1]
            return schema["$defs"][ref]["properties"]
        self.assertEqual(len(schema["properties"]), 30)
        recollection = section("recollection")
        self.assertEqual(set(recollection), {"enabled", "auto_correct", "interval_seconds",
                                            "max_daily_batches", "repeat_cooldown_hours"})
        for name, default in plugin.Config().recollection.model_dump().items():
            self.assertEqual(recollection[name]["default"], default)
            self.assertTrue(recollection[name]["description"])
        self.assertFalse(recollection["enabled"]["default"])
        self.assertEqual(recollection["interval_seconds"]["minimum"], 30)
        self.assertEqual(recollection["max_daily_batches"]["maximum"], 30)
        self.assertEqual(recollection["repeat_cooldown_hours"]["minimum"], 1)
        reserve = section("unified_mind")["reserved_reply_calls"]
        self.assertEqual((reserve["type"], reserve["default"], reserve["minimum"], reserve["maximum"]),
                         ("integer", 12, 0, 1000))
        self.assertIn("23 /", plugin.Recollection.__ui_label__)

    def test_personality_and_activity_require_opt_in_with_webui_descriptions(self):
        config = plugin.Config.model_validate({"growth": {"enabled": True}, "attention": {"enabled": True}})
        schema = plugin.Config.model_json_schema()
        for section, name in (("growth", "personality_tensions"), ("attention", "activity_awareness")):
            with self.subTest(section=section, name=name):
                self.assertFalse(getattr(getattr(config, section), name))
                ref = schema["properties"][section]["$ref"].rsplit("/", 1)[1]
                field = schema["$defs"][ref]["properties"][name]
                self.assertEqual(field["type"], "boolean")
                self.assertFalse(field["default"])
                self.assertTrue(field["description"])

    def test_dynamic_webui_config_includes_safe_habits_and_followthrough_defaults(self):
        schema = plugin.Config.model_json_schema()
        expected = {
            "habits": {"enabled": False, "learn_from_feedback": True, "interval_seconds": 300, "max_daily_batches": 4},
            "followthrough": {"enabled": False, "interval_seconds": 300, "max_daily_checks": 12},
        }
        for name, defaults in expected.items():
            with self.subTest(section=name):
                self.assertEqual(getattr(plugin.Config(), name).model_dump(), defaults)
                ref = schema["properties"][name]["$ref"].rsplit("/", 1)[1]
                fields = schema["$defs"][ref]["properties"]
                self.assertEqual(set(fields), set(defaults))
                self.assertEqual({key: field["default"] for key, field in fields.items()}, defaults)
                self.assertTrue(all(field["description"] for field in fields.values()))

    def test_reply_emotions_and_archive_features_require_separate_opt_in(self):
        config = plugin.Config.model_validate({name: {"enabled": True} for name in ("attention", "growth", "advice", "repair")})
        schema = plugin.Config.model_json_schema()
        for section, key in (("attention", "reply_coordination"), ("growth", "emotional_episodes"),
                             ("advice", "archive_enabled"), ("repair", "archive_enabled")):
            with self.subTest(section=section, key=key):
                self.assertFalse(getattr(getattr(config, section), key))
                ref = schema["properties"][section]["$ref"].rsplit("/", 1)[1]
                field = schema["$defs"][ref]["properties"][key]
                self.assertEqual(field["type"], "boolean")
                self.assertFalse(field["default"])
                self.assertTrue(field["description"])

    def test_project_and_shared_reading_defaults_and_bounds_are_explicit(self):
        schema = plugin.Config.model_json_schema()
        expected = {
            "projects": {"enabled": False, "interval_seconds": 900, "max_daily_actions": 6, "max_active_projects": 2},
            "shared_reading": {"enabled": False},
        }
        for name, defaults in expected.items():
            with self.subTest(section=name):
                self.assertEqual(getattr(plugin.Config(), name).model_dump(), defaults)
                ref = schema["properties"][name]["$ref"].rsplit("/", 1)[1]
                fields = schema["$defs"][ref]["properties"]
                self.assertEqual(set(fields), set(defaults))
                self.assertEqual({key: field["default"] for key, field in fields.items()}, defaults)
                self.assertTrue(all(field["description"] for field in fields.values()))
                if name == "projects":
                    for field, limits in {"interval_seconds": (300, 86400), "max_daily_actions": (0, 24), "max_active_projects": (1, 3)}.items():
                        self.assertEqual((fields[field]["minimum"], fields[field]["maximum"]), limits)


    def test_repair_defaults_and_bounds_are_explicit(self):
        defaults = {"enabled": False, "auto_review": True, "interval_seconds": 300, "max_daily_batches": 4,
                    "archive_enabled": False}
        self.assertEqual(plugin.Config().repair.model_dump(), defaults)
        schema = plugin.Config.model_json_schema()
        ref = schema["properties"]["repair"]["$ref"].rsplit("/", 1)[1]
        fields = schema["$defs"][ref]["properties"]
        self.assertEqual(set(fields), set(defaults))
        self.assertEqual({key: field["default"] for key, field in fields.items()}, defaults)
        self.assertTrue(all(field["description"] for field in fields.values()))
        for field, bounds in {"interval_seconds": (30, 86400), "max_daily_batches": (0, 24)}.items():
            self.assertEqual((fields[field]["minimum"], fields[field]["maximum"]), bounds)

    def test_advice_and_reference_defaults_and_bounds_are_explicit(self):
        schema = plugin.Config.model_json_schema()
        expected = {
            "advice": {"enabled": False, "auto_feedback": True, "interval_seconds": 600, "max_daily_batches": 4,
                       "archive_enabled": False},
            "reference": {"enabled": False, "ttl_hours": 24},
        }
        for name, defaults in expected.items():
            with self.subTest(section=name):
                self.assertEqual(getattr(plugin.Config(), name).model_dump(), defaults)
                ref = schema["properties"][name]["$ref"].rsplit("/", 1)[1]
                fields = schema["$defs"][ref]["properties"]
                self.assertEqual(set(fields), set(defaults))
                self.assertEqual({key: field["default"] for key, field in fields.items()}, defaults)
                self.assertTrue(all(field["description"] for field in fields.values()))
                limits = {"interval_seconds": (60, 86400), "max_daily_batches": (0, 24)} if name == "advice" else {"ttl_hours": (1, 72)}
                for field, bounds in limits.items():
                    self.assertEqual((fields[field]["minimum"], fields[field]["maximum"]), bounds)


if __name__ == "__main__":
    unittest.main()
