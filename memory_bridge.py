"""通过 MaiBot 官方 WebUI 文本导入任务写入 A_Memorix（仅公开见闻）。

仅连接 127.0.0.1；管理 token 只能从环境变量获取；确认完成才标记成功。
"""
from __future__ import annotations

import hashlib
import http.client
import json
import time
from http.cookies import SimpleCookie
from typing import Any


class MemoryBridgeError(RuntimeError):
    """不包含秘密信息的明确错误。"""


class WebUIMemoryBridge:
    def __init__(self, *, port: int, token: str, timeout: float = 8.0):
        if not (1 <= int(port) <= 65535):
            raise MemoryBridgeError("WebUI 端口超出范围")
        if not token or len(token) < 10:
            raise MemoryBridgeError("未配置有效的 MLW_WEBUI_TOKEN 环境变量")
        self.port = int(port)
        self.token = token
        self.timeout = min(15.0, max(1.0, float(timeout)))

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None,
                 cookie: str = "") -> tuple[dict[str, Any], str]:
        if not path.startswith("/api/webui/") or "://" in path:
            raise MemoryBridgeError("非法 WebUI API 路径")
        headers: dict[str, str] = {"Accept": "application/json"}
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise MemoryBridgeError("WebUI API 响应过大")
            if response.status != 200:
                raise MemoryBridgeError(f"WebUI API HTTP {response.status}")
            try:
                data = json.loads(raw)
            except (UnicodeError, ValueError) as exc:
                raise MemoryBridgeError("WebUI API 返回非 JSON") from exc
            if not isinstance(data, dict):
                raise MemoryBridgeError("WebUI API 返回结构错误")
            return data, response.getheader("Set-Cookie", "")
        except (OSError, http.client.HTTPException) as exc:
            raise MemoryBridgeError(f"WebUI 本机连接失败：{type(exc).__name__}") from exc
        finally:
            conn.close()

    def _login(self) -> str:
        data, raw_cookie = self._request(
            "POST", "/api/webui/auth/verify", {"token": self.token}
        )
        if data.get("valid") is not True:
            raise MemoryBridgeError("WebUI Token 验证失败")
        parsed = SimpleCookie()
        try:
            parsed.load(raw_cookie)
        except Exception as exc:
            raise MemoryBridgeError("无法解析 WebUI 登录 Cookie") from exc
        if "maibot_session" not in parsed:
            raise MemoryBridgeError("WebUI 未返回 maibot_session Cookie")
        value = parsed["maibot_session"].value
        if not value or "\r" in value or "\n" in value:
            raise MemoryBridgeError("WebUI 登录 Cookie 不合法")
        return "maibot_session=" + value

    def import_public_story(self, *, key: str, text: str, max_wait_seconds: float = 45.0) -> dict[str, Any]:
        if not key or not text.strip() or len(text) > 6000:
            raise MemoryBridgeError("记忆文本为空或超长")
        cookie = self._login()
        name = "living_world_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24] + ".txt"
        data, _ = self._request("POST", "/api/webui/memory/import/paste", {
            "name": name,
            "content": text,
            "input_mode": "text",
            "scope_type": "global",
            "dedupe_policy": "content_hash",
            "llm_enabled": False,
        }, cookie)
        task = data.get("task")
        if data.get("success") is not True or not isinstance(task, dict):
            raise MemoryBridgeError("A_Memorix 拒绝创建记忆导入任务")
        task_id = str(task.get("task_id") or "")
        if not task_id or len(task_id) > 100 or not task_id.isalnum():
            raise MemoryBridgeError("记忆导入任务 ID 不合法")
        deadline = time.monotonic() + min(max(5, max_wait_seconds), 120)
        while time.monotonic() < deadline:
            result, _ = self._request(
                "GET", f"/api/webui/memory/import/tasks/{task_id}", cookie=cookie
            )
            current = result.get("task")
            if not isinstance(current, dict):
                raise MemoryBridgeError("无法查询记忆导入任务状态")
            status = str(current.get("status") or "")
            if status == "completed":
                return {"success": True, "task_id": task_id, "status": status}
            if status in {"completed_with_errors", "failed", "cancelled"}:
                raise MemoryBridgeError(f"记忆导入任务未成功：{status}")
            if status not in {"queued", "preparing", "running", "cancel_requested"}:
                raise MemoryBridgeError(f"未知记忆导入任务状态：{status[:40]}")
            time.sleep(1.0)
        raise MemoryBridgeError("记忆导入任务仍在排队/处理中，请在 WebUI 中检查任务进度")
