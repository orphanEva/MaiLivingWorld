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
        self.assertEqual(defaults["plugin"]["config_version"], "0.21.0")
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
        self.assertEqual(len(defaults), 29)
        self.assertEqual(total_fields, 186)
        import re
        comments = re.findall(r"^# [^\n]+\n[a-z_][a-z_0-9]*\s*=", content, flags=re.M)
        self.assertEqual(len(comments), total_fields, "每个字段必须紧跟一条中文注释")

    def test_manifest_version_consistent(self):
        manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], plugin.Config().plugin.config_version)

    def test_dynamic_webui_config_schema_exposes_recollection_and_reply_reserve(self):
        schema = plugin.Config.model_json_schema()
        def section(name):
            ref = schema["properties"][name]["$ref"].rsplit("/", 1)[1]
            return schema["$defs"][ref]["properties"]
        self.assertEqual(len(schema["properties"]), 29)
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


    def test_advice_and_reference_defaults_and_bounds_are_explicit(self):
        schema = plugin.Config.model_json_schema()
        expected = {
            "advice": {"enabled": False, "auto_feedback": True, "interval_seconds": 600, "max_daily_batches": 4},
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
