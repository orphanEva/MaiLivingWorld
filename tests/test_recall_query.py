"""Retrieval expansion/ranking must preserve provenance and event uncertainty."""
import copy
from datetime import datetime, timezone
import unittest

from MaiLivingWorld.recall_query import expand_query, rank_records


NOW = datetime(2026, 11, 5, 0, 30, tzinfo=timezone.utc).timestamp()


def row(id, text, **metadata):
    return {"id": id, "text": text, "source": "mind", "scope": "private:123456",
            "sensitivity": "personal", "evidence_status": "owner_reported", **metadata}


class RecallQueryTests(unittest.TestCase):
    def test_fixed_expansion_retains_query_and_covers_related_words(self):
        self.assertEqual(expand_query("你好"), "你好")
        for query, term in (("面试怎么样", "应聘"), ("考试", "测验"), ("有些难过", "心情低落"),
                            ("应聘", "面试"), ("心情低落", "难过")):
            result = expand_query(query)
            self.assertTrue(result.startswith(query))
            self.assertIn(term, result)
        self.assertIn("不是讨厌猫", expand_query("我不是讨厌猫"))

    def test_expansion_bounded_deterministic_and_no_external_search(self):
        self.assertEqual(expand_query(None), "")
        self.assertEqual(expand_query("  \n "), "")
        self.assertLessEqual(len(expand_query("面试考试难过"*1000)), 1200)
        self.assertEqual(expand_query("  面试\n考试  "), expand_query("面试 考试"))
        self.assertNotIn("完成", expand_query("终于结束了"))
        self.assertNotIn("抑郁", expand_query("难过"))

    def test_direct_match_outranks_related_words_and_related_outweighs_irrelevant(self):
        rows = [row("unrelated", "今天买了水果"), row("related", "应聘安排在周五"), row("direct", "面试安排在周三")]
        self.assertEqual([r["id"] for r in rank_records(rows, "面试", NOW, "Asia/Shanghai")], ["direct", "related", "unrelated"])
        self.assertEqual([r["id"] for r in rank_records([row("plain", "聊天记录"), row("mood", "心情低落")], "难过", NOW, "UTC")], ["mood", "plain"])

    def test_all_original_fields_references_and_sources_preserved(self):
        rows = [row("group", "测验", source="brain_fact", scope="group:654321", refs=[{"id": "a", "digest": "d"}]),
                row("private", "考试", sensitivity="confidential", score=10, timestamp=1)]
        before = copy.deepcopy(rows)
        ranked = rank_records(rows, "考试", NOW, "UTC")
        self.assertEqual(rows, before)
        self.assertIs(ranked[0], rows[1])
        self.assertIs(ranked[1], rows[0])
        self.assertIsNot(ranked, rows)
        self.assertEqual({r["id"] for r in ranked}, {"group", "private"})

    def test_storage_update_timestamps_do_not_mean_event_happened_today(self):
        rows = [row("old", "面试安排", timestamp=1), row("updated", "面试安排", timestamp=NOW, updated=NOW)]
        self.assertEqual([r["id"] for r in rank_records(rows, "今天面试", NOW, "UTC")], ["old", "updated"])
        rows[1]["text"] = "面试安排；收到于2026-11-05 00:30 UTC：尚未确定日期。"
        self.assertEqual([r["id"] for r in rank_records(rows, "今天面试", NOW, "UTC")], ["old", "updated"])

    def test_actual_event_date_or_explicit_body_date_only_breaks_related_ties(self):
        rows = [row("no-date", "面试安排"), row("dated", "面试安排 2026-11-05"), row("unrelated", "买菜", happened=NOW)]
        self.assertEqual([r["id"] for r in rank_records(rows, "今天面试", NOW, "UTC")], ["dated", "no-date", "unrelated"])
        rows[1] = row("dated", "面试安排", occurred_on="2026-11-05")
        self.assertEqual(rank_records(rows, "今天面试", NOW, "UTC")[0]["id"], "dated")

    def test_relative_query_uses_actual_timezone_without_shifting_sources(self):
        rows = [row("nov4", "面试安排", occurred_on="2026-11-04"), row("nov5", "面试安排", occurred_on="2026-11-05")]
        self.assertEqual(rank_records(rows, "今天面试", NOW, "Asia/Shanghai")[0]["id"], "nov5")
        self.assertEqual(rank_records(rows, "今天面试", NOW, "America/Los_Angeles")[0]["id"], "nov4")
        self.assertEqual(rank_records(rows, "昨天面试", NOW, "Asia/Shanghai")[0]["id"], "nov4")
        self.assertEqual(rank_records(rows, "2026年11月5日面试", NOW, "UTC")[0]["id"], "nov5")

    def test_recent_only_uses_explicit_events_not_updated_or_source_relative_words(self):
        rows = [row("updated", "面试安排", updated=NOW), row("event", "面试安排", event_timestamp=NOW-86400)]
        self.assertEqual(rank_records(rows, "最近面试", NOW, "UTC")[0]["id"], "event")
        # "明天" remains an original relative claim, not a date derived from updated.
        rows = [row("unknown", "面试安排"), row("relative", "明天面试安排", timestamp=NOW-86400)]
        self.assertEqual(rank_records(rows, "今天面试", NOW, "UTC")[0]["id"], "unknown")

    def test_ambiguous_completion_does_not_mutate_status_or_guess_target(self):
        rows = [row("exam", "考试还未开始", status="open"), row("interview", "面试仍在准备", status="open")]
        self.assertEqual(rank_records(rows, "终于结束了", NOW, "UTC"), rows)
        self.assertEqual([r["status"] for r in rows], ["open", "open"])

    def test_stable_ties_empty_queries_and_invalid_bounds(self):
        rows = [row("b", "同样的内容"), row("a", "同样的内容")]
        for query in ("不相关内容", "", None):
            self.assertEqual(rank_records(rows, query, NOW, "UTC"), rows)
        for invalid in (None, [None], rows*501):
            with self.assertRaises(ValueError):
                rank_records(invalid, "面试", NOW, "UTC")
        for now, tz in ((True, "UTC"), (float("nan"), "UTC"), (10**1000, "UTC"), (NOW, "not/a/zone"),
                        (datetime(2026, 11, 5), "UTC")):
            with self.assertRaises(ValueError):
                rank_records(rows, "面试", now, tz)


if __name__ == "__main__":
    unittest.main()
