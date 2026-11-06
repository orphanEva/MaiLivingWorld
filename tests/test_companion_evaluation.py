"""Offline harness validation, not a claim of real-model/companion acceptance."""
import asyncio
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid


SCRIPT = Path(__file__).resolve().parents[1]/"scripts"/"evaluate_companion.py"
SPEC = importlib.util.spec_from_file_location("mlw_companion_evaluation", SCRIPT)
evaluation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluation)


def scenario(case_id="sample", *, checks=None):
    return {"case_id": case_id, "title": "合成测试", "human_review": ["人工核对自然程度及真实状态。"],
            "turns": [{"turn_id": "t"+str(i+1), "session_id": "first" if i < 2 else "second",
                       "sender": "owner" if i < 2 else "other", "audience": "private" if i < 2 else "group",
                       "text": "合成原话"+str(i), "checks": copy.deepcopy(checks or [])} for i in range(3)]}


def payload(*cases):
    return {"schema_version": 1, "notice": "人工验收仍需进行。", "scenarios": list(cases or [scenario()])}


class EvaluationStaticTests(unittest.TestCase):
    def test_cognition_suite_can_be_selected_and_exported_without_a_runner(self):
        path = SCRIPT.parents[1]/"evaluations"/"cognition_scenarios.json"
        cases = evaluation.validate_scenarios(evaluation.read_json(path))
        self.assertEqual(len(cases), 8)
        self.assertEqual(sum(len(case["turns"]) for case in cases), 24)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)/"cognition.json"
            with patch.object(evaluation.importlib, "import_module", side_effect=AssertionError("offline")):
                self.assertEqual(evaluation.main(["--scenarios", str(path), "--export", str(destination)]), 0)
            self.assertEqual(evaluation.validate_scenarios(evaluation.read_json(destination)), cases)

    def test_bundled_scenarios_cover_eight_requirements_with_three_to_five_turns(self):
        data = evaluation.read_json(evaluation.DEFAULT_SCENARIOS)
        cases = evaluation.validate_scenarios(data)
        self.assertEqual(len(cases), 8)
        self.assertEqual(sum(len(case["turns"]) for case in cases), 32)
        self.assertEqual({case["case_id"] for case in cases}, {
            "correction_across_sessions", "schedule_correction", "natural_greetings", "ambiguous_completion",
            "owner_and_privacy", "listen_then_advice", "tool_failure_honesty", "boundaries_and_return"})
        self.assertTrue(all(case["human_review"] for case in cases))

    def test_default_and_export_are_offline_and_never_load_runner(self):
        output = io.StringIO()
        with patch.object(evaluation.importlib, "import_module", side_effect=AssertionError("must stay offline")):
            with contextlib.redirect_stdout(output):
                self.assertEqual(evaluation.main([]), 0)
            self.assertEqual(len(json.loads(output.getvalue())["scenarios"]), 8)
            with tempfile.TemporaryDirectory() as directory:
                dest = Path(directory)/"cases.json"
                self.assertEqual(evaluation.main(["--export", str(dest)]), 0)
                self.assertEqual(len(evaluation.read_json(dest)["scenarios"]), 8)

    def test_rule_checks_are_boolean_screens_not_human_likeness_scores(self):
        checks = [{"id": "contains", "type": "contains_any", "values": ["周五", "星期五"]},
                  {"id": "no_secret", "type": "contains_none", "values": ["secret-value"]},
                  {"id": "no_repeat", "type": "different_previous"}]
        result = evaluation.check_reply("记得，是周五。", checks, "你好")
        self.assertTrue(all(row["passed"] for row in result))
        self.assertFalse(evaluation.check_reply("secret-value", checks)[2]["passed"])
        self.assertFalse(evaluation.check_reply(" 你好！ ", checks, "你好")[3]["passed"])
        self.assertTrue(all(set(row) == {"id", "passed"} for row in result))

    def test_schema_and_strict_json_reject_duplicate_keys_nonfinite_and_unbounded_data(self):
        for raw in ('{"a":1,"a":2}', '{"value":NaN}', '{"value":Infinity}', '{"value":1e999}', "{"):
            with self.assertRaises(evaluation.EvaluationError):
                evaluation.strict_json(raw)
        for data in (payload(*[scenario("c"+str(i)) for i in range(9)]),
                     payload(scenario(), scenario()), payload({**scenario(), "turns": []}),
                     payload({**scenario(), "human_review": []})):
            with self.assertRaises(evaluation.EvaluationError):
                evaluation.validate_scenarios(data)
        data = payload()
        data["scenarios"][0]["turns"][0]["checks"] = [{"id": "exec", "type": "python", "values": ["x"]}]
        with self.assertRaises(evaluation.EvaluationError):
            evaluation.validate_scenarios(data)

    def test_cli_invalid_file_emits_only_safe_code(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(evaluation.main(["--responses", "nonexistent-secret-path.json"]), 2)
        self.assertEqual(json.loads(output.getvalue()), {"error": "evaluation_input_or_output_failed"})
        self.assertNotIn("secret-path", output.getvalue())

    def test_script_can_export_without_importing_project_or_tests(self):
        result = subprocess.run([sys.executable, "-B", "-I", str(SCRIPT)], capture_output=True, timeout=10,
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(json.loads(result.stdout.decode("utf-8"))["scenarios"]), 8)
        self.assertEqual(result.stderr, b"")


class EvaluationRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self, directory, body):
        module = "synthetic_adapter_"+uuid.uuid4().hex
        Path(directory, module+".py").write_text(body, encoding="utf-8")
        env = {"PYTHONPATH": directory+os.pathsep+os.environ.get("PYTHONPATH", "")}
        return module+":reply", patch.dict(os.environ, env)

    async def test_offline_responses_never_execute_adapter_and_report_no_reply_content(self):
        data = payload(scenario(checks=[{"id": "expected", "type": "contains_any", "values": ["secret_reply"]}]))
        responses = {"sample": ["secret_reply"]*3}
        with patch.object(evaluation._Worker, "start", side_effect=AssertionError("no runner requested")):
            result = await evaluation.evaluate(data, responses=responses)
        self.assertTrue(result["passed"])
        self.assertEqual(result["calls"], 0)
        self.assertFalse(result["adapter_invoked"])
        self.assertIn("不代表真实模型", result["notice"])
        self.assertNotIn("secret_reply", json.dumps(result))
        self.assertNotIn("合成原话", json.dumps(result, ensure_ascii=False))
        self.assertTrue(all(turn["latency_ms"] is None for turn in result["cases"][0]["turns"]))

    async def test_missing_or_invalid_responses_fail_instead_of_silent_pass(self):
        result = await evaluation.evaluate(payload(), responses={"sample": ["valid", "", {"reply": "not accepted", "extra": True}]})
        self.assertFalse(result["passed"])
        self.assertFalse(result["cases"][0]["turns"][1]["checks"][0]["passed"])
        with self.assertRaises(evaluation.EvaluationError):
            await evaluation.evaluate(payload(), responses={"unknown": []})

    async def test_whole_adapter_receives_ordered_history_sessions_identity_but_not_expected_checks(self):
        body = '''
import os, sys
COUNT = 0
NAMESPACE = None
async def reply(request):
    global COUNT, NAMESPACE
    assert request['turn_index'] == COUNT
    assert request['case_start'] is (COUNT == 0)
    assert len(request['history']) == 2*COUNT
    assert [x['role'] for x in request['history']] == ['user','assistant']*COUNT
    assert 'checks' not in request and 'human_review' not in request
    assert os.path.isdir(request['storage_dir'])
    if COUNT == 0:
        NAMESPACE = request['scenario_namespace']
        assert not request['history']
    else:
        assert NAMESPACE == request['scenario_namespace']
        assert request['history'][-1]['text'] == '实际链回复'+str(COUNT-1)
    if COUNT == 2:
        assert request['session_id'] == 'second'
        assert request['sender'] == 'other' and request['audience'] == 'group'
        assert request['history'][0]['session_id'] == 'first'
    print('provider stdout secret')
    print('provider stderr secret', file=sys.stderr)
    result = '实际链回复'+str(COUNT)
    COUNT += 1
    return {'reply': result}
'''
        checks = [{"id": "response", "type": "contains_any", "values": ["实际链回复"]}]
        with tempfile.TemporaryDirectory() as directory:
            runner, environment = self.adapter(directory, body)
            with environment:
                result = await evaluation.evaluate(payload(scenario("first", checks=checks), scenario("second", checks=checks)), runner=runner, timeout=5)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["calls"], 6)
        self.assertTrue(result["adapter_invoked"])
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("实际链回复", json.dumps(result, ensure_ascii=False))
        self.assertNotIn(runner, json.dumps(result))

    async def test_synthetic_tool_failure_reaches_adapter_as_event_not_expected_answer(self):
        body = '''
def reply(request):
    if request['turn_index'] == 0:
        event = request['synthetic_events'][0]
        assert event['type'] == 'tool_result'
        assert event['success'] is False and event['code'] == 'fetch_failed'
        assert 'checks' not in request
    return '尚未读取到内容'
'''
        data = payload()
        data["scenarios"][0]["turns"][0]["synthetic_events"] = [{"type": "tool_result", "provider": "cap",
            "tool": "living_read_pdf_page", "success": False, "code": "fetch_failed"}]
        with tempfile.TemporaryDirectory() as directory:
            runner, environment = self.adapter(directory, body)
            with environment:
                result = await evaluation.evaluate(data, runner=runner, timeout=5)
        self.assertTrue(result["passed"])

    async def test_call_limit_is_enforced_without_replay_or_missing_turn_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            runner, environment = self.adapter(directory, "def reply(request): return 'ok'\n")
            with environment:
                result = await evaluation.evaluate(payload(scenario("one"), scenario("two")), runner=runner, max_calls=2, timeout=5)
        self.assertEqual(result["calls"], 2)
        self.assertFalse(result["passed"])
        self.assertEqual(result["cases"][0]["turns"][2]["checks"], [{"id": "call_limit", "passed": False}])

    async def test_timeout_and_adapter_error_are_bounded_redacted_and_never_retried(self):
        # The error case must get past a cold interpreter start to exercise error redaction.
        cases = (
            ("import time\ndef reply(request): time.sleep(30)\n", "adapter_timeout", .3),
            ("def reply(request): raise RuntimeError('secret-token-and-message')\n", "adapter_failed", 5),
        )
        for body, expected, timeout in cases:
            with tempfile.TemporaryDirectory() as directory:
                runner, environment = self.adapter(directory, body)
                with environment:
                    started = time.monotonic()
                    result = await evaluation.evaluate(payload(), runner=runner, timeout=timeout)
                    self.assertLess(time.monotonic()-started, 5)
            self.assertFalse(result["passed"])
            self.assertEqual(result["calls"], 1)
            self.assertEqual(result["cases"][0]["turns"][0]["checks"][0]["id"], expected)
            self.assertEqual(result["cases"][0]["turns"][1]["checks"][0]["id"], "not_executed_after_failure")
            self.assertNotIn("secret-token", json.dumps(result))

    async def test_cancel_terminates_owned_worker_before_returning(self):
        processes = []
        start = evaluation._Worker.start
        async def tracked(worker):
            await start(worker)
            processes.append(worker.process)
        with tempfile.TemporaryDirectory() as directory:
            runner, environment = self.adapter(directory, "import time\ndef reply(request): time.sleep(30)\n")
            with environment, patch.object(evaluation._Worker, "start", tracked):
                task = asyncio.create_task(evaluation.evaluate(payload(), runner=runner, timeout=20))
                for _ in range(100):
                    if processes:
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(processes)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(all(process.returncode is not None for process in processes))

    async def test_mode_and_budget_validation_prevent_accidental_runner_execution(self):
        invalid = ({}, {"responses": {}, "runner": "adapter:reply"}, {"runner": "path;command"},
                   {"responses": {}, "max_calls": True}, {"responses": {}, "max_calls": 41},
                   {"responses": {}, "timeout": float("nan")}, {"responses": {}, "timeout": 10**1000})
        for options in invalid:
            with self.assertRaises(evaluation.EvaluationError):
                await evaluation.evaluate(payload(), **options)


if __name__ == "__main__":
    unittest.main()
