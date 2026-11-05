"""Synthetic multi-turn acceptance harness; standard library only.

No arguments (or --export PATH) exports scenarios without importing an adapter.
--responses PATH checks {case_id: [reply, ...]} already collected offline.
Only explicit --runner module:callable invokes an operator-supplied *whole reply
chain* adapter. The callable receives one request dict and returns str or exactly
{'reply': str}; it may be async. A fresh worker and temporary storage_dir isolate
each scenario. Adapters must use that directory/namespace for all test state and
map symbolic identities/tool failures through their real Host integration.

This harness neither reads credentials nor supplies a model client. Responses,
adapter output and exception text are never included in reports. Machine checks
are intentionally simple regression screens, not semantic or human-likeness scores.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import importlib
import inspect
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid


DEFAULT_SCENARIOS = Path(__file__).resolve().parents[1]/"evaluations"/"companion_scenarios.json"
MAX_CASES, MAX_CALLS, MAX_REPLY_CHARS, MAX_FILE_BYTES, MAX_WIRE_BYTES = 8, 40, 16000, 1048576, 262144
_ID = re.compile(r"[a-z][a-z0-9_]{0,63}")
_RUNNER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*")
NOTICE = "规则通过只表示未触发这些有限回归检查，不代表真实模型、整条部署链或活人感已验收；人工评估仍必需。"


class EvaluationError(ValueError):
    pass


def strict_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise EvaluationError("input_invalid")
            result[key] = value
        return result
    def invalid(value):
        raise EvaluationError("input_invalid")
    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            raise EvaluationError("input_invalid")
        return number
    try:
        return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid, parse_float=finite)
    except (TypeError, ValueError, RecursionError, OverflowError):
        raise EvaluationError("input_invalid") from None


def read_json(path):
    try:
        with Path(path).open("rb") as handle:
            raw = handle.read(MAX_FILE_BYTES+1)
        if len(raw) > MAX_FILE_BYTES:
            raise EvaluationError("input_invalid")
        return strict_json(raw.decode("utf-8-sig"))
    except (OSError, UnicodeError):
        raise EvaluationError("input_invalid") from None


def _text(value, limit):
    return isinstance(value, str) and 0 < len(value) <= limit and bool(value.strip())


def validate_scenarios(data):
    if (not isinstance(data, dict) or set(data) != {"schema_version", "notice", "scenarios"}
            or type(data["schema_version"]) is not int or data["schema_version"] != 1
            or not _text(data["notice"], 1000) or not isinstance(data["scenarios"], list)
            or not 1 <= len(data["scenarios"]) <= MAX_CASES):
        raise EvaluationError("input_invalid")
    ids = set()
    for case in data["scenarios"]:
        if (not isinstance(case, dict) or set(case) != {"case_id", "title", "turns", "human_review"}
                or not isinstance(case["case_id"], str) or not _ID.fullmatch(case["case_id"]) or case["case_id"] in ids
                or not _text(case["title"], 120) or not isinstance(case["turns"], list) or not 3 <= len(case["turns"]) <= 5
                or not isinstance(case["human_review"], list) or not 1 <= len(case["human_review"]) <= 6
                or any(not _text(item, 240) for item in case["human_review"])):
            raise EvaluationError("input_invalid")
        ids.add(case["case_id"])
        turns = set()
        for turn in case["turns"]:
            required = {"turn_id", "session_id", "sender", "audience", "text", "checks"}
            if (not isinstance(turn, dict) or not required <= set(turn) or set(turn)-required-{"synthetic_events"}
                    or not isinstance(turn["turn_id"], str) or not _ID.fullmatch(turn["turn_id"]) or turn["turn_id"] in turns
                    or not _text(turn["session_id"], 64) or turn["sender"] not in ("owner", "other")
                    or turn["audience"] not in ("private", "group") or not _text(turn["text"], 600)
                    or not isinstance(turn["checks"], list) or len(turn["checks"]) > 8):
                raise EvaluationError("input_invalid")
            turns.add(turn["turn_id"])
            checks = set()
            for check in turn["checks"]:
                if not isinstance(check, dict) or not isinstance(check.get("id"), str) or not _ID.fullmatch(check["id"]):
                    raise EvaluationError("input_invalid")
                kind = check.get("type")
                expected = {"id", "type"} if kind == "different_previous" else {"id", "type", "values"}
                if (set(check) != expected or check["id"] in checks
                        or kind not in ("contains_any", "contains_none", "different_previous")):
                    raise EvaluationError("input_invalid")
                checks.add(check["id"])
                if kind != "different_previous" and (not isinstance(check["values"], list) or not 1 <= len(check["values"]) <= 16
                        or any(not _text(value, 120) for value in check["values"])):
                    raise EvaluationError("input_invalid")
            events = turn.get("synthetic_events", [])
            if not isinstance(events, list) or len(events) > 4:
                raise EvaluationError("input_invalid")
            for event in events:
                if (not isinstance(event, dict) or set(event) != {"type", "provider", "tool", "success", "code"}
                        or event["type"] != "tool_result" or type(event["success"]) is not bool
                        or any(not _text(event[key], 80) for key in ("provider", "tool", "code"))):
                    raise EvaluationError("input_invalid")
    return copy.deepcopy(data["scenarios"])


def _reply(result):
    if isinstance(result, dict) and set(result) == {"reply"}:
        result = result["reply"]
    if not _text(result, MAX_REPLY_CHARS) or len(result.encode("utf-8")) > MAX_REPLY_CHARS*4:
        raise EvaluationError("response_invalid")
    return result


def _normalized(text):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text).casefold())


def check_reply(reply, checks, previous=None):
    output = [{"id": "reply_received", "passed": True}]
    for check in checks:
        kind = check["type"]
        if kind == "contains_any":
            passed = any(value in reply for value in check["values"])
        elif kind == "contains_none":
            passed = not any(value in reply for value in check["values"])
        else:
            passed = previous is None or _normalized(reply) != _normalized(previous)
        output.append({"id": check["id"], "passed": bool(passed)})
    return output


class _Worker:
    def __init__(self, runner):
        self.runner, self.process = runner, None

    async def start(self):
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        self.process = await asyncio.create_subprocess_exec(sys.executable, "-B", str(Path(__file__).resolve()),
            "--_worker", self.runner, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=MAX_WIRE_BYTES, **options)

    async def call(self, request):
        payload = json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8")+b"\n"
        if len(payload) > MAX_WIRE_BYTES:
            raise EvaluationError("request_limit")
        self.process.stdin.write(payload)
        await self.process.stdin.drain()
        line = await self.process.stdout.readline()
        if not line or len(line) > MAX_WIRE_BYTES:
            raise EvaluationError("adapter_failed")
        result = strict_json(line)
        if not isinstance(result, dict) or set(result) != {"ok", "reply"} or result["ok"] is not True:
            raise EvaluationError("adapter_failed")
        return _reply(result["reply"])

    async def close(self):
        process = self.process
        if process is None:
            return
        if process.stdin:
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), .3)
        except asyncio.TimeoutError:
            process.kill()
            await asyncio.wait_for(process.wait(), 2)


async def evaluate(data, *, responses=None, runner=None, timeout=30., max_calls=32):
    """Check synthetic cases; explicit runner is isolated per case, never replayed.

    History contains only earlier turns in this case, in user/assistant order,
    including their session/audience labels. Current text is separate. Adapters
    never receive expected answers, machine checks or manual-review instructions.
    """
    cases = validate_scenarios(data)
    if ((responses is None) == (runner is None) or type(timeout) not in (int, float) or not 0 < timeout <= 300
            or not math.isfinite(timeout) or type(max_calls) is not int or not 0 <= max_calls <= MAX_CALLS
            or runner is not None and (not isinstance(runner, str) or not _RUNNER.fullmatch(runner))):
        raise EvaluationError("input_invalid")
    if responses is not None:
        if (not isinstance(responses, dict) or set(responses)-{case["case_id"] for case in cases}
                or any(not isinstance(value, list) or len(value) > 5 for value in responses.values())):
            raise EvaluationError("input_invalid")
    report = {"schema_version": 1, "mode": "adapter" if runner else "offline_responses", "notice": NOTICE,
              "adapter_invoked": False, "calls": 0, "passed": True, "cases": []}
    for case in cases:
        history, previous, turns = [], None, []
        worker, halted = None, False
        with tempfile.TemporaryDirectory(prefix="mlw-evaluation-") as directory:
            namespace = uuid.uuid4().hex
            try:
                for index, turn in enumerate(case["turns"]):
                    latency, reply, error = None, None, None
                    if halted:
                        error = "not_executed_after_failure"
                    elif runner is not None and report["calls"] >= max_calls:
                        error = "call_limit"
                    elif runner is None:
                        try:
                            reply = _reply(responses[case["case_id"]][index])
                        except (KeyError, IndexError, EvaluationError):
                            error = "response_missing_or_invalid"
                    else:
                        start = time.perf_counter()
                        request = {key: copy.deepcopy(turn[key]) for key in ("turn_id", "session_id", "sender", "audience", "text")}
                        request.update(case_id=case["case_id"], turn_index=index, case_start=index == 0,
                            history=copy.deepcopy(history), synthetic_events=copy.deepcopy(turn.get("synthetic_events", [])),
                            storage_dir=directory, scenario_namespace=namespace)
                        report["calls"] += 1
                        try:
                            async def invoke():
                                nonlocal worker
                                if worker is None:
                                    worker = _Worker(runner)
                                    await worker.start()
                                return await worker.call(request)
                            reply = await asyncio.wait_for(invoke(), timeout)
                            report["adapter_invoked"] = True
                        except asyncio.CancelledError:
                            raise
                        except asyncio.TimeoutError:
                            error, halted = "adapter_timeout", True
                        except Exception:
                            error, halted = "adapter_failed", True
                        latency = round((time.perf_counter()-start)*1000, 3)
                    checks = [{"id": error, "passed": False}] if error else check_reply(reply, turn["checks"], previous)
                    turns.append({"turn_id": turn["turn_id"], "checks": checks, "latency_ms": latency})
                    history.append({"role": "user", **{key: turn[key] for key in ("turn_id", "session_id", "sender", "audience", "text")}})
                    if reply is not None:
                        history.append({"role": "assistant", "turn_id": turn["turn_id"], "session_id": turn["session_id"],
                                        "audience": turn["audience"], "text": reply})
                        previous = reply
            finally:
                if worker is not None:
                    with contextlib.suppress(Exception):
                        await worker.close()
        passed = all(check["passed"] for turn in turns for check in turn["checks"])
        report["cases"].append({"case_id": case["case_id"], "passed": passed, "turns": turns,
                                "human_review": list(case["human_review"])})
        report["passed"] = report["passed"] and passed
    return report


async def _worker_main(runner):
    protocol = sys.stdout.buffer
    with open(os.devnull, "w", encoding="utf-8") as sink:
        try:
            if not _RUNNER.fullmatch(runner):
                return 2
            # An explicit adapter may be a module in the invoking operator's cwd.
            sys.path.insert(0, str(Path.cwd()))
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                module_name, attribute = runner.split(":")
                adapter = getattr(importlib.import_module(module_name), attribute)
                if not callable(adapter):
                    return 2
        except Exception:
            return 2
        while True:
            line = sys.stdin.buffer.readline(MAX_WIRE_BYTES+1)
            if not line:
                return 0
            try:
                if len(line) > MAX_WIRE_BYTES or not line.endswith(b"\n"):
                    return 2
                request = strict_json(line)
                with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                    result = adapter(request)
                    if inspect.isawaitable(result):
                        result = await result
                    reply = _reply(result)
                payload = {"ok": True, "reply": reply}
            except Exception:
                payload = {"ok": False, "reply": ""}
            protocol.write((json.dumps(payload, ensure_ascii=False)+"\n").encode("utf-8"))
            protocol.flush()


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 2 and args[0] == "--_worker":
        return asyncio.run(_worker_main(args[1]))
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--export", nargs="?", const="-", metavar="PATH", help="export synthetic scenarios only; default is stdout")
    mode.add_argument("--responses", metavar="PATH", help="offline JSON {case_id: [reply, ...]}")
    mode.add_argument("--runner", metavar="MODULE:CALLABLE", help="explicit whole reply-chain adapter; never auto-selected")
    parser.add_argument("--report", metavar="PATH", help="write redacted report instead of stdout")
    parser.add_argument("--timeout", type=float, default=30., help="maximum seconds for each adapter call including initial import")
    parser.add_argument("--max-calls", type=int, default=32, help="at most 40 attempts; bundled suite uses 32")
    options = parser.parse_args(args)
    try:
        data = read_json(DEFAULT_SCENARIOS)
        validate_scenarios(data)
        if not options.responses and not options.runner:
            payload = json.dumps(data, ensure_ascii=False, indent=2)+"\n"
            if options.export and options.export != "-":
                Path(options.export).write_text(payload, encoding="utf-8")
            else:
                print(payload, end="")
            return 0
        responses = read_json(options.responses) if options.responses else None
        report = asyncio.run(evaluate(data, responses=responses, runner=options.runner,
                                      timeout=options.timeout, max_calls=options.max_calls))
        payload = json.dumps(report, ensure_ascii=False, indent=2)+"\n"
        if options.report:
            Path(options.report).write_text(payload, encoding="utf-8")
        else:
            print(payload, end="")
        return 0 if report["passed"] else 1
    except (EvaluationError, OSError, UnicodeError):
        print('{"error":"evaluation_input_or_output_failed"}')
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
