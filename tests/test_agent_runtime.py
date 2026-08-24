"""Regression coverage for the event-sourced normal conversation runtime."""
from __future__ import annotations

import asyncio
from pathlib import Path

from agent.tool_policy import ToolPolicy
from agent.turn_events import TurnEventStore
from agent.turn_runtime import TurnRuntime
from infrastructure.db import Database
from tools.base import ToolRegistry, ToolSpec

ROOT = Path(__file__).resolve().parent.parent


class _Config:
    def __init__(self, **values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


class _Sessions:
    def __init__(self):
        self.messages: list[dict] = []

    def append_message(self, _session_id, role, content, **kwargs):
        self.messages.append({"role": role, "content": content, **kwargs})
        return len(self.messages)


class _Provider:
    model_id = "test-model"


class _Providers:
    def snapshot_for(self, _slot):
        return _Provider()


class _LLM:
    def __init__(self):
        self.prompts: list[list[dict]] = []
        self.responses = [
            {"content": "", "tool_calls": [{
                "id": "call_lookup",
                "type": "function",
                "function": {"name": "lookup", "arguments": '{"key":"A"}'},
            }]},
            {"content": "查询结果是 A=42。", "tool_calls": []},
        ]

    async def stream_chat(self, _snap, messages, **_kwargs):
        self.prompts.append(messages)
        resp = self.responses.pop(0)
        content = resp.get("content") or ""
        tool_calls = resp.get("tool_calls") or []
        if content:
            yield "content", content
        yield "done", {"content": content, "tool_calls": tool_calls,
                       "usage": {"input_tokens": 0, "output_tokens": 0}}


class _Executor:
    async def execute_tool(self, name, params, **_kwargs):
        assert name == "lookup"
        assert params == {"key": "A"}
        return {"ok": True, "result": {"value": 42}}


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "runtime.db")
    db.run_migrations(ROOT / "migrations")
    return db


def test_turn_events_project_tool_results_back_into_model_messages(tmp_path: Path):
    async def scenario():
        db = _db(tmp_path)
        try:
            config = _Config(agent_max_steps=4, tool_approval_ttl_minutes=5,
                             tool_writes_require_approval=True)
            registry = ToolRegistry()
            registry.register_function(ToolSpec(
                "lookup", "read a value", {"type": "object", "properties": {
                    "key": {"type": "string"}}, "required": ["key"]}),
                lambda **_kwargs: None)
            llm = _LLM()
            events: list[tuple[str, dict]] = []

            async def context_loader(**_kwargs):
                return {"snap": _Provider(), "history": [], "extra_system": "",
                        "memory_count": 0}

            async def emit(name, data):
                events.append((name, data))

            runtime = TurnRuntime(
                db=db, config=config, sessions=_Sessions(), registry=registry,
                executor=_Executor(), llm=llm, providers=_Providers(),
                tool_policy=ToolPolicy(db, config),
                system_prompt=lambda *_args: "system", context_loader=context_loader)
            outcome = await runtime.run(
                session_id="sess_runtime", message="查询 A", reasoning_effort="high",
                emit=emit, client_request_id="cr_runtime")

            assert outcome["content"] == "查询结果是 A=42。"
            assistant_message = runtime.sessions.messages[-1]
            assert assistant_message["role"] == "assistant"
            # 生命周期标签中的 turn/step 已剔除（对读者零信息量），只保留
            # 工具事件；模型的 reasoning 增量走 thinking_delta 汇入面板与落库
            assert assistant_message["thinking"] == (
                "【工具】正在执行 lookup\n"
                "【工具】lookup已完成\n"
            )
            assert [name for name, _data in events] == [
                "turn_started", "step_started", "tool_executing", "tool_result",
                "step_started", "content_delta", "turn_completed",
            ]
            # The second model request receives the actual tool result, not a
            # host-side inferred intent label or a transient queue payload.
            assert any(message["role"] == "tool" and "42" in message["content"]
                       for message in llm.prompts[1])
            turn = TurnEventStore(db).get_turn(outcome["turn_id"])
            assert turn["status"] == "completed"
            assert turn["reasoning_effort"] == "high"
            assert [event["type"] for event in TurnEventStore(db).events(outcome["turn_id"])] == [
                "turn.started", "user.message", "step.started", "request.header",
                "assistant.tool_calls", "tool.call", "tool.result", "step.finished",
                "step.started", "request.header", "assistant.message", "step.finished",
                "turn.finished",
            ]
        finally:
            db.close()

    asyncio.run(scenario())


def test_write_tool_requires_explicit_approval_and_records_decision(tmp_path: Path):
    async def scenario():
        db = _db(tmp_path)
        try:
            config = _Config(tool_approval_ttl_minutes=5, tool_writes_require_approval=True)
            registry = ToolRegistry()
            registry.register_function(ToolSpec(
                "write_note", "write", {"type": "object", "properties": {}},
                risk_level="write", approval_policy="every_call", parallel_safe=False),
                lambda **_kwargs: None)
            policy = ToolPolicy(db, config)
            decision = policy.inspect(registry.get("write_note"), {}, turn_id="turn_policy",
                                      call_id="call_policy")
            assert decision.action == "approval"
            assert decision.approval_id
            waiter = asyncio.create_task(policy.wait(decision.approval_id))
            await asyncio.sleep(0)
            row = policy.decide(decision.approval_id, approved=True)
            assert row and row["status"] == "approved"
            assert await waiter is True
        finally:
            db.close()

    asyncio.run(scenario())
