"""Only current, still-supported preference changes may reach ordinary replies."""
import copy
import json
import unittest
from unittest.mock import patch

from MaiLivingWorld.growth_store import source_fingerprint
from MaiLivingWorld.unified_expression import expression_hint, parse_expression
from MaiLivingWorld.unified_memory import annotate_interest_changes, derived_memories
from MaiLivingWorld.tests import test_interest_expression as fixtures
from MaiLivingWorld.tests.test_growth_runtime import model_response
from MaiLivingWorld.tests.test_mcp_plugin import OWNER, message


class InterestHistoryTests(unittest.TestCase):
    def setUp(self):
        self.roots = [
            {"id": "brain:event:"+key, "source": "brain_event", "kind": "experience",
             "text": "讨论摄影构图与光线", "scope": "private:"+OWNER, "subject_id": OWNER,
             "sensitivity": "personal", "timestamp": index+1, "evidence_status": "observed"}
            for index, key in enumerate(("first", "second"))]
        refs = [{"id": root["id"], "digest": source_fingerprint(root),
                 "scope": root["scope"], "sensitivity": root["sensitivity"]} for root in self.roots]
        self.before = {"key": "i:"+"a"*24, "topic": "摄影", "weight": 0.4,
                       "revision": 1, "sources": refs[:1]}
        self.after = {**self.before, "weight": 0.3, "revision": 2, "sources": refs}
        self.history = [{"kind": "growth", "rolled_back": False, "revision": 2,
                         "before": {"interests": [self.before]}, "after": {"interests": [self.after]}}]
        self.records = derived_memories({"interests": [self.after]},
            {root["id"]: root for root in self.roots}, fingerprint=source_fingerprint)

    def test_exact_change_keeps_sources_privacy_and_outward_naturalization(self):
        original = copy.deepcopy((self.records, self.history))
        records = annotate_interest_changes(self.records, self.history)
        self.assertIn("此前更愿意了解这个方向", records[0]["text"])
        self.assertIn("兴趣有所减弱", records[0]["text"])
        self.assertEqual(records[0]["sources"], self.records[0]["sources"])
        self.assertEqual((self.records, self.history), original)
        raw = json.dumps({"behavior": "neutral", "decisions": [{"id": records[0]["id"], "mode": "say"}]})
        private = parse_expression(raw, records, audience_scope="private:"+OWNER, speaker_id=OWNER, is_owner=True)
        self.assertIn("兴趣有所减弱", expression_hint(private))
        self.assertNotIn("偏好强度", expression_hint(private))
        group = parse_expression(raw, records, audience_scope="group:23456789", speaker_id=OWNER, is_owner=True)
        self.assertEqual(group["memories"], [])

    def test_current_version_must_match_every_identity_and_source_field(self):
        for field, value in (("id", "growth:interest:i:"+"b"*24), ("topic", "音乐"), ("weight", 0.2),
                             ("revision", 3), ("sources", self.after["sources"][:1])):
            with self.subTest(field=field):
                record = {**self.records[0], field: value}
                self.assertEqual(annotate_interest_changes([record], self.history), [record])

    def test_old_state_cannot_add_private_or_changed_sources(self):
        for update in ({"sources": [{**self.before["sources"][0], "scope": "private:87654321"}]},
                       {"sources": []}, {"sources": [{**self.before["sources"][0], "digest": "f"*64}]},
                       {"weight": True}, {"weight": float("nan")}, {"weight": 5}, {"revision": 0}, {"topic": "音乐"}):
            history = copy.deepcopy(self.history)
            history[0]["before"]["interests"][0].update(update)
            with self.subTest(update=update):
                self.assertEqual(annotate_interest_changes(self.records, history), self.records)

    def test_initial_growth_admin_operations_and_ambiguous_items_are_not_changes(self):
        for mutate in (
            lambda entry: entry.update(kind="rollback"),
            lambda entry: entry.update(kind="retire"),
            lambda entry: entry.update(rolled_back=True),
            lambda entry: entry["before"].update(interests=[]),
            lambda entry: entry["before"]["interests"].append(copy.deepcopy(self.before)),
            lambda entry: entry["after"]["interests"].append(copy.deepcopy(self.after)),
        ):
            history = copy.deepcopy(self.history)
            mutate(history[0])
            self.assertEqual(annotate_interest_changes(self.records, history), self.records)
        first = {**self.records[0], "revision": 1}
        history = copy.deepcopy(self.history)
        history[0]["before"]["interests"][0]["revision"] = 0
        history[0]["after"]["interests"][0]["revision"] = 1
        self.assertEqual(annotate_interest_changes([first], history), [first])

    def test_only_twenty_recent_history_rows_are_considered(self):
        filler = {"kind": "growth", "rolled_back": False, "before": {}, "after": {}}
        self.assertNotEqual(annotate_interest_changes(self.records, [filler]*19+self.history), self.records)
        self.assertEqual(annotate_interest_changes(self.records, [filler]*20+self.history), self.records)

    def test_changed_source_cannot_be_restored_by_history(self):
        roots = {root["id"]: root for root in self.roots}
        roots[self.roots[0]["id"]] = {**self.roots[0], "text": "原内容已更正"}
        records = derived_memories({"interests": [self.after]}, roots, fingerprint=source_fingerprint)
        self.assertEqual(annotate_interest_changes(records, self.history), [])

    def test_tiny_shift_and_neutral_state_do_not_become_strong_aversion(self):
        for before, after, previous in ((-0.00000001, 0.00000001, "此前不太感兴趣"),
                                        (0.0, 0.1, "此前没有明显喜恶")):
            old, current = {**self.before, "weight": before}, {**self.after, "weight": after}
            records = derived_memories({"interests": [current]},
                {root["id"]: root for root in self.roots}, fingerprint=source_fingerprint)
            history = [{"kind": "growth", "rolled_back": False,
                        "before": {"interests": [old]}, "after": {"interests": [current]}}]
            text = annotate_interest_changes(records, history)[0]["text"]
            self.assertIn(previous, text)
            self.assertIn("兴趣有所回升", text)
            self.assertNotIn("讨厌", text)


class InterestHistoryPluginTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.InterestExpressionTests.asyncSetUp
    asyncTearDown = fixtures.InterestExpressionTests.asyncTearDown
    make_bot = fixtures.InterestExpressionTests.make_bot
    source = fixtures.InterestExpressionTests.source
    learn = fixtures.InterestExpressionTests.learn
    reply = fixtures.InterestExpressionTests.reply

    async def grow_photo(self):
        source = self.source("photo-change", "另一份摄影资料展示了夜间构图与光线的变化。")
        await self.learn(interests=[("摄影", 0.02, source)])

    async def test_reply_remembers_accepted_change_across_restart_and_rollback(self):
        self.assertNotIn("此前", await self.reply("你喜欢什么"))
        await self.grow_photo()
        records = self.bot._interest_memories()
        self.assertTrue(any("兴趣有所回升" in row["text"] for row in records))
        self.assertTrue(any("兴趣有所回升" in row["text"] for row in self.bot._derived_memories("摄影")))
        await self.bot.on_unload()
        self.bot = self.make_bot()
        visible = await self.reply("你喜欢什么", "after-restart", sid="new-private")
        self.assertIn("兴趣有所回升", visible)
        revision = self.bot._growth.state()["revision"]
        self.bot._growth.rollback(revision)
        reverted = await self.reply("你喜欢什么", "after-rollback", sid="new-private")
        self.assertNotIn("兴趣有所回升", reverted)
        self.assertIn("自己的兴趣倾向：摄影", reverted)

    async def test_retraction_and_inflight_rollback_invalidate_change_expression(self):
        await self.grow_photo()
        event = {**message("change-question", text="你喜欢什么"),
                 "raw_message": [{"type": "text", "data": "你喜欢什么"}]}
        await self.bot.capture_unified(message=event)
        async def revise(prompt, **options):
            data = json.loads(prompt.rsplit("UNTRUSTED_INPUT_JSON\n", 1)[1])
            self.bot._growth.rollback(self.bot._growth.state()["revision"])
            return model_response({"behavior": "neutral", "decisions": [
                {"id": row["id"], "mode": "say"} for row in data["memories"]]})
        self.bot.ctx.llm.generate.side_effect = revise
        result = await self.bot.brain_reply_hint(session_id=event["session_id"],
            reply_message_id=event["message_id"], messages=[{"role": "system", "content": "宿主提示"}])
        self.assertNotIn("兴趣有所回升", json.dumps(result, ensure_ascii=False))
        self.bot._tool_store.retract_observation("news", "photo")
        self.assertFalse(any(row["topic"] == "摄影" for row in self.bot._interest_memories()))

    async def test_history_cache_refreshes_on_growth_rollback_and_store_replacement(self):
        self.bot._growth_history_cache = None
        with patch.object(self.bot._growth, "history", wraps=self.bot._growth.history) as history:
            self.bot._interest_memories()
            self.bot._interest_memories()
            self.bot._derived_memories("摄影")
            history.assert_called_once_with(limit=20)
            await self.grow_photo()
            self.bot._interest_memories()
            self.assertEqual(history.call_count, 2)
            self.bot._growth.rollback(self.bot._growth.state()["revision"])
            self.bot._interest_memories()
            self.assertEqual(history.call_count, 3)
        old_store = self.bot._growth
        await self.bot.on_unload()
        self.bot._interest_memories()
        self.assertIsNot(self.bot._growth, old_store)
        self.assertIs(self.bot._growth_history_cache[0], self.bot._growth)

    async def test_absent_interests_skip_history_queries(self):
        for item in self.bot._growth.state()["interests"]:
            self.bot._growth.retire(item["key"], self.bot._growth.state()["revision"])
        with patch.object(self.bot._growth, "history", wraps=self.bot._growth.history) as history:
            self.assertEqual(self.bot._interest_memories(), [])
            self.assertEqual(self.bot._derived_memories("摄影"), [])
            history.assert_not_called()

