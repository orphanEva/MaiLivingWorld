from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from MaiLivingWorld.tool_store import ToolStore
from MaiLivingWorld.growth_store import GrowthStore, source_fingerprint
from MaiLivingWorld.growth_model import parse_growth, empty_growth
from MaiLivingWorld.unified_memory import collect_memories, resolve_memories, derived_memories


class ToolMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.tools = ToolStore(self.root / "tool_memory.sqlite3")
        self.growth = GrowthStore(self.root / "growth.sqlite3")

    def tearDown(self):
        self.tools.close()
        self.growth.close()
        self.temp.cleanup()

    def row(self, key="illust:123", scope="private:12345678", text="风景绘画的光线与色彩", now=100):
        return self.tools.put_observation("pixiv", key, text, scope, subject_id="12345678", now=now)

    def test_tool_observations_share_global_recall_without_losing_provenance(self):
        private = self.row()
        group = self.row("illust:124", "group:98765432")
        rows = collect_memories(tools=self.tools, query="绘画", now=101)
        self.assertEqual({row["id"] for row in rows}, {private["id"], group["id"]})
        self.assertEqual({row["scope"] for row in rows}, {"private:12345678", "group:98765432"})
        self.assertTrue(all(row["sensitivity"] == "personal" for row in rows))
        self.assertTrue(all(row["evidence_status"] == "tool_observation" for row in rows))

    def test_exact_resolution_uses_the_identical_source_fingerprint(self):
        original = self.row()
        current = resolve_memories([original["id"]], tools=self.tools, now=101)[original["id"]]
        self.assertEqual(source_fingerprint(original), source_fingerprint(current))
        self.assertEqual(current["origin_key"], original["origin_key"])

    def test_retrieval_prioritizes_old_relevant_observations_and_stays_readonly(self):
        relevant = self.row(text="二分查找可以减少检索次数", now=1)
        for index in range(15):
            self.row("new:"+str(index), text="今天分享的风景画", now=100+index)
        before = self.tools.con.total_changes
        statements = []
        self.tools.con.set_trace_callback(statements.append)
        rows = collect_memories(tools=self.tools, query="二分查找", limit=1, scan_limit=2, now=120)
        resolved = resolve_memories([relevant["id"]], tools=self.tools, now=120)
        self.assertEqual(rows[0]["id"], relevant["id"])
        self.assertIn(relevant["id"], resolved)
        self.assertEqual(self.tools.con.total_changes, before)
        self.assertTrue(all(sql.startswith("SELECT ") for sql in statements))

    def test_changed_or_retracted_source_suspends_derived_growth(self):
        original = self.row()
        proposal = {"knowledge": [{"topic": "绘画光线", "statement": "不同光线下的色彩需要分别观察",
                                   "confidence": .6, "evidence_ids": [original["id"]]}]}
        job_id = self.growth.enqueue([original], now=100)
        job = self.growth.claim(now=100)
        self.growth.complete(job_id, job["token"], proposal, [original], now=101)
        state = self.growth.state(now=102)
        resolved = resolve_memories([original["id"]], tools=self.tools)
        self.assertEqual(len(derived_memories(state, resolved, fingerprint=source_fingerprint)), 1)
        self.row(text="已更正的光线与色彩资料", now=103)
        resolved = resolve_memories([original["id"]], tools=self.tools)
        self.assertEqual(derived_memories(state, resolved, fingerprint=source_fingerprint), [])
        self.tools.retract_observation("pixiv", "illust:123", now=104)
        self.assertEqual(resolve_memories([original["id"]], tools=self.tools), {})
        self.assertEqual(collect_memories(tools=self.tools), [])

    def test_repeated_tool_read_cannot_reapply_scalar_growth(self):
        row = self.row()
        proposal = {"interests": [{"topic": "绘画光线", "delta": .1, "evidence_ids": [row["id"]]}]}
        jid = self.growth.enqueue([row], now=100)
        job = self.growth.claim(now=100)
        self.growth.complete(jid, job["token"], proposal, [row], now=101)
        self.assertEqual(self.growth.enqueue([self.row(now=105)], now=105), jid)
        changed = self.row(text="新增的光线资料", now=106)
        new_id = self.growth.enqueue([changed], now=106)
        job = self.growth.claim(now=106)
        result = self.growth.complete(new_id, job["token"], proposal, [changed], now=107)
        self.assertFalse(result["changed"])
        self.assertEqual(self.growth.state(now=107)["interests"][0]["weight"], .1)

    def test_growth_model_accepts_original_tools_but_not_growth_derived_records(self):
        row = self.row()
        proposal = empty_growth()
        proposal["interests"] = [{"topic": "绘画光线", "delta": .03, "evidence_ids": [row["id"]]}]
        self.assertIsNotNone(parse_growth(json.dumps(proposal, ensure_ascii=False), [row]))
        self.assertIsNone(parse_growth(json.dumps(proposal, ensure_ascii=False), [{**row, "source": "growth"}]))

    def test_malformed_tool_ids_never_trigger_a_query(self):
        for identity in ("tool:observation:bad", "tool:call:"+"a"*64, "tool:observation:"+"A"*64):
            with self.assertRaises(ValueError):
                resolve_memories([identity], tools=self.tools)


if __name__ == "__main__":
    unittest.main()
