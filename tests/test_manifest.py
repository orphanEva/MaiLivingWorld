"""只检查发布协议的必填结构，避免重复出现 ZIP 无法安装的问题。"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))

    def test_required_fields(self):
        required = {
            "manifest_version", "id", "version", "name", "description", "author",
            "license", "urls", "host_application", "sdk", "capabilities", "i18n",
        }
        self.assertFalse(required - set(self.manifest))
        self.assertEqual(self.manifest["manifest_version"], 2)
        self.assertRegex(self.manifest["version"], r"^\d+\.\d+\.\d+$")
        self.assertRegex(self.manifest["id"], r"^[a-z0-9]+(?:[.-][a-z0-9]+)+$")

    def test_author_and_urls(self):
        self.assertTrue(self.manifest["author"].get("name"))
        self.assertTrue(self.manifest["author"].get("url"))
        self.assertTrue(self.manifest["urls"].get("repository"))
        for value in [self.manifest["author"]["url"], *self.manifest["urls"].values()]:
            self.assertIsInstance(value, str)
            parsed = urlsplit(value)
            self.assertIn(parsed.scheme, ("http", "https"))
            self.assertTrue(parsed.netloc)

    def test_compatibility_ranges(self):
        for key in ("host_application", "sdk"):
            for limit in ("min_version", "max_version"):
                self.assertRegex(self.manifest[key][limit], r"^\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
