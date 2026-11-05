import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("mlw_dashboard_tested", ROOT/"dashboard.py")
dashboard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dashboard)


def fingerprint(record):
    return hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.owner = "12345678"
        self.identity = {"owner_id": self.owner, "agent_id": "persistent-private-identity", "revision": 3}
        self.source = {"id": "brain_fact:1", "scope": "public:news", "sensitivity": "public", "subject_id": "", "text": "source original", "timestamp": 5}
        self.item = {"topic": "天文学", "text": "想继续了解恒星", "sources": [{"id": self.source["id"], "digest": fingerprint(self.source)}], "confidence": .7, "status": "open", "revision": 2}
        self.growth = {"revision": 4, "traits": {"curiosity": .6}, "mood": {"valence": -.2, "energy": .8}, "questions": [self.item]}

    def build(self, **kwargs):
        options = dict(identity=self.identity, growth=self.growth, resolved_sources={self.source["id"]: self.source}, fingerprint=fingerprint, authorized=True, now=100)
        options.update(kwargs)
        return dashboard.build_dashboard(**options)

    def test_requires_server_authorization(self):
        for value in (False, None, "true", 1):
            with self.subTest(value=value), self.assertRaisesRegex(dashboard.DashboardError, "access_denied"):
                self.build(authorized=value)

    def test_public_growth_summary_but_no_original_or_identity(self):
        result = self.build()
        text = json.dumps(result, ensure_ascii=False)
        self.assertIn("天文学", text)
        for private in (self.owner, self.identity["agent_id"], self.source["text"], self.source["id"], self.source["scope"]):
            self.assertNotIn(private, text)
        self.assertTrue(result["identity"]["owner_bound"])
        self.assertEqual(result["source_counts"], {"valid": 1, "invalid": 0, "protected": 0})

    def test_deleted_changed_or_expired_source_invalidates_summary(self):
        for records in ({}, {self.source["id"]: dict(self.source, timestamp=6)}, {self.source["id"]: dict(self.source, text="changed")}):
            with self.subTest(records=records):
                result = self.build(resolved_sources=records)
                self.assertEqual(result["growth"]["questions"][0]["source_status"], "invalid")
                self.assertNotIn("天文学", json.dumps(result, ensure_ascii=False))

    def test_private_sources_require_current_owner_and_nonconfidential(self):
        for sensitivity, subject, scope, expected in (("personal", self.owner, "private:"+self.owner, "valid"),
                ("personal", self.owner, "group:888888", "valid"), ("personal", "87654321", "private:87654321", "protected"),
                ("confidential", self.owner, "private:"+self.owner, "protected"), ("unknown", self.owner, "public", "protected"),
                ("public", "", "private:87654321", "protected")):
            with self.subTest(sensitivity=sensitivity, subject=subject, scope=scope):
                self.source.update(sensitivity=sensitivity, subject_id=subject, scope=scope)
                self.item["sources"][0]["digest"] = fingerprint(self.source)
                result = self.build()
                self.assertEqual(result["growth"]["questions"][0]["source_status"], expected)
                if expected == "protected":
                    self.assertNotIn("天文学", json.dumps(result, ensure_ascii=False))

    def test_all_dependencies_valid_and_readable(self):
        other = dict(self.source, id="mind:other", sensitivity="personal", scope="group:222222", subject_id="87654321")
        self.item["sources"].append({"id": other["id"], "digest": fingerprint(other)})
        result = self.build(resolved_sources={self.source["id"]: self.source, other["id"]: other})
        self.assertEqual(result["source_counts"]["protected"], 1)

    def test_sensitive_generated_text_is_omitted(self):
        for text in ("密码 hello", "邮箱 a@example.com", "QQ 12345678", "https://example.com/private", "token api_key secret"):
            self.item["text"] = text
            result = self.build()
            self.assertEqual(result["growth"]["questions"][0]["text"], "")
            self.assertEqual(result["source_counts"]["protected"], 1)

    def test_companion_only_owner_private_and_whitelisted_fields(self):
        plan = {"owner_id": self.owner, "scope": "private:"+self.owner, "title": "一起读书", "next_step": "阅读下一章", "status": "accepted", "due_at": 500, "updated": 100, "revision": 3, "payload": "private_payload"}
        reading = dict(plan, title="天文随笔", provider="pixiv", resource_type="novel", cursor=200, total_units=1000, source_refs=[{"text": "private_original"}])
        result = self.build(companion={"plans": [plan, dict(plan, owner_id="87654321"), dict(plan, scope="group:888888")], "reading": [reading]})
        self.assertEqual(len(result["plans"]), 1)
        self.assertEqual(result["reading"][0]["current"], 200)
        self.assertEqual(result["reading"][0]["unit"], "字符")
        text = json.dumps(result)
        for secret in ("private_payload", "private_original", self.owner, "87654321", "888888"):
            self.assertNotIn(secret, text)

    def test_malformed_and_large_inputs_are_bounded(self):
        self.growth["questions"] = [self.item]*10000
        self.growth["traits"]["curiosity"] = float("nan")
        result = self.build(budgets={"model": {"used": True, "limit": -1}})
        self.assertEqual(len(result["growth"]["questions"]), 32)
        self.assertIsNone(result["traits"][0]["value"])
        self.assertEqual(result["budgets"]["model"], {"used": 0, "limit": 0})
        self.assertLess(len(json.dumps(result)), 512*1024)

    def test_export_escapes_script_and_discards_unlisted_fields(self):
        self.item["topic"] = "</script><script>alert('x')</script>"
        snapshot = self.build()
        snapshot["raw"] = "neverserialize"
        snapshot["identity"]["owner_id"] = self.owner
        html = dashboard.render_dashboard(snapshot)
        self.assertNotIn("neverserialize", html)
        self.assertNotIn(self.owner, html)
        self.assertNotIn(self.item["topic"], html)
        self.assertIn("\\u003c/script\\u003e", html)
        self.assertIn("window.maibot.query(\"summary\")", html)
        self.assertNotIn("innerHTML", html)
        self.assertNotIn("fetch(", html)

    def test_export_cannot_unhide_protected_item(self):
        snapshot = self.build()
        snapshot["growth"]["questions"][0].update(source_status="protected", topic="hidden_topic", text="hidden_text")
        html = dashboard.render_dashboard(snapshot)
        self.assertNotIn("hidden_topic", html)
        self.assertNotIn("hidden_text", html)

    def test_offline_cli_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            source, output = Path(temp)/"state.json", Path(temp)/"panel.html"
            source.write_text(json.dumps(self.build()), encoding="utf-8")
            self.assertEqual(dashboard.main(["--input", str(source), "--output", str(output)]), 0)
            content = output.read_bytes()
            with self.assertRaises(SystemExit):
                dashboard.main(["--input", str(source), "--output", str(output)])
            self.assertEqual(content, output.read_bytes())

    def test_real_webui_binding_and_offline_seed(self):
        manifest = json.loads((ROOT/"webui.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(manifest["pages"][0]["queries"]["summary"], {"api": "living_world_dashboard", "version": "1"})
        self.assertIn('<script id="snapshot" type="application/json">null</script>', dashboard.render_dashboard())

    @unittest.skipUnless(shutil.which("node"), "JavaScript syntax validation requires Node")
    def test_browser_javascript_parses(self):
        scripts = re.findall(r"<script>(.*?)</script>", dashboard.render_dashboard(), re.S)
        self.assertEqual(len(scripts), 1)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/"dashboard.js"
            path.write_text(scripts[0], encoding="utf-8")
            result = subprocess.run([shutil.which("node"), "--check", str(path)], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
