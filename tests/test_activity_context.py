"""Finite questions and fixed self-reporting cannot invent activity or fatigue."""
import unittest

from MaiLivingWorld.activity_context import activity_question, activity_hint, load_view


class ActivityContextTests(unittest.TestCase):
    def test_complete_self_questions_only(self):
        for text in ("你在做什么？", "你现在在忙什么", "你的进展怎么样！", " 你刚才在忙什么 "):
            self.assertTrue(activity_question(text), text)
        for text in ("进展怎么样", "你在做什么，帮我看看", "他问你在做什么", "“你在做什么”", "你在做什么\n",
                     "如果你在忙什么", "/你在做什么", "你在做什么\x00", None, {}):
            self.assertFalse(activity_question(text), text)

    def test_unknown_activity_never_claims_recent_work_even_while_recovering(self):
        text = activity_hint(recovering=True)
        self.assertIn("没有可核验", text)
        self.assertNotIn("近期实际处理过", text)
        self.assertNotIn("正在进行这一步", text)

    def test_progress_and_interruption_cannot_become_full_completion(self):
        self.assertIn("不代表整项工作完成", activity_hint("project", "progress"))
        self.assertIn("不能说已经完成", activity_hint("reading", "interrupted"))
        self.assertIn("不能说现在仍在进行", activity_hint("research", "unfinished"))
        self.assertIn("当前对话照常", activity_hint("reading", "progress", recovering=True))

    def test_load_projection_rejects_invalid_numeric_values_without_overflow(self):
        for value in (True, None, [], "1", float("nan"), float("inf"), 10**400, -1):
            for key in ("load", "at"):
                with self.assertRaises(ValueError):
                    load_view({"load": .25, "at": 100, key: value}, 101)
        self.assertEqual(load_view({"load": .25, "at": 100}, 90)["load"], .25)
        self.assertEqual(load_view({"load": .25, "at": 100}, 3700)["load"], 0)
