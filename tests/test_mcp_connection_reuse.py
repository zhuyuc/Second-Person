"""MCP HTTP transport keeps one client for its connection lifetime."""
from __future__ import annotations

import asyncio

from connectors.mcp_client import MCPClient


class _Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {"jsonrpc": "2.0", "result": {"ok": True}}


class _Client:
    is_closed = False

    def __init__(self):
        self.calls = []
        self.closed = False

    async def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return _Response()

    async def aclose(self):
        self.closed = True
        self.is_closed = True


def test_http_client_is_reused_and_closed():
    async def scenario():
        client = MCPClient("http", {"url": "https://mcp.example.test"})
        fake = _Client()
        client._http_client = fake
        await client._rpc("tools/list", {})
        await client._rpc("tools/call", {"name": "read"})
        assert len(fake.calls) == 2
        await client.disconnect()
        assert fake.closed is True
    asyncio.run(scenario())
