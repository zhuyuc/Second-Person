"""Interleaved reasoning + tool_call timeline (v7 前端时间线视图数据源)."""
from __future__ import annotations

import asyncio
from pathlib import Path

from agent.turn_runtime import TurnRuntime
from infrastructure.db import Database
from tools.base import ToolRegistry, ToolSpec

ROOT = Path(__file__).resolve().parent.parent


class _Config:
    def __init__(self, **v): self.values = v
    def get(self, k, default=None): return self.values.get(k, default)


class _Sessions:
    def __init__(self): self.messages: list[dict] = []
    def append_message(self, _sid, role, content, **kw):
        self.messages.append({"role": role, "content": content, **kw})
        return len(self.messages)


class _Provider:
    model_id = "test-model"
    context_window = 10000


class _Providers:
    def snapshot_for(self, _slot): return _Provider()


class _Executor:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
    async def execute_tool(self, name, params, **_kw):
        self.calls.append((name, params))
        return {"ok": True, "result": {"summary": f"{name} 结果"}}


def _db(tmp_path):
    db = Database(tmp_path / "tl.db")
    db.run_migrations(ROOT / "migrations")
    return db


class _InterleavedLLM:
    """两 step：step1 reasoning + 2 个 tool_calls；step2 reasoning + 结束。

    保证 SSE 到达顺序是 reasoning → tool_call → reasoning → tool_call。
    """
    async def stream_chat(self, _snap, _messages, **_kw):
        # step 1
        yield "reasoning", "我先看看项目根目录。"
        yield "done", {"content": "", "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "lookup", "arguments": '{"key":"A"}'}},
        ], "usage": {"input_tokens": 10, "output_tokens": 5}}


class _TwoStepLLM:
    """第一 step reasoning + 1 tool；第二 step reasoning + final answer。"""
    def __init__(self):
        self.n = 0
    async def stream_chat(self, _snap, _messages, **_kw):
        self.n += 1
        if self.n == 1:
            yield "reasoning", "先看目录。"
            yield "done", {"content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "lookup", "arguments": '{"key":"A"}'}}
            ], "usage": {"input_tokens": 10, "output_tokens": 5}}
        else:
            yield "reasoning", "了解到了，"
            yield "reasoning", "继续读一个文件。"
            yield "content", "OK"
            yield "done", {"content": "OK", "tool_calls": [],
                            "usage": {"input_tokens": 20, "output_tokens": 2}}


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ------------------------------------------------------------ core shape ----

def test_timeline_interleaves_reasoning_and_tools(tmp_path: Path):
    """一轮完整：reasoning → tool_executing → tool_result → reasoning → 结束。"""
    async def scenario():
        db = _db(tmp_path)
        try:
            registry = ToolRegistry()
            registry.register_function(ToolSpec(
                "lookup", "test", {"type": "object", "properties": {
                    "key": {"type": "string"}}, "required": ["key"]}),
                lambda **_: None)
            sessions = _Sessions()

            async def context_loader(**_kw):
                return {"snap": _Provider(), "history": [],
                        "history_ids": [], "memory_count": 0}
            async def emit(_n, _d): pass

            runtime = TurnRuntime(
                db=db, config=_Config(agent_max_steps=3),
                sessions=sessions, registry=registry,
                executor=_Executor(), llm=_TwoStepLLM(),
                providers=_Providers(),
                system_prompt=lambda *_a: "sys",
                context_loader=context_loader)
            await runtime.run(session_id="s", message="hi",
                              reasoning_effort="high", emit=emit)
            saved = sessions.messages[-1]
            tl = saved["analysis_metadata"]["timeline"]
            # 期望结构：reasoning → tool_call(ok) → reasoning (合并的)
            kinds = [x["kind"] for x in tl]
            assert kinds == ["reasoning", "tool_call", "reasoning"]
            # 第一段 reasoning 是 step1 的
            assert tl[0]["text"] == "先看目录。"
            # tool_call 项完整字段
            tc = tl[1]
            assert tc["name"] == "lookup"
            assert tc["call_id"] == "c1"
            assert tc["arguments"] == '{"key": "A"}'
            assert tc["status"] == "ok"
            assert "lookup" in tc["result_preview"] or "结果" in tc["result_preview"]
            # 第二段 reasoning 合并了两个 delta
            assert tl[2]["text"] == "了解到了，继续读一个文件。"
        finally:
            db.close()

    _run(scenario())


def test_timeline_consecutive_reasoning_merges(tmp_path: Path):
    """连续的 reasoning_delta 应该合并到同一 reasoning 段。"""
    class _MultiDeltaLLM:
        async def stream_chat(self, _snap, _msgs, **_kw):
            yield "reasoning", "第一句。"
            yield "reasoning", "第二句。"
            yield "reasoning", "第三句。"
            yield "content", "done"
            yield "done", {"content": "done", "tool_calls": [],
                            "usage": {"input_tokens": 10, "output_tokens": 3}}

    async def scenario():
        db = _db(tmp_path)
        try:
            sessions = _Sessions()
            async def context_loader(**_kw):
                return {"snap": _Provider(), "history": [],
                        "history_ids": [], "memory_count": 0}
            async def emit(_n, _d): pass
            runtime = TurnRuntime(
                db=db, config=_Config(agent_max_steps=2),
                sessions=sessions, registry=ToolRegistry(),
                executor=_Executor(), llm=_MultiDeltaLLM(),
                providers=_Providers(),
                system_prompt=lambda *_a: "sys",
                context_loader=context_loader)
            await runtime.run(session_id="s", message="hi",
                              reasoning_effort="high", emit=emit)
            tl = sessions.messages[-1]["analysis_metadata"]["timeline"]
            reasoning_items = [x for x in tl if x["kind"] == "reasoning"]
            assert len(reasoning_items) == 1
            assert reasoning_items[0]["text"] == "第一句。第二句。第三句。"
        finally:
            db.close()

    _run(scenario())


