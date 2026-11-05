import json
import unittest
from unittest.mock import AsyncMock

from MaiLivingWorld.advice_model import parse_feedback, target_for, understand_feedback


ITEM = {"id": "advice:item:"+"a"*64, "status": "active", "topic": "学习安排", "advice": "先列一张小清单，再开始一个任务"}
TEXT = "你说的“先列一张小清单”我试了，没有帮助"


def response(data):
    return {"response": json.dumps(data, ensure_ascii=False)}


class AdviceModelTests(unittest.IsolatedAsyncioTestCase):
    def test_ambiguous_pronoun_and_shared_quote_never_choose_most_recent(self):
        self.assertIsNone(target_for("那个有用，我试过了", [ITEM]))
        self.assertEqual(target_for(TEXT, [ITEM]), ITEM)
        self.assertIsNone(target_for(TEXT, [ITEM, {**ITEM, "id": "other", "advice": "先列一张小清单，再休息一会"}]))

    def test_literal_unique_bounded_feedback_required(self):
        self.assertIsNone(parse_feedback(response({"outcome": "helpful", "quote": "非常有帮助"}), TEXT))
        self.assertIsNone(parse_feedback(response({"outcome": [], "quote": "没有帮助"}), TEXT))
        self.assertIsNone(parse_feedback(response({"outcome": "unhelpful", "quote": "没有帮助"}), TEXT+"没有帮助"))

    async def test_two_pass_confirmation_and_boolean_rejection(self):
        for checked, expected in ((True, True), (False, False), (1, False)):
            model = AsyncMock(side_effect=[response({"outcome": "unhelpful", "quote": "我试了，没有帮助"}), response({"supported": checked})])
            result = await understand_feedback(TEXT, ITEM, generate=model, reserve=lambda: True, valid=lambda: True, task_name="test")
            self.assertEqual(bool(result), expected)
            self.assertEqual(model.await_count, 2)

    async def test_ambiguous_or_budget_exhausted_uses_no_model(self):
        model = AsyncMock()
        for text, budget in (("那个有用", True), (TEXT, False)):
            self.assertIsNone(await understand_feedback(text, ITEM, generate=model, reserve=lambda: budget, valid=lambda: True, task_name="test"))
        model.assert_not_awaited()

    async def test_changed_binding_after_first_call_skips_review(self):
        valid = [True]
        async def generate(*args, **kwargs):
            valid[0] = False
            return response({"outcome": "unhelpful", "quote": "我试了，没有帮助"})
        model = AsyncMock(side_effect=generate)
        self.assertIsNone(await understand_feedback(TEXT, ITEM, generate=model, reserve=lambda: True, valid=lambda: valid[0], task_name="test"))
        self.assertEqual(model.await_count, 1)
