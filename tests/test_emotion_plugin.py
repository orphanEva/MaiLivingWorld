"""Real growth, source validation and coordinated expression across restarts."""
import copy
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from MaiLivingWorld.tests.test_runtime import FakeContext, plugin
from MaiLivingWorld.tests.test_mcp_plugin import OWNER, OTHER, GROUP, message
from MaiLivingWorld.growth_model import empty_growth
from MaiLivingWorld.reply_policy import MARKER


EVENT = "共同阅读了虚构小说霜桥的结局：人物互助的细节充分，但关键转折缺少铺垫。"
APPRECIATION = "欣赏霜桥中人物互助的细节,愿意继续理解他们的选择"
DISAPPOINTMENT = "对霜桥结局的关键转折缺少铺垫有些失望"


def response(value):
    return {"response": json.dumps(value, ensure_ascii=False)}


class EmotionPluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = time.time()
        self.bot = self.make_bot()

    def make_bot(self):
        b = plugin.MaiLivingWorld()
        b.ctx = FakeContext()
        b.ctx.paths = SimpleNamespace(data_dir=Path(self.temp.name))
        b.config = plugin.Config()
        b.config.owner.enabled = b.config.unified_mind.enabled = b.config.brain_memory.enabled = True
        b.config.owner.qq = OWNER
        b.config.unified_mind.mode = "active"
        b.config.unified_mind.min_interval_seconds = 0
        b.config.unified_mind.max_daily_model_calls = 100
        b.config.growth.enabled = b.config.growth.emotional_episodes = True
        b.config.growth.mode = "active"
        b.config.growth.interval_seconds = 60
        b.config.attention.enabled = b.config.attention.reply_coordination = True
        b.config.brain_memory.private_users = [OTHER]
        b.config.brain_memory.group_ids = [GROUP]
        return b

    async def asyncTearDown(self):
        await self.bot.on_unload()
        self.temp.cleanup()

    async def learn(self, *, during_proposal=None):
        brain = self.bot._ensure_brain()
        with brain.con:
            brain.con.execute("INSERT INTO brain_events(id,scope,actor_id,kind,summary,importance,happened,created) VALUES (?,?,?,'experience',?,3,?,?)",
                ("emotion-real-event", "private:"+OWNER, OWNER, EVENT, self.now, self.now))
        ident = "brain:event:emotion-real-event"
        proposal = empty_growth(emotional_episodes=True)
        proposal["emotions"] = [{"topic": "霜桥", "feeling": feeling, "appraisal": appraisal, "intensity": .5,
            "evidence_ids": [ident], "quotes": [{"source_id": ident, "quote": EVENT}]}
            for feeling, appraisal in (("appreciative", APPRECIATION), ("disappointed", DISAPPOINTMENT))]
        approved = {key: list(range(len(value))) if isinstance(value, list) else bool(value)
                    for key, value in proposal.items()}
        calls = 0
        async def model(*args, **kw):
            nonlocal calls
            calls += 1
            if calls == 1:
                if during_proposal:
                    during_proposal()
                return response(proposal)
            return response({"approved": approved})
        self.bot.ctx.llm.generate.side_effect = model
        with patch("time.time", return_value=self.now):
            await self.bot._grow()
        return ident

    async def reply(self, text="再聊聊霜桥的结局", mid="current", *, advance=0, **kw):
        event = message(mid=mid, text=text, **kw)
        with patch("time.time", return_value=self.now):
            await self.bot.capture_owner(message=event)
            await self.bot.capture_unified(message=event)
            request = {"session_id": event["session_id"], "reply_message_id": mid,
                       "messages": [{"role": "system", "content": "宿主提示"}]}
            rows = self.bot._collect_unified(text)
        async def expression(*args, **kwargs):
            self.now += advance
            return response({"behavior": "neutral", "decisions": [{"id": row["id"], "mode": "say"} for row in rows]})
        self.bot.ctx.llm.generate.side_effect = expression
        with patch("time.time", side_effect=lambda: self.now):
            output = await self.bot.brain_reply_hint(**request)
        return json.dumps(output, ensure_ascii=False)

    def emotions(self):
        return self.bot._growth.state(now=self.now).get("emotions", [])

    async def test_mixed_reactions_survive_restart_and_flow_through_expression(self):
        await self.learn()
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 2)
        self.assertEqual({row["feeling"] for row in self.emotions()}, {"appreciative", "disappointed"})
        result = await self.reply(advance=1)
        self.assertIn(APPRECIATION, result)
        self.assertIn(DISAPPOINTMENT, result)
        self.assertIn("self_emotion", result)
        self.assertIn(MARKER, result)
        self.assertNotIn(APPRECIATION, self.bot._growth_style_hint())
        original = copy.deepcopy(self.emotions())
        await self.bot.on_unload()
        self.bot = self.make_bot()
        self.bot._ensure_growth()
        self.assertEqual(self.emotions(), original)
        result = await self.reply(mid="restart", sid="new-private", advance=1)
        self.assertIn(APPRECIATION, result)
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 1)

    async def test_reaction_question_reuses_mentioned_source_and_its_grounded_emotions(self):
        await self.learn()
        self.bot.config.reference.enabled = True
        await self.reply()
        with patch("time.time", return_value=self.now):
            await self.bot.reference_send_feedback(sent=True, message=message(
                mid="reaction-topic-sent", uid="99999999", text=EVENT))
        self.assertTrue(self.bot._reference.store.state(OWNER)["active"])
        await self.bot.on_unload()
        self.bot = self.make_bot()
        self.bot.config.reference.enabled = True

        async def expression(prompt, **options):
            data = json.loads(prompt.rsplit("UNTRUSTED_INPUT_JSON\n", 1)[1])
            return response({"behavior": "neutral", "decisions": [
                {"id": row["id"], "mode": "say"} for row in data["memories"]]})
        self.bot.ctx.llm.generate.side_effect = expression
        for enabled, mid in ((True, "reaction-current"), (False, "reaction-without-episodes")):
            self.bot.config.growth.emotional_episodes = enabled
            event = message(mid=mid, text="你觉得它哪里有意思？", sid="new-private")
            event["raw_message"] = [{"type": "text", "data": event["processed_plain_text"]}]
            with patch("time.time", return_value=self.now):
                await self.bot.capture_unified(message=event)
                output = await self.bot.brain_reply_hint(
                    session_id="new-private", reply_message_id=mid,
                    messages=[{"role": "system", "content": "宿主提示"}])
            visible = json.dumps(output, ensure_ascii=False)
            self.assertIn(EVENT, visible)
            self.assertIn("自己的判断", visible)
            self.assertEqual(APPRECIATION in visible, enabled)
            self.assertEqual(DISAPPOINTMENT in visible, enabled)
            self.assertEqual("self_emotion" in visible, enabled)

    async def test_private_reactions_do_not_become_group_or_other_person_facts(self):
        await self.learn()
        for mid, args in (("group", {"group": GROUP, "sid": "group-one"}),
                          ("other", {"uid": OTHER, "sid": "other-private"})):
            result = await self.reply(mid=mid, **args)
            self.assertNotIn(APPRECIATION, result)
            self.assertNotIn(DISAPPOINTMENT, result)

    async def test_source_change_and_feature_disable_stop_emotion_expression(self):
        await self.learn()
        self.bot.config.growth.emotional_episodes = False
        with patch("time.time", return_value=self.now):
            self.assertNotIn("emotions", self.bot._growth_snapshot())
            self.assertFalse(any(row["kind"] == "emotion" for row in self.bot._derived_memories("霜桥")))
        self.assertEqual(len(self.emotions()), 2)
        self.bot.config.growth.emotional_episodes = True
        with self.bot._brain.con:
            self.bot._brain.con.execute("UPDATE brain_events SET summary=? WHERE id=?", ("原始阅读事件已更正", "emotion-real-event"))
        with patch("time.time", return_value=self.now):
            self.assertEqual(self.bot._growth_snapshot()["emotions"], [])
        result = await self.reply(mid="changed")
        self.assertNotIn(APPRECIATION, result)

    async def test_hot_disable_during_growth_review_cannot_commit_episodes(self):
        await self.learn(during_proposal=lambda: setattr(self.bot.config.growth, "emotional_episodes", False))
        self.assertEqual(self.emotions(), [])
        self.assertEqual(self.bot.ctx.llm.generate.await_count, 1)

    async def test_decay_and_unrelated_question_do_not_reanimate_old_reactions(self):
        await self.learn()
        result = await self.reply("明天的天气如何", "unrelated")
        self.assertNotIn(APPRECIATION, result)
        self.now += 7*86400+1
        self.assertEqual(self.emotions(), [])
        result = await self.reply(mid="expired")
        self.assertNotIn(APPRECIATION, result)


if __name__ == "__main__":
    unittest.main()
