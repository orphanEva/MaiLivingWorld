from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, Mock

from MaiLivingWorld.memory_semantics import semantic_queries


class MemorySemanticTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, value, **options):
        self.generate = AsyncMock(return_value={"content": json.dumps(value, ensure_ascii=False)})
        self.reserve = Mock(return_value=True)
        return await semantic_queries("上次求职的时候", generate=self.generate,
            reserve=self.reserve, valid=options.pop("valid", lambda: True), **options)

    async def test_semantic_query_uses_model_and_keeps_phrases_as_internal_candidates(self):
        result = await self.call({"queries": ["面试 招聘 经历", "工作 申请 进展"]})
        self.assertEqual(result, ["面试 招聘 经历", "工作 申请 进展"])
        self.reserve.assert_called_once_with()
        self.generate.assert_awaited_once()
        self.assertEqual(self.generate.call_args.kwargs["task_name"], "unified_memory_semantic_query")
        self.assertIn("上次求职的时候", self.generate.call_args.args[0])

    async def test_semantic_query_rejects_invalid_json_shape_and_control_characters(self):
        for value in ({"queries": "面试"}, {"queries": ["x"] * 4}, {"queries": ["x" * 81]},
                      {"queries": ["x\n秘密"]}, {"queries": [42]}, {"queries": ["x"], "facts": []}):
            with self.subTest(value=value):
                self.assertEqual(await self.call(value), [])

    async def test_semantic_query_deduplicates_and_omits_identical_original(self):
        result = await self.call({"queries": ["上次求职的时候", "面试", " 面试 "]})
        self.assertEqual(result, ["面试"])

    async def test_semantic_query_respects_budget_and_context_before_and_after_request(self):
        generate = AsyncMock(return_value={"content": '{"queries":["面试"]}'})
        reserve = Mock(return_value=False)
        self.assertEqual(await semantic_queries("求职", generate=generate, reserve=reserve, valid=lambda: True), [])
        generate.assert_not_called()
        reserve.reset_mock()
        self.assertEqual(await semantic_queries("求职", generate=generate, reserve=reserve, valid=lambda: False), [])
        reserve.assert_not_called()
        valid = Mock(side_effect=[True, True, False, False])
        self.assertEqual(await semantic_queries("求职", generate=generate, reserve=lambda: True, valid=valid), [])
        generate.assert_awaited_once()

    async def test_semantic_query_falls_back_on_timeout_failure_and_propagates_cancellation(self):
        for failure in (TimeoutError(), RuntimeError("model unavailable")):
            self.assertEqual(await semantic_queries("求职", generate=AsyncMock(side_effect=failure),
                reserve=lambda: True, valid=lambda: True), [])
        with self.assertRaises(asyncio.CancelledError):
            await semantic_queries("求职", generate=AsyncMock(side_effect=asyncio.CancelledError),
                reserve=lambda: True, valid=lambda: True)

    async def test_semantic_query_empty_input_and_invalid_timeout_use_no_model_budget(self):
        generate, reserve = AsyncMock(), Mock(return_value=True)
        for query, timeout in (("", 12), (None, 12), ("查询", float("nan")), ("查询", 0)):
            self.assertEqual(await semantic_queries(query, generate=generate, reserve=reserve,
                valid=lambda: True, timeout=timeout), [])
        reserve.assert_not_called()
        generate.assert_not_called()
