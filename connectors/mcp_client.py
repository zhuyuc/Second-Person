"""
MCP 客户端（产品文档 §外部系统连接器 / 开发文档 §6.3 MCP 连接）。

- 传输：stdio（本地子进程）+ Streamable HTTP（远程）
- JSON-RPC 2.0：initialize / tools/list / tools/call
- stdio：Gateway 以子进程启动并持有生命周期，进程退出即标记不可用
- HTTP：endpoint URL + 认证头，连接后 tools/list 注册
- 不做后台心跳，调用时判断可用性
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

logger = logging.getLogger("second_person.mcp")


class MCPError(RuntimeError):
    pass


class MCPClient:
    """单个 MCP Server 连接。"""

    def __init__(self, transport: str, config: dict[str, Any], timeout: int = 120):
        self.transport = transport            # stdio / http
        self.config = config
        self.timeout = timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._req_id = 0

    # ---- 连接 -------------------------------------------------------------
    async def connect(self) -> None:
        if self.transport == "stdio":
            await self._start_stdio()
        # HTTP 无需常驻连接
        await self._initialize()

    async def _start_stdio(self) -> None:
        cmd = self.config["command"]
        args = self.config.get("args", [])
        env = self.config.get("env", {})
        import os
        full_env = {**os.environ, **env}
        self._proc = await asyncio.create_subprocess_exec(
            cmd, *args, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=full_env)

    async def _initialize(self) -> None:
        await self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {}, "clientInfo": {"name": "SecondPerson", "version": "1.0.0"}})

    # ---- JSON-RPC ---------------------------------------------------------
    async def _rpc(self, method: str, params: dict) -> Any:
        self._req_id += 1
        request = {"jsonrpc": "2.0", "id": self._req_id,
                   "method": method, "params": params}
        if self.transport == "stdio":
            return await self._rpc_stdio(request)
        return await self._rpc_http(request)

    async def _rpc_stdio(self, request: dict) -> Any:
        if not self._proc or not self._proc.stdin:
            raise MCPError("stdio 进程未启动")
        line = json.dumps(request) + "\n"
        self._proc.stdin.write(line.encode("utf-8"))
        await self._proc.stdin.drain()
        try:
            raw = await asyncio.wait_for(self._proc.stdout.readline(), timeout=self.timeout)
        except asyncio.TimeoutError as e:
            raise MCPError("MCP stdio 调用超时") from e
        if not raw:
            raise MCPError("MCP stdio 无响应（进程可能已退出）")
        resp = json.loads(raw.decode("utf-8"))
        if "error" in resp:
            raise MCPError(str(resp["error"]))
        return resp.get("result")

    async def _rpc_http(self, request: dict) -> Any:
        url = self.config["url"]
        headers = dict(self.config.get("headers", {}))
        auth = self.config.get("auth", {})
        if auth.get("type") == "api_key":
            headers[auth.get("header", "Authorization")
                    ] = auth.get("value", "")
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(url, json=request, headers=headers)
            r.raise_for_status()
            resp = r.json()
        if "error" in resp:
            raise MCPError(str(resp["error"]))
        return resp.get("result")

    # ---- 工具 -------------------------------------------------------------
    async def list_tools(self) -> list[dict]:
        result = await self._rpc("tools/list", {})
        return result.get("tools", []) if result else []

    async def call_tool(self, name: str, arguments: dict) -> Any:
        result = await self._rpc("tools/call", {"name": name, "arguments": arguments})
        # MCP 返回 content 数组
        if isinstance(result, dict) and "content" in result:
            texts = [c.get("text", "")
                     for c in result["content"] if c.get("type") == "text"]
            return "\n".join(texts) if texts else result
        return result

    async def disconnect(self) -> None:
        if self._proc:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                self._proc.kill()
            self._proc = None

    def is_alive(self) -> bool:
        if self.transport == "http":
            return True
        return self._proc is not None and self._proc.returncode is None
