"""Exercise the native tab, field and save paths used by MaiBot 1.3.10."""
import copy
import unittest
from unittest.mock import patch

from MaiLivingWorld.config_layout import config_layout
from MaiLivingWorld.tests.test_runtime import plugin


def sdk_schema():
    # Minimal shape emitted by SDK 2.12.0. Every field comes from our real config model.
    return {
        "sections": {
            name: {"name": name, "title": name, "collapsed": False,
                   "fields": {key: {"name": key, "default": value, "hidden": False, "order": index}
                              for index, (key, value) in enumerate(fields.items())}}
            for name, fields in plugin.Config().model_dump().items()
        },
        "layout": {"type": "auto", "tabs": []},
    }


class ConfigLayoutTests(unittest.TestCase):
    def test_quick_start_is_first_and_has_only_two_inputs(self):
        original = sdk_schema()
        with patch.object(plugin.MaiBotPlugin, "build_config_schema", create=True, return_value=original) as base:
            result = plugin.MaiLivingWorld.build_config_schema(plugin_id="local.test", plugin_name="demo")
        base.assert_called_once_with(plugin_id="local.test", plugin_name="demo", plugin_version="",
                                     plugin_description="", plugin_author="")
        self.assertEqual(result["layout"]["type"], "tabs")
        self.assertEqual(result["layout"]["tabs"][0]["sections"], ["owner"])
        self.assertEqual(list(result["sections"]["owner"]["fields"]), ["setup_mode", "qq"])
        self.assertFalse(result["sections"]["owner"]["collapsed"])
        self.assertTrue(result["sections"]["owner_advanced"]["collapsed"])
        self.assertEqual(original, sdk_schema())

    def test_each_saved_field_has_one_visible_editor_and_round_trips(self):
        stored = plugin.Config().model_dump()
        # These values must survive a save on an unrelated tab.
        stored["owner"]["private_template"] = "沿用已有模板"
        stored["mcp"]["max_daily_calls"] = 27
        schema = config_layout(sdk_schema())
        tabs = schema["layout"]["tabs"]
        seen_sections = [key for tab in tabs for key in tab["sections"]]
        self.assertCountEqual(seen_sections, schema["sections"].keys())
        seen_fields = []
        edited = copy.deepcopy(stored)
        for key in seen_sections:
            view = schema["sections"][key]
            # The host SectionRenderer uses section.name || key for both reading
            # and writing. Saving the form submits that full edited config.
            real_section = view.get("name") or key
            for field, meta in view["fields"].items():
                self.assertFalse(meta["hidden"])
                seen_fields.append((real_section, field))
                self.assertIn(field, stored[real_section])
            if key == "owner":
                edited[real_section]["qq"] = "12345678"
                edited[real_section]["setup_mode"] = "日常陪伴"
            elif key == "owner_advanced":
                edited[real_section]["title"] = "小麦"
        self.assertCountEqual(seen_fields, [(name, field) for name, fields in stored.items() for field in fields])
        expected = copy.deepcopy(stored)
        expected["owner"].update(qq="12345678", setup_mode="日常陪伴", title="小麦")
        self.assertEqual(plugin.Config.model_validate(edited).model_dump(), expected)
        self.assertNotIn("owner_advanced", edited)

    def test_linear_layout_and_later_sdk_sections_stay_reachable(self):
        original = sdk_schema()
        original["sections"]["new_section"] = {"name": "new_section", "fields": {"extra": {"name": "extra"}}}
        schema = config_layout(original)
        self.assertIn("new_section", schema["layout"]["tabs"][-1]["sections"])
        linear = sorted(schema["sections"].items(), key=lambda entry: entry[1]["order"])
        self.assertEqual(linear[0][0], "owner")
        self.assertTrue(all(section["collapsed"] for _, section in linear[1:]))
        self.assertEqual(config_layout({"sections": {}}), {"sections": {}})
