"""Exercise quick setup at the SDK configuration boundary and real QQ hook entry points."""
import json
import tempfile
import tomllib
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from MaiLivingWorld.tests.test_mcp_plugin import GROUP, OTHER, OWNER, message
from MaiLivingWorld.tests.test_runtime import FakeContext, plugin


def settings(mode="日常陪伴", qq=OWNER):
    version = plugin.Config().plugin.config_version
    return tomllib.loads(
        f'[plugin]\nconfig_version = "{version}"\n[owner]\nsetup_mode = "{mode}"\nqq = "{qq}"'
    )


def load_config(bot, raw):
    # Runner first normalizes what it may save, then injects the expanded snapshot.
    normalized, _ = bot.normalize_plugin_config(raw)
    before = deepcopy(normalized)
    bot.set_plugin_config(normalized)
    if normalized != before or bot.get_plugin_config_data() != before:
        raise AssertionError("a runtime preset changed the settings saved by the host")
    return normalized


class QuickSetupTests(unittest.TestCase):
    def test_legacy_custom_config_keeps_every_value_after_upgrade(self):
        old = {
            "plugin": {"config_version": "0.26.0"},
            "owner": {"qq": OWNER, "enabled": False, "relationship_mode": "custom", "private_template": "旧风格"},
            "brain_memory": {"enabled": True, "auto_reflect": False, "group_ids": [GROUP]},
            "unified_mind": {"enabled": True, "mode": "observe", "max_daily_model_calls": 7},
        }
        bot = plugin.MaiLivingWorld()
        stored = load_config(bot, old)
        self.assertEqual(bot.config.owner.setup_mode, "详细配置")
        self.assertEqual(bot.config.model_dump(), stored)
        for section, fields in old.items():
            for key, value in fields.items():
                self.assertEqual(stored[section][key], value)

    def test_two_field_toml_and_expanded_webui_config_produce_the_same_companion(self):
        bot = plugin.MaiLivingWorld()
        for raw in (settings(), plugin.Config.model_validate(settings()).model_dump()):
            with self.subTest(expanded="bond" in raw):
                original = deepcopy(raw)
                stored = load_config(bot, raw)
                cfg = bot.config
                self.assertEqual(raw, original)
                self.assertFalse(stored["owner"]["enabled"])
                self.assertTrue(cfg.owner.enabled and cfg.owner.recognize_in_private)
                self.assertEqual(cfg.owner.relationship_mode, "partner")
                self.assertTrue(cfg.unified_mind.enabled and cfg.unified_mind.mode == "active")
                self.assertTrue(cfg.bond.enabled and cfg.reference.enabled and cfg.continuity.enabled)
                self.assertFalse(cfg.continuity.auto_topics or cfg.brain_memory.auto_reflect or cfg.growth.enabled)
                self.assertFalse(cfg.general.enabled or cfg.explorer.enabled or cfg.mcp.enabled)
                self.assertEqual(cfg.brain_memory.group_ids, [])
                self.assertEqual(cfg.audience.private_users, [])

    def test_growing_companion_learns_and_detailed_mode_restores_manual_settings(self):
        bot = plugin.MaiLivingWorld()
        raw = settings("陪伴与成长")
        raw.update({
            "storage": {"backend": "mysql", "mysql_database": "private_test_db", "sql_debug": True},
            "brain_memory": {"private_users": [OTHER], "group_ids": [GROUP], "interval_hours": 20},
            "audience": {"group_ids": [GROUP], "max_daily_per_target": 1},
            "general": {"enabled": True, "dry_run": True},
            "mcp": {"enabled": True, "allow_writes": False, "max_daily_calls": 9},
            "unified_mind": {"max_daily_model_calls": 23, "min_interval_seconds": 90},
            "growth": {"interval_seconds": 7200},
        })
        stored = load_config(bot, raw)
        cfg = bot.config
        self.assertTrue(cfg.continuity.auto_topics)
        self.assertTrue(cfg.brain_memory.enabled and cfg.brain_memory.auto_reflect)
        self.assertTrue(cfg.brain_memory.enable_events and cfg.brain_memory.enable_facts)
        self.assertTrue(cfg.growth.enabled and cfg.growth.mode == "active")
        for section in ("storage", "audience", "general", "mcp"):
            self.assertEqual(getattr(cfg, section).model_dump(), stored[section])
        for section in ("brain_memory", "growth", "unified_mind"):
            for key, value in raw[section].items():
                self.assertEqual(getattr(getattr(cfg, section), key), value)
        stored["owner"]["setup_mode"] = "日常陪伴"
        load_config(bot, stored)
        self.assertFalse(bot.config.growth.enabled or bot.config.brain_memory.auto_reflect)
        stored["owner"]["setup_mode"] = "详细配置"
        load_config(bot, stored)
        self.assertEqual(bot.config.model_dump(), stored)

    def test_invalid_quick_owner_does_not_replace_the_last_valid_configuration(self):
        bot = plugin.MaiLivingWorld()
        stored = load_config(bot, settings())
        effective = bot.config.model_dump()
        for qq in ("", "１２３４５６", "nickname", "1234", "1"*21):
            with self.subTest(qq=qq):
                with self.assertRaisesRegex(ValueError, "主人.*QQ"):
                    bot.set_plugin_config(settings("陪伴与成长", qq=qq))
                self.assertEqual(bot.get_plugin_config_data(), stored)
                self.assertEqual(bot.config.model_dump(), effective)

    def test_global_off_applies_to_quick_modes(self):
        for mode in ("日常陪伴", "陪伴与成长"):
            with self.subTest(mode=mode):
                bot = plugin.MaiLivingWorld()
                raw = settings(mode)
                raw["plugin"]["enabled"] = False
                load_config(bot, raw)
                self.assertIsNone(bot._ensure_unified())
                self.assertIsNone(bot._ensure_bond())
                self.assertIsNone(bot._ensure_growth())


class QuickSetupHookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bot = self.make_bot()
        await self.bot.on_load()

    def make_bot(self):
        bot = plugin.MaiLivingWorld()
        bot.ctx = FakeContext()
        bot.ctx.paths = SimpleNamespace(data_dir=Path(self.temp.name))
        load_config(bot, settings())
        return bot

    async def asyncTearDown(self):
        await self.bot.on_unload()

    async def capture(self, text, mid, **sender):
        event = message(mid=mid, text=text, **sender)
        await self.bot.capture_owner(message=event)
        await self.bot.capture_unified(message=event)
        return {"session_id": event["session_id"], "reply_message_id": event["message_id"]}

    async def test_basic_preset_saves_only_owner_confirmed_memory_across_restart(self):
        await self.capture("/相处 记住 群里的冒领", "group", sid="room", group=GROUP)
        guest = await self.capture("/相处 记住 私聊里的冒领", "guest", sid="guest", uid=OTHER)
        self.assertFalse((await self.bot.relationship(**guest))["success"])
        await self.capture("/相处 记住 我们约好周末一起读小说", "owner")
        self.bot.ctx.llm.generate.assert_not_awaited()
        self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()
        await self.bot.on_unload()
        self.bot = self.make_bot()
        await self.bot.on_load()
        request = await self.capture("之前约好周末做什么？", "return", sid="new-session")
        result = await self.bot.relationship(**request)
        self.assertTrue(result["success"])
        data = json.loads(result["content"].split("\n", 1)[1])
        self.assertEqual([m["summary"] for m in data["moments"]], ["我们约好周末一起读小说"])

    async def test_hot_reload_retracts_old_context_and_reactivates_owner_memory(self):
        await self.capture("/相处 称呼 小月亮", "remember")
        old = await self.capture("你好", "hello")
        self.assertIn("小月亮", self.bot._owner_prompt_for(old))
        raw = settings("详细配置")
        stored = load_config(self.bot, raw)
        await self.bot.on_config_update("plugin", stored, "manual")
        self.assertEqual(self.bot._owner_prompt_for(old), "")
        self.assertFalse((await self.bot.relationship(**old))["success"])
        stored = load_config(self.bot, settings())
        await self.bot.on_config_update("plugin", stored, "basic")
        self.assertEqual(self.bot._owner_prompt_for(old), "")
        new = await self.capture("我回来啦", "hello-again")
        self.assertIn("小月亮", self.bot._owner_prompt_for(new))
