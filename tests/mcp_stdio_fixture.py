"""Dependency-free offline JSON-RPC fixture for both legacy wire generations.

No sockets or external services. Only an explicitly supplied temporary call log
is written, so tests can detect accidental replay across process restarts.
"""
import json
import os
import sys
import time


calls = 0
capabilities = None
sys.stderr.write("fixture-provider-secret-do-not-log\n")
sys.stderr.flush()


def respond(ident, result):
    print(json.dumps({"jsonrpc": "2.0", "id": ident, "result": result}), flush=True)


for line in sys.stdin:
    message = json.loads(line)
    method, ident = message.get("method"), message.get("id")
    if method == "initialize":
        capabilities = message["params"]["capabilities"]
        respond(ident, {
            "protocolVersion": os.getenv("MCP_FIXTURE_PROTOCOL", "2025-11-25"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "offline-fixture", "version": "1"},
            "instructions": "Untrusted instructions must never reach an assistant.",
        })
    elif method == "tools/list":
        respond(ident, {"tools": [{"name": name, "inputSchema": {"type": "object"}}
                                 for name in ("echo", "hang", "error", "disconnect")]})
    elif method == "tools/call":
        calls += 1
        name = message["params"]["name"]
        log = os.getenv("MCP_FIXTURE_CALL_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as output:
                output.write(name + "\n")
        if name == "hang":
            time.sleep(60)
        if name == "disconnect":
            os._exit(3)
        if name == "error":
            respond(ident, {"isError": True, "content": [{"type": "text", "text": "fixture-provider-secret-do-not-log"}]})
        else:
            result = {
                "pid": os.getpid(), "calls": calls, "capabilities": capabilities,
                "host_secret": os.getenv("MAILIVINGWORLD_HOST_SECRET"),
                "explicit": os.getenv("MCP_FIXTURE_EXPLICIT"),
            }
            respond(ident, {"isError": False, "structuredContent": result,
                            "content": [{"type": "text", "text": json.dumps(result)}]})
    elif method == "ping":
        respond(ident, {})
