"""HTTP 记忆桥的本机认证、导入任务确认和安全边界回归测试。"""
from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from MaiLivingWorld.memory_bridge import WebUIMemoryBridge, MemoryBridgeError


class FakeWebUI(BaseHTTPRequestHandler):
    status = "completed"
    requests = []

    def log_message(self, *args):
        return

    def reply(self, value, code=200, cookie=False):
        raw = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        if cookie:
            self.send_header("Set-Cookie", "maibot_session=testing-cookie; HttpOnly; Path=/")
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        value = json.loads(raw)
        self.requests.append((self.path, self.headers.get("Cookie"), value))
        if self.path == "/api/webui/auth/verify":
            return self.reply({"valid": value["token"] == "a-secret-admin-token"}, cookie=True)
        if self.path == "/api/webui/memory/import/paste":
            if self.headers.get("Cookie") != "maibot_session=testing-cookie":
                return self.reply({}, 401)
            return self.reply({"success": True, "task": {"task_id": "abc123", "status": "queued"}})
        self.reply({}, 404)

    def do_GET(self):
        self.requests.append((self.path, self.headers.get("Cookie"), None))
        if self.path == "/api/webui/memory/import/tasks/abc123":
            return self.reply({"success": True, "task": {"task_id": "abc123", "status": self.status}})
        self.reply({}, 404)


class MemoryBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeWebUI)
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join()

    def setUp(self):
        FakeWebUI.status = "completed"
        FakeWebUI.requests = []
        self.bridge = WebUIMemoryBridge(port=self.server.server_port, token="a-secret-admin-token")

    def test_complete_marks_success_only_after_status(self):
        response = self.bridge.import_public_story(key="story1", text="公开资讯")
        self.assertTrue(response["success"])
        self.assertEqual(len(FakeWebUI.requests), 3)
        self.assertEqual(FakeWebUI.requests[1][2]["scope_type"], "global")
        self.assertEqual(FakeWebUI.requests[1][2]["dedupe_policy"], "content_hash")
        self.assertEqual(FakeWebUI.requests[2][1], "maibot_session=testing-cookie")

    def test_failed_task_does_not_mark_success(self):
        FakeWebUI.status = "failed"
        with self.assertRaises(MemoryBridgeError):
            self.bridge.import_public_story(key="story1", text="公开资讯")

    def test_bad_token_never_reaches_import_endpoint(self):
        bridge = WebUIMemoryBridge(port=self.server.server_port, token="wrong-admin-token")
        with self.assertRaises(MemoryBridgeError) as ctx:
            bridge.import_public_story(key="story2", text="公开资讯")
        self.assertEqual(len(FakeWebUI.requests), 1)
        self.assertNotIn("wrong-admin-token", str(ctx.exception))

    def test_remote_url_is_rejected(self):
        with self.assertRaises(MemoryBridgeError):
            self.bridge._request("GET", "http://bad.example/")
