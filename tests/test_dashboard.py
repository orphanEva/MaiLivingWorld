import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

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

    def test_attention_projection_never_exposes_refs_or_private_text(self):
        value = {"enabled": True, "owner_id": self.owner, "engaged": True, "engaged_until": 150,
                 "focus_kind": "research", "focus_available": True, "focus": {"key": "private-source"},
                 "conversation": {"mode": "repair", "until": 160, "active": True, "text": "private original"},
                 "last_outcome": "interrupted", "cycle_count": 2}
        result = self.build(attention=value)["attention"]
        self.assertTrue(result["engaged"])
        self.assertEqual(result["focus_kind"], "research")
        self.assertEqual(result["conversation"]["mode"], "repair")
        for secret in (self.owner, "private-source", "private original"):
            self.assertNotIn(secret, json.dumps(result))
        self.assertFalse(self.build(attention=dict(value, owner_id="87654321"))["attention"]["enabled"])

    def test_attention_export_expires_transient_state_and_drops_unknown_fields(self):
        value = {"enabled": True, "owner_id": self.owner, "engaged": True, "engaged_until": 150,
                 "focus_kind": "reading", "focus_available": True,
                 "conversation": {"mode": "listen", "until": 160, "active": True}, "last_outcome": "forged text"}
        snapshot = self.build(attention=value)
        snapshot["attention"]["secret"] = "secret private original"
        with patch.object(dashboard.time, "time", return_value=200):
            exported = dashboard._export(snapshot)["attention"]
        self.assertFalse(exported["engaged"])
        self.assertFalse(exported["conversation"]["active"])
        self.assertEqual(exported["last_outcome"], "")
        self.assertNotIn("secret", json.dumps(exported))

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

    def long_interest(self, count):
        records = {f"brain_fact:{number}": dict(self.source, id=f"brain_fact:{number}") for number in range(count)}
        refs = [{"id": record["id"], "digest": fingerprint(record)} for record in records.values()]
        return dict(self.item, sources=refs, weight=.6), records

    def test_interest_capacity_uses_all_roots_while_other_categories_stay_bounded(self):
        for count in (1, 24, 25, 129, 160, 161):
            item, records = self.long_interest(count)
            for category in dashboard.CATEGORIES:
                with self.subTest(count=count, category=category):
                    result = self.build(growth={category: [item]}, resolved_sources=records)
                    visible = result["growth"][category][0]
                    expected = "valid" if count <= (160 if category == "interests" else 24) else "invalid"
                    self.assertEqual(visible["source_status"], expected)
                    self.assertEqual(visible["source_count"], count if expected == "valid" else 0)
                    self.assertEqual(visible["topic"], item["topic"] if expected == "valid" else "")
                    self.assertNotIn(self.source["text"], dashboard.render_dashboard(result))

    def test_long_interest_keeps_old_private_and_invalid_dependencies_protected(self):
        for position in (0, 23, 24, 128, 159):
            for condition in ("missing", "changed", "private", "confidential"):
                with self.subTest(position=position, condition=condition):
                    item, records = self.long_interest(160)
                    original = records[f"brain_fact:{position}"]
                    if condition == "missing":
                        del records[f"brain_fact:{position}"]
                    elif condition == "changed":
                        original["text"] = "changed original"
                    else:
                        original.update(sensitivity="personal" if condition == "private" else "confidential",
                                        scope="private:87654321", subject_id="87654321")
                        item["sources"][position]["digest"] = fingerprint(original)
                    result = self.build(growth={"interests": [item]}, resolved_sources=records)
                    visible = result["growth"]["interests"][0]
                    expected = "invalid" if condition in ("missing", "changed") else "protected"
                    self.assertEqual(visible["source_status"], expected)
                    self.assertEqual(visible["source_count"], 160)
                    self.assertIsNone(visible["value"])
                    for output in (json.dumps(result, ensure_ascii=False), dashboard.render_dashboard(result)):
                        for hidden in (item["topic"], item["text"], original["text"], "87654321"):
                            self.assertNotIn(hidden, output)

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

    def bond(self):
        return {"profile": {"owner_id": self.owner, "preferred_address": "小伙伴", "blocked_addresses": ["老板"],
                "affection": "allow", "boundaries": [{"id": "boundary-private-key", "text": "忙的时候晚点聊"}], "score": 999},
                "moments": [{"id": "bond-private-key", "owner_id": self.owner, "scope": "private:"+self.owner,
                    "kind": "anniversary", "status": "active", "summary": "开始一起读书", "occurred_on": "2024-02-29",
                    "updated": 100, "source_refs": [{"text": "private-source-original"}]}]}

    def test_bond_projection_has_no_identifiers_scores_or_raw_sources(self):
        result = self.build(bond=self.bond())["bond"]
        self.assertTrue(result["enabled"])
        self.assertEqual(result["preferred_address"], "小伙伴")
        self.assertEqual(result["boundaries"], [{"text": "忙的时候晚点聊"}])
        self.assertEqual(result["moments"][0]["occurred_on"], "2024-02-29")
        text = json.dumps(result, ensure_ascii=False)
        for secret in (self.owner, "boundary-private-key", "bond-private-key", "private-source-original", "999", "score"):
            self.assertNotIn(secret, text)

    def test_bond_requires_current_owner_profile_and_exact_private_moments(self):
        values = self.bond()
        own = values["moments"][0]
        values["moments"] += [dict(own, summary="foreign owner", owner_id="87654321"),
            dict(own, summary="group moment", scope="group:888888"), dict(own, summary="retired moment", status="retired")]
        result = self.build(bond=values)["bond"]
        self.assertEqual(len(result["moments"]), 1)
        values["profile"]["owner_id"] = "87654321"
        result = self.build(bond=values)["bond"]
        self.assertFalse(result["enabled"])
        self.assertEqual(result["moments"], [])
        self.assertNotIn("小伙伴", json.dumps(result, ensure_ascii=False))

    def test_bond_absent_and_readonly_default_profile_are_distinct(self):
        self.assertFalse(self.build()["bond"]["enabled"])
        result = self.build(bond={"profile": {"owner_id": self.owner}, "moments": []})["bond"]
        self.assertTrue(result["enabled"])
        self.assertEqual(result["affection"], "default")
        self.assertEqual(result["preferred_address"], "")
        self.assertFalse(self.build(identity={}, bond=self.bond())["bond"]["enabled"])

    def test_bond_sensitive_text_invalid_dates_and_unknown_types_are_filtered(self):
        values = self.bond()
        values["profile"].update(preferred_address="12345678", blocked_addresses=["老板", "密码 secret"],
                                boundaries=[{"text": "API_KEY private"}, {"text": "睡前少发消息"}])
        own = values["moments"][0]
        values["moments"] += [dict(own, summary="密码 secret"), dict(own, occurred_on="2023-02-29"),
                             dict(own, kind="unknown"), dict(own, occurred_on="2024-02-29T00:00:00Z")]
        result = self.build(bond=values)["bond"]
        self.assertEqual(result["preferred_address"], "")
        self.assertEqual(result["blocked_addresses"], ["老板"])
        self.assertEqual(result["boundaries"], [{"text": "睡前少发消息"}])
        self.assertEqual(len(result["moments"]), 1)

    def test_bond_export_revalidates_fields_and_never_embeds_disabled_private_text(self):
        result = self.build(bond=self.bond())
        result["bond"]["owner_id"] = self.owner
        result["bond"]["moments"][0]["raw"] = "hidden-original"
        result["bond"]["moments"][0]["summary"] = "</script><script>alert('bond')</script>"
        html = dashboard.render_dashboard(result)
        self.assertNotIn(self.owner, html)
        self.assertNotIn("hidden-original", html)
        self.assertNotIn("</script><script>alert('bond')", html)
        self.assertIn("\\u003c/script\\u003e", html)
        result["bond"]["enabled"] = False
        self.assertNotIn("小伙伴", dashboard.render_dashboard(result))

    def test_bond_is_bounded_and_ui_uses_concrete_agreements_and_memories(self):
        values = self.bond()
        values["moments"] *= 1000
        result = self.build(bond=values)
        self.assertEqual(len(result["bond"]["moments"]), 200)
        self.assertLess(len(json.dumps(result).encode()), 512*1024)
        html = dashboard.render_dashboard(result)
        for label in ("我们的相处方式", "留下的回忆与纪念日", "主人记下的回忆", "/相处"):
            self.assertIn(label, html)
        self.assertNotIn("好感分", html)

    def test_malformed_and_large_inputs_are_bounded(self):
        self.growth["questions"] = [self.item]*10000
        self.growth["traits"]["curiosity"] = float("nan")
        result = self.build(budgets={"model": {"used": True, "limit": -1}})
        self.assertEqual(len(result["growth"]["questions"]), 32)
        self.assertIsNone(result["traits"][0]["value"])
        self.assertEqual(result["budgets"]["model"], {"used": 0, "limit": 0})
        self.assertLess(len(json.dumps(result)), 512*1024)

    def continuity(self):
        return {"state": {"owner_id": self.owner, "revision": 5,
                    "contact": {"mode": "busy", "until": 500, "active": True, "effective_mode": "busy"},
                    "support": {"mode": "listen", "until": 0, "active": True, "effective_mode": "listen"},
                    "lease_token": "private-lease-token", "cycle_count": 3},
                "threads": [{"id": "continuity-private-key", "owner_id": self.owner, "scope": "private:"+self.owner,
                    "title": "准备考试", "summary": "最近有些担心考试", "kind": "concern", "status": "open", "updated": 99,
                    "source_refs": [{"id": "private-source-id", "digest": "private-digest", "scope": "private:"+self.owner, "sensitivity": "personal"}],
                    "evidence": [{"quote": "private-original-quote"}]}]}

    def test_continuity_projects_only_current_owner_summary_and_effective_status(self):
        result = self.build(continuity=self.continuity())["continuity"]
        self.assertTrue(result["enabled"])
        self.assertEqual(result["contact"], {"mode": "busy", "active": True, "until": "1970-01-01T00:08:20+00:00"})
        self.assertEqual(result["support"], {"mode": "listen", "active": True, "until": ""})
        self.assertEqual(result["threads"], [{"title": "准备考试", "summary": "最近有些担心考试", "kind": "concern",
            "status": "open", "updated_at": "1970-01-01T00:01:39+00:00", "source_count": 1}])
        text = json.dumps(result, ensure_ascii=False)
        for hidden in (self.owner, "private:", "private-key", "private-lease-token", "private-source-id",
                       "private-digest", "private-original-quote", "cycle_count", "revision"):
            self.assertNotIn(hidden, text)

    def test_continuity_disabled_unknown_owner_and_old_owner_are_empty(self):
        for values in (None, {}, {"state": {"owner_id": "87654321"}, "threads": self.continuity()["threads"]}):
            result = self.build(continuity=values)["continuity"]
            self.assertFalse(result["enabled"])
            self.assertEqual(result["threads"], [])
        result = self.build(identity={}, continuity=self.continuity())["continuity"]
        self.assertFalse(result["enabled"])

    def test_continuity_rejects_foreign_group_closed_and_sensitive_summaries(self):
        values = self.continuity()
        own = values["threads"][0]
        values["threads"] += [dict(own, owner_id="87654321"), dict(own, scope="group:777777"),
            dict(own, status="resolved"), dict(own, status="dismissed"), dict(own, kind="unknown"),
            dict(own, title="密码 secret"), dict(own, summary="QQ 12345678"), dict(own, source_refs=[]),
            dict(own, source_refs=[{"scope": "private:87654321", "sensitivity": "personal"}]),
            dict(own, source_refs=[{"scope": "private:"+self.owner, "sensitivity": "confidential"}])]
        result = self.build(continuity=values)["continuity"]
        self.assertEqual(len(result["threads"]), 1)
        self.assertEqual(result["threads"][0]["title"], "准备考试")

    def test_continuity_elapsed_pause_is_normal_even_if_source_active_is_stale(self):
        for now in (500, 501):
            result = self.build(continuity=self.continuity(), now=now)["continuity"]
            self.assertEqual(result["contact"], {"mode": "normal", "active": False, "until": ""})
            self.assertEqual(result["support"]["mode"], "listen")
        values = self.continuity()
        values["state"]["contact"] = {"mode": "manual", "active": True, "until": 0}
        self.assertEqual(self.build(continuity=values, now=501)["continuity"]["contact"]["mode"], "manual")

    def test_continuity_malformed_pause_fails_closed_without_invented_busy_status(self):
        for value in ({"mode": "busy", "active": True, "until": 0}, {"mode": "busy", "active": True, "until": float("nan")},
                      {"mode": "rest", "active": "yes", "until": 500}, {"mode": [], "active": True, "until": 500},
                      {"mode": "busy", "active": True, "until": "later"}):
            values = self.continuity()
            values["state"]["contact"] = value
            with self.subTest(value=value):
                self.assertEqual(self.build(continuity=values)["continuity"]["contact"]["mode"], "normal")

    def test_continuity_bounded_empty_state_and_readonly_ui_wording(self):
        values = self.continuity()
        values["threads"] *= 1000
        result = self.build(continuity=values)
        self.assertEqual(len(result["continuity"]["threads"]), 20)
        values["threads"] = []
        empty = self.build(continuity=values)["continuity"]
        self.assertTrue(empty["enabled"])
        self.assertEqual(empty["threads"], [])
        html = dashboard.render_dashboard(result)
        for label in ("最近放在心上的事", "自然聊天", "不代表已承诺提醒", "不代表已经发出关心", "暂停至", "先倾听"):
            self.assertIn(label, html)
        self.assertIn("expires<=Date.now()", html)
        self.assertIn("setInterval", html)
        self.assertNotIn("innerHTML", html)

    def test_continuity_offline_export_revalidates_expiry_and_strips_hidden_fields(self):
        result = self.build(continuity=self.continuity())
        result["continuity"]["owner_id"] = self.owner
        thread = result["continuity"]["threads"][0]
        thread["evidence"] = [{"quote": "never-serialize-quote"}]
        thread["source_refs"] = [{"id": "never-serialize-id"}]
        thread["summary"] = "</script><script>alert('continuity')</script>"
        with patch.object(dashboard.time, "time", return_value=501):
            exported = dashboard._export(result)
            html = dashboard.render_dashboard(result)
        self.assertEqual(exported["continuity"]["contact"]["mode"], "normal")
        for hidden in (self.owner, "never-serialize-quote", "never-serialize-id", "</script><script>alert('continuity')"):
            self.assertNotIn(hidden, html)
        self.assertIn("\\u003c/script\\u003e", html)
        result["continuity"]["enabled"] = False
        self.assertNotIn("准备考试", dashboard.render_dashboard(result))

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

    def test_export_hides_legacy_private_interest_weights(self):
        item, records = self.long_interest(1)
        snapshot = self.build(growth={"interests": [item]}, resolved_sources=records)
        for status in ("protected", "invalid", "unknown"):
            with self.subTest(status=status):
                snapshot["growth"]["interests"][0].update(source_status=status, value=.6)
                exported = dashboard._export(snapshot)["growth"]["interests"][0]
                self.assertIsNone(exported["value"])
                self.assertEqual(exported["topic"], "")
                self.assertEqual(exported["text"], "")

    def test_offline_cli_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            source, output = Path(temp)/"state.json", Path(temp)/"panel.html"
            source.write_text(json.dumps(self.build()), encoding="utf-8")
            self.assertEqual(dashboard.main(["--input", str(source), "--output", str(output)]), 0)
            content = output.read_bytes()
            with self.assertRaises(SystemExit):
                dashboard.main(["--input", str(source), "--output", str(output)])
            self.assertEqual(content, output.read_bytes())

    def test_direct_offline_cli_still_exports_without_a_package_context(self):
        with tempfile.TemporaryDirectory() as temp:
            source, output = Path(temp)/"state.json", Path(temp)/"panel.html"
            source.write_text(json.dumps(self.build()), encoding="utf-8")
            result = subprocess.run([sys.executable, "-I", str(ROOT/"dashboard.py"),
                "--input", str(source), "--output", str(output)],
                cwd=temp, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("<html", output.read_text(encoding="utf-8"))

    def test_top_level_import_builds_without_a_package_or_database_dependencies(self):
        code = """
import sys
sys.path.insert(0, sys.argv[1])
import dashboard
snapshot = dashboard.build_dashboard(identity={}, growth={}, resolved_sources={},
    fingerprint=lambda record: '', authorized=True)
assert snapshot['source_counts'] == {'valid': 0, 'invalid': 0, 'protected': 0}
assert 'growth_limits' in sys.modules
assert not any(name.endswith('growth_store') for name in sys.modules)
assert 'pydantic' not in sys.modules and 'pymysql' not in sys.modules
"""
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT)],
                cwd=temp, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)

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
