"""图形工具可视化回归：render_flowchart/render_mermaid 成功后必须

1. 实时发射 tool_visual SSE 事件（前端 DiagramRenderer 据此渲染）；
2. 把 visuals 随最终 assistant 消息持久化（刷新后可恢复渲染）。

背景：该链路曾在一次「Agent 架构精简」重构中被整段删除，导致图形工具
执行成功但前端不渲染、刷新也无。此测试锁死该契约，防止再次回归。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from agent.turn_runtime import TurnRuntime
from infrastructure.db import Database
from tools.base import ToolRegistry, ToolSpec

ROOT = Path(__file__).resolve().parent.parent

_FLOWCHART_RESULT = {
    "type": "flowchart",
    "nodes": [{"id": "a", "type": "terminal", "label": "开始"},
              {"id": "b", "type": "process", "label": "处理"}],
    "edges": [{"from": "a", "to": "b"}],
}


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


class _VisualExecutor:
    """render_flowchart 返回结构化图形结果；其它工具返回普通结果。"""
    async def execute_tool(self, name, params, **_kw):
        if name == "render_flowchart":
            return {"ok": True, "result": _FLOWCHART_RESULT}
        return {"ok": True, "result": {"summary": f"{name} 结果"}}


class _FlowchartLLM:
    """step1 调 render_flowchart；step2 给最终回复。"""
    def __init__(self): self.n = 0
    async def stream_chat(self, _snap, _messages, **_kw):
        self.n += 1
        if self.n == 1:
            yield "done", {"content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "render_flowchart",
                              "arguments": '{"nodes":[],"edges":[]}'}}
            ], "usage": {"input_tokens": 10, "output_tokens": 5}}
        else:
            yield "content", "图已生成"
            yield "done", {"content": "图已生成", "tool_calls": [],
                           "usage": {"input_tokens": 5, "output_tokens": 2}}


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _build_runtime(db, emit):
    registry = ToolRegistry()
    registry.register_function(ToolSpec(
        "render_flowchart", "生成 SVG 流程图",
        {"type": "object", "properties": {
            "nodes": {"type": "array"}, "edges": {"type": "array"}},
         "required": ["nodes", "edges"]}), lambda **_: None)
    sessions = _Sessions()

    async def context_loader(**_kw):
        return {"snap": _Provider(), "history": [],
                "history_ids": [], "memory_count": 0}

    runtime = TurnRuntime(
        db=db, config=_Config(agent_max_steps=3),
        sessions=sessions, registry=registry,
        executor=_VisualExecutor(), llm=_FlowchartLLM(),
        providers=_Providers(),
        system_prompt=lambda *_a: "sys",
        context_loader=context_loader)
    return runtime, sessions


def test_tool_visual_emitted_and_persisted(tmp_path: Path):
    async def scenario():
        db = Database(tmp_path / "v.db")
        db.run_migrations(ROOT / "migrations")
        try:
            events: list[tuple[str, dict]] = []

            async def emit(name, data):
                events.append((name, data))

            runtime, sessions = _build_runtime(db, emit)
            await runtime.run(session_id="s", message="画个流程图",
                              reasoning_effort="high", emit=emit)

            # 1) 实时发射了 tool_visual 事件，且形状为 {type, data}
            visual_events = [d for (n, d) in events if n == "tool_visual"]
            assert len(visual_events) == 1, "render_flowchart 成功后应发射一次 tool_visual"
            ve = visual_events[0]
            assert ve["type"] == "flowchart"
            assert ve["data"]["nodes"], "data 应携带完整图形 JSON（含 nodes）"

            # 2) visuals 随最终 assistant 消息持久化
            assistant = [m for m in sessions.messages if m["role"] == "assistant"][-1]
            visuals = assistant.get("visuals")
            assert visuals, "assistant 消息应持久化 visuals"
            assert visuals[0]["type"] == "flowchart"
            assert visuals[0]["data"]["nodes"]
        finally:
            db.close()

    _run(scenario())
