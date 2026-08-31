"""Regression coverage for the event-sourced normal conversation runtime."""
from __future__ import annotations

import asyncio
from pathlib import Path

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
            config = _Config(agent_max_steps=4)
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
                system_prompt=lambda *_args: "system", context_loader=context_loader)
            outcome = await runtime.run(
                session_id="sess_runtime", message="查询 A", reasoning_effort="high",
                emit=emit, client_request_id="cr_runtime")

            assert outcome["content"] == "查询结果是 A=42。"
            assistant_message = runtime.sessions.messages[-1]
            assert assistant_message["role"] == "assistant"
            # 生命周期标签中的 turn/step 已剔除（对读者零信息量），只保留
            # 工具事件；模型的 reasoning 增量走 reasoning_delta 汇入面板与落库
            assert assistant_message["thinking"] == (
                "【工具】正在执行 lookup\n"
                "【工具】lookup已完成\n"
            )
            assert [name for name, _data in events] == [
                "turn_started", "step_started", "tool_executing", "tool_result",
                "step_metrics", "step_started", "content_delta", "turn_completed",
            ]
            # The second model request receives the actual tool result, not a
            # host-side inferred intent label or a transient queue payload.
            assert any(message["role"] == "tool" and "42" in message["content"]
                       for message in llm.prompts[1])
            # 本轮时间以 context.time 事件形式追加到 messages 末尾（紧跟 user）——
            # 系统提示不再含时间戳，保护 provider prefix cache 不被分钟级抖动击穿。
            # 文本极简化后标签固定为 [北京时间]（T1 优化，见 turn_runtime._format_turn_time）
            assert not any("[北京时间]" in (m.get("content") or "")
                           for m in llm.prompts[0]
                           if m.get("role") == "system")
            assert any(m.get("role") == "user"
                       and "[北京时间]" in (m.get("content") or "")
                       for m in llm.prompts[0])
            turn = TurnEventStore(db).get_turn(outcome["turn_id"])
            assert turn["status"] == "completed"
            assert turn["reasoning_effort"] == "high"
            assert [event["type"] for event in TurnEventStore(db).events(outcome["turn_id"])] == [
                "turn.started", "user.message", "context.time",
                "step.started", "request.header",
                "assistant.tool_calls", "tool.call", "tool.result", "step.finished",
                "step.started", "request.header", "assistant.message", "step.finished",
                "turn.finished",
            ]
        finally:
            db.close()

    asyncio.run(scenario())


def test_provider_reasoning_is_separate_from_tool_progress_and_persisted(tmp_path: Path):
    async def scenario():
        db = _db(tmp_path)
        try:
            config = _Config(agent_max_steps=2)
            registry = ToolRegistry()
            events: list[tuple[str, dict]] = []

            class ReasoningProvider(_Provider):
                reasoning_efforts = ("off", "low", "high", "max")
                native_reasoning = True

            class ReasoningLLM:
                async def stream_chat(self, _snap, _messages, **_kwargs):
                    yield "reasoning", "先核对用户问题。"
                    yield "content", "已核对。"
                    yield "done", {"content": "已核对。", "tool_calls": []}

            async def context_loader(**_kwargs):
                return {"snap": ReasoningProvider(), "history": [], "extra_system": "",
                        "memory_count": 0}

            async def emit(name, data):
                events.append((name, data))

            runtime = TurnRuntime(
                db=db, config=config, sessions=_Sessions(), registry=registry,
                executor=_Executor(), llm=ReasoningLLM(), providers=_Providers(),
                system_prompt=lambda *_args: "system", context_loader=context_loader)
            outcome = await runtime.run(
                session_id="sess_reasoning", message="核对", reasoning_effort="high", emit=emit)
            saved = runtime.sessions.messages[-1]
            assert outcome["content"] == "已核对。"
            assert saved["thinking"] is None
            assert saved["analysis_metadata"]["reasoning_text"] == "先核对用户问题。"
            assert saved["analysis_metadata"]["reasoning_available"] is True
            assert [name for name, _data in events] == [
                "turn_started", "step_started", "reasoning_delta",
                "content_delta", "turn_completed",
            ]
        finally:
            db.close()

    asyncio.run(scenario())


