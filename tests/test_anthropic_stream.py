"""Anthropic 兼容：消息/工具转换 + 流式走 /v1/messages（不再误打 /chat/completions）。"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from infrastructure.llm_provider import (
    LLMClient,
    ProviderSnapshot,
    _openai_messages_to_anthropic,
    _openai_tools_to_anthropic,
)


def test_openai_tools_to_anthropic():
    tools = [{
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "search",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
    }]
    out = _openai_tools_to_anthropic(tools)
    assert out == [{
        "name": "web_search",
        "description": "search",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
    }]


def test_openai_messages_to_anthropic_tools_roundtrip():
    messages = [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {"name": "web_search", "arguments": '{"q":"x"}'},
        }]},
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        {"role": "user", "content": "thanks"},
    ]
    system, conv = _openai_messages_to_anthropic(messages)
    assert system == "you are helpful"
    assert conv[0]["role"] == "user"
    assert conv[1]["role"] == "assistant"
    assert conv[1]["content"][0]["type"] == "tool_use"
    assert conv[1]["content"][0]["name"] == "web_search"
    assert conv[1]["content"][0]["input"] == {"q": "x"}
    assert conv[2]["role"] == "user"
    assert conv[2]["content"][0]["type"] == "tool_result"
    assert conv[2]["content"][0]["tool_use_id"] == "call_1"
    assert conv[3]["role"] == "user"


def test_anthropic_stream_hits_v1_messages_not_chat_completions(monkeypatch):
    """对话流式必须打 /v1/messages；之前误打 /api/plan/chat/completions → 404。"""
    seen = {"url": None, "headers": None, "body": None}

    class _FakeStream:
        def __init__(self, lines):
            self._lines = lines
            self.status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            for line in self._lines:
                yield line

    class _FakeClient:
        def stream(self, method, url, json=None, headers=None, timeout=None):
            seen["url"] = url
            seen["headers"] = headers
            seen["body"] = json
            lines = [
                'data: {"type":"message_start","message":{"usage":{"input_tokens":3}}}',
                'data: {"type":"content_block_start","index":0,'
                '"content_block":{"type":"text","text":""}}',
                'data: {"type":"content_block_delta","index":0,'
                '"delta":{"type":"text_delta","text":"你好"}}',
                'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
                '"usage":{"output_tokens":2}}',
                'data: {"type":"message_stop"}',
            ]
            return _FakeStream(lines)

    client = LLMClient()
    monkeypatch.setattr(client, "_get_client", lambda: _FakeClient())
    snap = ProviderSnapshot(
        "p1", "anthropic",
        "https://ark.cn-beijing.volces.com/api/plan",
        "sk-test", "kimi-k3")

    async def _run():
        usage = {"input_tokens": 0, "output_tokens": 0,
                 "cache_read_tokens": 0, "cache_write_tokens": 0}
        parts = []
        async for kind, chunk in client._do_stream(
                snap, [{"role": "user", "content": "hi"}], usage,
                tools=[{
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "description": "s",
                        "parameters": {"type": "object"},
                    },
                }]):
            if kind == "content":
                parts.append(chunk)
        return "".join(parts), usage

    text, usage = asyncio.run(_run())
    assert text == "你好"
    assert usage["input_tokens"] == 3
    assert usage["output_tokens"] == 2
    assert seen["url"] == "https://ark.cn-beijing.volces.com/api/plan/v1/messages"
    assert "/chat/completions" not in seen["url"]
    assert seen["headers"]["Authorization"] == "Bearer sk-test"
    assert seen["headers"]["x-api-key"] == "sk-test"
    assert seen["body"]["stream"] is True
    assert seen["body"]["tools"][0]["name"] == "web_search"
    assert "input_schema" in seen["body"]["tools"][0]


def test_anthropic_stream_emits_openai_shaped_tool_deltas(monkeypatch):
    class _FakeStream:
        def __init__(self, lines):
            self._lines = lines
            self.status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            for line in self._lines:
                yield line

    class _FakeClient:
        def stream(self, method, url, json=None, headers=None, timeout=None):
            lines = [
                'data: {"type":"content_block_start","index":0,"content_block":'
                '{"type":"tool_use","id":"tu_1","name":"web_search","input":{}}}',
                'data: {"type":"content_block_delta","index":0,"delta":'
                '{"type":"input_json_delta","partial_json":"{\\"q\\":\\"a\\"}"}}',
                'data: {"type":"message_stop"}',
            ]
            return _FakeStream(lines)

    client = LLMClient()
    monkeypatch.setattr(client, "_get_client", lambda: _FakeClient())
    snap = ProviderSnapshot(
        "p1", "anthropic", "https://ark.cn-beijing.volces.com/api/plan",
        "sk-test", "kimi-k3")

    async def _run():
        usage = {"input_tokens": 0, "output_tokens": 0,
                 "cache_read_tokens": 0, "cache_write_tokens": 0}
        events = []
        async for kind, chunk in client._do_stream(
                snap, [{"role": "user", "content": "hi"}], usage):
            events.append((kind, chunk))
        return events

    events = asyncio.run(_run())
    assert events[0][0] == "tool_call"
    assert events[0][1]["id"] == "tu_1"
    assert events[0][1]["function"]["name"] == "web_search"
    assert events[1][0] == "tool_call"
    assert events[1][1]["function"]["arguments"] == '{"q":"a"}'
