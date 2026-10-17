"""MCP 客户端：与 mcp.mcd.cn 直连，用于价格漂移校验。

只实现三个动作：initialize 握手 → tools/call → 解析（JSON 或 SSE）。
本地求解不依赖本模块；没有 token 时整个项目走 fixtures 回放。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Optional

DEFAULT_URL = "https://mcp.mcd.cn"
PROTOCOL = "2024-11-05"


class McpError(RuntimeError):
    pass


class McpClient:
    def __init__(self, token: Optional[str] = None, url: str = DEFAULT_URL, timeout: float = 15.0):
        self.token = token or os.environ.get("MCD_MCP_TOKEN", "")
        self.url = url
        self.timeout = timeout
        self.session_id: Optional[str] = None
        self._id = 0

    @property
    def available(self) -> bool:
        return bool(self.token)

    # ---------- 传输 ----------
    def _post(self, payload: dict) -> tuple[Optional[dict], dict]:
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.token}",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self.session_id = sid
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise McpError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from e
        except urllib.error.URLError as e:
            raise McpError(f"网络不可达：{e.reason}") from e
        return _parse_body(raw), {"session": self.session_id}

    def _call(self, method: str, params: dict | None = None, notify: bool = False) -> dict:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notify:
            self._id += 1
            payload["id"] = self._id
        data, _ = self._post(payload)
        if data and "error" in data:
            raise McpError(str(data["error"]))
        return data or {}

    # ---------- 协议 ----------
    def connect(self) -> None:
        if not self.available:
            raise McpError("缺少 MCD_MCP_TOKEN 环境变量")
        self._call("initialize", {
            "protocolVersion": PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "mcpair", "version": "0.1.0"},
        })
        self._call("notifications/initialized", {}, notify=True)

    def tools(self) -> list[dict]:
        return self._call("tools/list", {}).get("result", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict | None = None) -> Any:
        res = self._call("tools/call", {"name": name, "arguments": arguments or {}})
        result = res.get("result", {})
        for block in result.get("content", []):
            if block.get("type") == "text":
                text = block.get("text", "")
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
        return result

    # ---------- 业务动作 ----------
    def calculate_price(self, store_code: str, items: list[dict],
                        order_type: int = 1, be_type: int = 1,
                        be_code: str | None = None) -> dict:
        args = {"storeCode": store_code, "orderType": order_type, "beType": be_type,
                "items": items}
        if be_code:
            args["beCode"] = be_code
        return self.call_tool("calculate-price", args)


def _parse_body(raw: str) -> Optional[dict]:
    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith("{") or raw.startswith("["):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    # SSE：逐行取 data:
    for line in raw.splitlines():
        if line.startswith("data:"):
            chunk = line[5:].strip()
            try:
                return json.loads(chunk)
            except json.JSONDecodeError:
                continue
    return None


class TokenBucket:
    """MCP 限制 600 req/min，这里按 480 保守估计。"""

    def __init__(self, capacity: int = 480, window: float = 60.0):
        self.capacity = capacity
        self.window = window
        self._hits: list[float] = []

    def take(self) -> bool:
        now = time.time()
        self._hits = [t for t in self._hits if now - t < self.window]
        if len(self._hits) >= self.capacity:
            return False
        self._hits.append(now)
        return True