def test_timeline_tool_failure_marked(tmp_path: Path):
    """工具执行失败 → tool_call.status = 'fail'，携带 error 字段。"""
    class _FailingExecutor:
        async def execute_tool(self, _name, _params, **_kw):
            return {"ok": False, "error": "网络超时"}
    class _OneToolLLM:
        def __init__(self): self.n = 0
        async def stream_chat(self, _snap, _msgs, **_kw):
            self.n += 1
            if self.n == 1:
                yield "reasoning", "试试。"
                yield "done", {"content": "", "tool_calls": [
                    {"id": "c1", "type": "function",
                     "function": {"name": "lookup", "arguments": "{}"}}
                ], "usage": {"input_tokens": 5, "output_tokens": 0}}
            else:
                yield "content", "失败了"
                yield "done", {"content": "失败了", "tool_calls": [],
                                "usage": {"input_tokens": 10, "output_tokens": 1}}

    async def scenario():
        db = _db(tmp_path)
        try:
            registry = ToolRegistry()
            registry.register_function(ToolSpec(
                "lookup", "", {"type": "object", "properties": {}}),
                lambda **_: None)
            sessions = _Sessions()
            async def context_loader(**_kw):
                return {"snap": _Provider(), "history": [],
                        "history_ids": [], "memory_count": 0}
            async def emit(_n, _d): pass
            runtime = TurnRuntime(
                db=db, config=_Config(agent_max_steps=3),
                sessions=sessions, registry=registry,
                executor=_FailingExecutor(), llm=_OneToolLLM(),
                providers=_Providers(),
                system_prompt=lambda *_a: "sys",
                context_loader=context_loader)
            await runtime.run(session_id="s", message="try",
                              reasoning_effort="high", emit=emit)
            tl = sessions.messages[-1]["analysis_metadata"]["timeline"]
            tools = [x for x in tl if x["kind"] == "tool_call"]
            assert len(tools) == 1
            assert tools[0]["status"] == "fail"
        finally:
            db.close()

    _run(scenario())


def test_timeline_present_even_without_provider_reasoning(tmp_path: Path):
    """provider 不吐 reasoning 时，timeline 仍应含 tool_call 项。"""
    class _ToolOnlyLLM:
        def __init__(self): self.n = 0
        async def stream_chat(self, _snap, _msgs, **_kw):
            self.n += 1
            if self.n == 1:
                yield "done", {"content": "", "tool_calls": [
                    {"id": "c1", "type": "function",
                     "function": {"name": "lookup", "arguments": "{}"}}
                ], "usage": {"input_tokens": 5, "output_tokens": 0}}
            else:
                yield "content", "OK"
                yield "done", {"content": "OK", "tool_calls": [],
                                "usage": {"input_tokens": 10, "output_tokens": 1}}

    async def scenario():
        db = _db(tmp_path)
        try:
            registry = ToolRegistry()
            registry.register_function(ToolSpec(
                "lookup", "", {"type": "object", "properties": {}}),
                lambda **_: None)
            sessions = _Sessions()
            async def context_loader(**_kw):
                return {"snap": _Provider(), "history": [],
                        "history_ids": [], "memory_count": 0}
            async def emit(_n, _d): pass
            runtime = TurnRuntime(
                db=db, config=_Config(agent_max_steps=3),
                sessions=sessions, registry=registry,
                executor=_Executor(), llm=_ToolOnlyLLM(),
                providers=_Providers(),
                system_prompt=lambda *_a: "sys",
                context_loader=context_loader)
            await runtime.run(session_id="s", message="try",
                              reasoning_effort="high", emit=emit)
            tl = sessions.messages[-1]["analysis_metadata"]["timeline"]
            assert any(x["kind"] == "tool_call" for x in tl)
        finally:
            db.close()

    _run(scenario())


def test_timeline_empty_when_no_events(tmp_path: Path):
    """纯 content 输出 → timeline 为空数组，前端 fallback。"""
    class _ContentOnlyLLM:
        async def stream_chat(self, _snap, _msgs, **_kw):
            yield "content", "简单回答"
            yield "done", {"content": "简单回答", "tool_calls": [],
                            "usage": {"input_tokens": 5, "output_tokens": 3}}

    async def scenario():
        db = _db(tmp_path)
        try:
            sessions = _Sessions()
            async def context_loader(**_kw):
                return {"snap": _Provider(), "history": [],
                        "history_ids": [], "memory_count": 0}
            async def emit(_n, _d): pass
            runtime = TurnRuntime(
                db=db, config=_Config(agent_max_steps=2),
                sessions=sessions, registry=ToolRegistry(),
                executor=_Executor(), llm=_ContentOnlyLLM(),
                providers=_Providers(),
                system_prompt=lambda *_a: "sys",
                context_loader=context_loader)
            await runtime.run(session_id="s", message="hi",
                              reasoning_effort="high", emit=emit)
            tl = sessions.messages[-1]["analysis_metadata"]["timeline"]
            assert tl == []
        finally:
            db.close()

    _run(scenario())