def test_tool_step_narration_is_retracted_from_body(tmp_path: Path):
    """工具步里模型"边说边调工具"的旁白会实时进正文；步结束确认带 tool_calls
    后必须下发 content_reset 撤回，保证正文只留最终答案，不出现"思考先写进
    正文、最后又消失"的观感。"""
    async def scenario():
        db = _db(tmp_path)
        try:
            config = _Config(agent_max_steps=4)
            registry = ToolRegistry()
            registry.register_function(ToolSpec(
                "lookup", "read a value", {"type": "object", "properties": {
                    "key": {"type": "string"}}, "required": ["key"]}),
                lambda **_kwargs: None)

            class NarratingLLM:
                async def stream_chat(self, _snap, _messages, **_kwargs):
                    # 第一次调用：先吐旁白，再调工具（工具步）
                    yield "content", "先查一下。"
                    yield "done", {"content": "先查一下。", "tool_calls": [{
                        "id": "call_lookup", "type": "function",
                        "function": {"name": "lookup", "arguments": '{"key":"A"}'}}],
                        "usage": {"input_tokens": 0, "output_tokens": 0}}

            calls = {"n": 0}
            inner = NarratingLLM()

            class TwoStepLLM:
                async def stream_chat(self, snap, messages, **kwargs):
                    calls["n"] += 1
                    if calls["n"] == 1:
                        async for item in inner.stream_chat(snap, messages, **kwargs):
                            yield item
                    else:
                        yield "content", "结果是 42。"
                        yield "done", {"content": "结果是 42。", "tool_calls": [],
                                       "usage": {"input_tokens": 0, "output_tokens": 0}}

            events: list[tuple[str, dict]] = []

            async def context_loader(**_kwargs):
                return {"snap": _Provider(), "history": [], "memory_count": 0}

            async def emit(name, data):
                events.append((name, data))

            runtime = TurnRuntime(
                db=db, config=config, sessions=_Sessions(), registry=registry,
                executor=_Executor(), llm=TwoStepLLM(), providers=_Providers(),
                system_prompt=lambda *_args: "system", context_loader=context_loader)
            outcome = await runtime.run(
                session_id="sess_retract", message="查询 A", reasoning_effort="high", emit=emit)

            names = [name for name, _data in events]
            # 工具步旁白已流式进正文（content_delta），随后被 content_reset 撤回
            assert "content_reset" in names
            assert names.index("content_reset") < names.index("turn_completed")
            # 最终正文只保留末步答案
            assert outcome["content"] == "结果是 42。"
            assert runtime.sessions.messages[-1]["content"] == "结果是 42。"
            # 旁白不丢失：被撤回出正文后记入 timeline 留存
            saved_timeline = runtime.sessions.messages[-1]["analysis_metadata"]["timeline"]
            narration_items = [it for it in saved_timeline if it.get("kind") == "narration"]
            assert narration_items and narration_items[0]["text"] == "先查一下。"
        finally:
            db.close()

    asyncio.run(scenario())


def test_reasoning_effort_switch_keeps_messages_and_tools_stable(tmp_path: Path):
    """N7 回归：同一 session 同一 message 切换 reasoning_effort 时，
    送到 LLM 的 messages 和 tools 应该字节完全相同——effort 只走 extra_body，
    不能污染 prompt payload；这样 provider prefix cache 才不会因用户切档而击穿。
    """
    class _CapturingLLM:
        def __init__(self):
            self.messages_seen: list[list[dict]] = []
            self.tools_seen: list[list[dict]] = []
            self.extra_body_seen: list[dict] = []

        async def stream_chat(self, _snap, messages, **kwargs):
            self.messages_seen.append(messages)
            self.tools_seen.append(kwargs.get("tools") or [])
            self.extra_body_seen.append(dict(kwargs.get("extra_body") or {}))
            yield "content", "ok"
            yield "done", {"content": "ok", "tool_calls": [],
                           "usage": {"input_tokens": 0, "output_tokens": 0}}

    async def scenario():
        db = _db(tmp_path)
        try:
            config = _Config(agent_max_steps=1)
            registry = ToolRegistry()
            llm = _CapturingLLM()

            async def context_loader(**_kwargs):
                return {"snap": _Provider(), "history": [], "memory_count": 0}

            async def emit(_name, _data):
                pass

            runtime = TurnRuntime(
                db=db, config=config, sessions=_Sessions(), registry=registry,
                executor=_Executor(), llm=llm, providers=_Providers(),
                system_prompt=lambda *_args: "system", context_loader=context_loader)

            # 冻结时间源：turn_runtime._format_turn_time() 用 now_cst，若两次 run
            # 分别在两个分钟里跑，追加的 context.time 文本会不同——那是"时间真的
            # 变了"而不是"切换 effort 造成的击穿"，我们要隔离前者只测后者。
            import agent.turn_runtime as tr
            original = tr._format_turn_time
            tr._format_turn_time = lambda: "[本轮元信息] 固定时间戳"
            try:
                await runtime.run(session_id="sess_effort", message="同一条消息",
                                  reasoning_effort="high", emit=emit)
                await runtime.run(session_id="sess_effort", message="同一条消息",
                                  reasoning_effort="low", emit=emit)
            finally:
                tr._format_turn_time = original

            # 两轮的 messages 严格相同（不受 effort 影响）
            assert llm.messages_seen[0] == llm.messages_seen[1]
            assert llm.tools_seen[0] == llm.tools_seen[1]
            # effort 只走 extra_body
            assert llm.extra_body_seen[0].get("reasoning_effort") == "high"
            assert llm.extra_body_seen[1].get("reasoning_effort") == "low"
        finally:
            db.close()

    asyncio.run(scenario())


def test_tool_schemas_are_canonical_json_stable():
    """N6 回归：同一 tool 无论 parameters 键顺序如何构造，openai_schemas 输出的
    字节应完全一致——避免 MCP 或外部动态注册的键序抖动击穿 prefix cache。
    """
    import json as _json
    registry_a = ToolRegistry()
    registry_a.register_function(ToolSpec(
        "lookup", "read",
        {"type": "object", "properties": {"key": {"type": "string"}},
         "required": ["key"]}),
        lambda **_kwargs: None)
    registry_b = ToolRegistry()
    registry_b.register_function(ToolSpec(
        "lookup", "read",
        # 完全相同的语义，但键顺序不同（properties 在 type 前）
        {"required": ["key"], "properties": {"key": {"type": "string"}},
         "type": "object"}),
        lambda **_kwargs: None)
    schemas_a = _json.dumps(registry_a.openai_schemas(), sort_keys=False,
                             ensure_ascii=False)
    schemas_b = _json.dumps(registry_b.openai_schemas(), sort_keys=False,
                             ensure_ascii=False)
    assert schemas_a == schemas_b
