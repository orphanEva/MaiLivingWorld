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
        self.assertEqual(defaults["plugin"]["config_version"], "0.7.0")
        self.assertFalse(defaults["general"]["enabled"])
        self.assertTrue(defaults["general"]["dry_run"])

    def test_minimal_toml_style_config_can_validate(self):
        cfg = plugin.Config.model_validate({"plugin": {"enabled": True, "config_version": "0.7.0"}})
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
        self.assertEqual(total_fields, 80)
        import re
        comments = re.findall(r"^# [^\n]+\n[a-z_][a-z_0-9]*\s*=", content, flags=re.M)
        self.assertEqual(len(comments), total_fields, "每个字段必须紧跟一条中文注释")

    def test_manifest_version_consistent(self):
        manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], plugin.Config().plugin.config_version)


if __name__ == "__main__":
    unittest.main()
