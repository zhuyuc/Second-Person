"""Langfuse agent.step 生命周期：工具必须挂在 step 下，step 需带摘要 output。"""
from __future__ import annotations

import asyncio
from pathlib import Path

from agent.turn_runtime import TurnRuntime
from infrastructure.db import Database
from langfuse.integration.config import LangfuseConfig
from langfuse.integration.tracer import PipelineTracer
from tools.base import ToolRegistry, ToolSpec

ROOT = Path(__file__).resolve().parent.parent


class _FakeClient:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def enqueue(self, event: dict) -> None:
        self.events.append(event)


class _Config:
    def get(self, key, default=None):
        return {"agent_max_steps": 4}.get(key, default)


class _Sessions:
    def __init__(self):
        self.messages: list[dict] = []

    def append_message(self, _session_id, role, content, **kwargs):
        self.messages.append({"role": role, "content": content, **kwargs})
        return len(self.messages)

    def update_message(self, msg_id, **fields):
        target = self.messages[msg_id - 1]
        for key, value in fields.items():
            if value is not None:
                target[key] = value


class _Provider:
    model_id = "test-model"


class _Providers:
    def snapshot_for(self, _slot):
        return _Provider()


class _LLM:
    def __init__(self):
        self.responses = [
            {"content": "", "tool_calls": [{
                "id": "call_lookup",
                "type": "function",
                "function": {"name": "lookup", "arguments": '{"key":"A"}'},
            }]},
            {"content": "结果是 42。", "tool_calls": []},
        ]

    async def stream_chat(self, _snap, _messages, **_kwargs):
        resp = self.responses.pop(0)
        content = resp.get("content") or ""
        tool_calls = resp.get("tool_calls") or []
        if content:
            yield "content", content
        yield "done", {"content": content, "tool_calls": tool_calls,
                       "usage": {"input_tokens": 1, "output_tokens": 1}}


class _TracingExecutor:
    """模拟 ToolExecutor：在活跃 step 下创建 tool_execute span。"""

    async def execute_tool(self, name, params, **_kwargs):
        from langfuse.integration import get_tracer
        span = get_tracer().span_start("tool_execute", input={"tool": name})
        out = {"ok": True, "result": {"value": 42}, "tool": name}
        span.end(output=out)
        return out


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "step_life.db")
    db.run_migrations(ROOT / "migrations")
    return db


def _events(fake: _FakeClient, event_type: str) -> list[dict]:
    return [e for e in fake.events if e["type"] == event_type]


def test_tool_execute_nests_under_agent_step(tmp_path: Path, monkeypatch):
    tracer = PipelineTracer(LangfuseConfig(
        enabled=True, public_key="pk-test", secret_key="sk-test",
        host="http://localhost:3000"))
    fake = _FakeClient()
    tracer._client = fake  # noqa: SLF001

    monkeypatch.setattr("langfuse.integration.get_tracer", lambda: tracer)
    monkeypatch.setattr("agent.turn_runtime.get_tracer", lambda: tracer)

    async def scenario():
        db = _db(tmp_path)
        try:
            registry = ToolRegistry()
            registry.register_function(ToolSpec(
                "lookup", "read", {"type": "object", "properties": {
                    "key": {"type": "string"}}, "required": ["key"]}),
                lambda **_k: None)

            async def context_loader(**_kw):
                return {"snap": _Provider(), "history": [], "memory_count": 0}

            async def emit(_n, _d):
                return None

            runtime = TurnRuntime(
                db=db, config=_Config(), sessions=_Sessions(),
                registry=registry, executor=_TracingExecutor(),
                llm=_LLM(), providers=_Providers(),
                system_prompt=lambda *_a: "sys",
                context_loader=context_loader)
            outcome = await runtime.run(
                session_id="sess_lf_step", message="查 A",
                reasoning_effort="low", emit=emit)
            assert "42" in outcome["content"]
        finally:
            db.close()

    asyncio.run(scenario())

    span_creates = _events(fake, "span-create")
    span_updates = _events(fake, "span-update")

    steps = [e["body"] for e in span_creates if e["body"]["name"] == "agent.step"]
    tools = [e["body"] for e in span_creates if e["body"]["name"] == "tool_execute"]
    assert len(steps) == 2
    assert len(tools) == 1

    tool = tools[0]
    step1 = steps[0]
    assert tool["parentObservationId"] == step1["id"], (
        "tool_execute 必须挂在发起工具调用的 agent.step 下，不能掉到 trace 根")

    # step 摘要 output：工具步 / 最终步
    step_ends = []
    for u in span_updates:
        body = u["body"]
        if body.get("id") in {s["id"] for s in steps} and "output" in body:
            step_ends.append(body)
    outcomes = [b["output"].get("outcome") for b in step_ends]
    assert "tool_calls" in outcomes
    assert "final" in outcomes
    final = next(b for b in step_ends if b["output"].get("outcome") == "final")
    assert final["output"].get("message_id")
    assert final["output"].get("content_chars", 0) > 0
    tool_step = next(b for b in step_ends if b["output"].get("outcome") == "tool_calls")
    assert tool_step["output"].get("tool_calls") == 1
    # 工具结束后才关掉 step：step1 endTime >= tool create 之后（同批事件用顺序近似）
    create_ids = [e["body"]["id"] for e in fake.events
                  if e["type"] == "span-create"]
    tool_idx = create_ids.index(tool["id"])
    step1_end_idx = next(
        i for i, e in enumerate(fake.events)
        if e["type"] == "span-update"
        and e["body"].get("id") == step1["id"]
        and e["body"].get("endTime"))
    assert step1_end_idx > tool_idx


def test_mood_judge_uses_dedicated_llm_source(monkeypatch):
    """情绪判定 generation 源为 mood_judge，避免与 agent_step 混淆。"""
    captured: dict = {}

    class _FakeLLM:
        async def chat(self, _snap, _prompt, **kwargs):
            captured.update(kwargs)
            return {"content": '{"user":{"mood":"neutral"},"ai":{"mood":"curious"}}'}

    class _Providers:
        def snapshot_for(self, _slot):
            return _Provider()

    async def run():
        from soul.mood_judge import judge_turn_moods
        user, ai, peace = await judge_turn_moods(
            _FakeLLM(), _Providers(),
            user_message="hi", assistant_content="hello")
        assert user["mood"] == "neutral"
        assert ai["mood"] == "curious"
        assert peace == "none"

    asyncio.run(run())
    assert captured.get("source") == "mood_judge"
    assert captured.get("json_mode") is True
