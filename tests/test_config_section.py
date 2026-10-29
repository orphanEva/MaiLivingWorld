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
        self.assertEqual(defaults["plugin"]["config_version"], "0.4.3")
        self.assertFalse(defaults["general"]["enabled"])
        self.assertTrue(defaults["general"]["dry_run"])

    def test_minimal_toml_style_config_can_validate(self):
        cfg = plugin.Config.model_validate({"plugin": {"enabled": True, "config_version": "0.4.3"}})
        self.assertTrue(cfg.plugin.enabled)
        self.assertFalse(cfg.general.enabled)
        self.assertEqual(cfg.storage.backend, "sqlite")

    def test_manifest_version_consistent(self):
        manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], plugin.Config().plugin.config_version)


if __name__ == "__main__":
    unittest.main()
