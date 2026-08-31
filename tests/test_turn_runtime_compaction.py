"""Turn-runtime integration: pre-step compaction fires and rebuilds prompt."""
from __future__ import annotations

import asyncio
from pathlib import Path

from agent.compaction_engine import CompactionResult
from agent.turn_events import TurnEventStore
from agent.turn_runtime import TurnRuntime
from infrastructure.db import Database
from tools.base import ToolRegistry

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


class _LLM:
    """两 step 会话：第 1 step 要求 tool_calls，第 2 step 输出结果。"""
    def __init__(self):
        self.prompts: list[list[dict]] = []
        self.responses = [
            {"content": "", "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "noop", "arguments": "{}"}
            }]},
            {"content": "done", "tool_calls": []},
        ]

    async def stream_chat(self, _snap, messages, **_kw):
        self.prompts.append(messages)
        r = self.responses.pop(0)
        content = r.get("content") or ""
        if content:
            yield "content", content
        yield "done", {"content": content,
                       "tool_calls": r.get("tool_calls") or [],
                       "usage": {"input_tokens": 5000, "output_tokens": 100,
                                  "cache_read_tokens": 3000,
                                  "cache_write_tokens": 0}}


class _Executor:
    async def execute_tool(self, name, params, **_kw):
        return {"ok": True, "result": "ok"}


class _FiringCompaction:
    """在 step 2 触发一次压缩。"""
    def __init__(self):
        self.calls = 0

    async def compact_if_needed(self, **kw):
        self.calls += 1
        if self.calls == 1:
            return CompactionResult(
                trigger="pressure",
                shadowed_count=3,
                shadowed_message_ids=(1, 2, 3),
                last_shadowed_message_id=3,
                released_tokens_est=2500,
                summary_text="[SUMMARY]",
                total_before=9000,
                total_after_est=6500)
        return None


class _NoopCompaction:
    """从不触发的压缩引擎（用于对照）。"""
    async def compact_if_needed(self, **kw):
        return None


class _CountingMeter:
    def __init__(self): self.commits = 0
    def commit_anchor(self, *a, **kw): self.commits += 1


def _db(tmp_path):
    db = Database(tmp_path / "cmp.db")
    db.run_migrations(ROOT / "migrations")
    return db


# ---------------------------------------------------------------- happy path

def test_compaction_fires_at_step_2_and_reloads_context(tmp_path: Path):
    """step≥2 触发压缩 → 压缩成功 → 重新加载 context → 事件序列含 context.compacted。"""
    async def scenario():
        db = _db(tmp_path)
        try:
            events: list[tuple[str, dict]] = []
            load_calls = 0

            async def context_loader(**_kw):
                nonlocal load_calls
                load_calls += 1
                return {"snap": _Provider(), "history": [],
                        "history_ids": [], "memory_count": 0}

            async def emit(name, data):
                events.append((name, data))

            comp = _FiringCompaction()
            meter = _CountingMeter()
            runtime = TurnRuntime(
                db=db, config=_Config(agent_max_steps=3),
                sessions=_Sessions(), registry=ToolRegistry(),
                executor=_Executor(), llm=_LLM(), providers=_Providers(),
                system_prompt=lambda *_a: "sys",
                context_loader=context_loader,
                token_meter=meter, compaction_engine=comp)
            outcome = await runtime.run(
                session_id="s", message="hi", reasoning_effort="high", emit=emit)
            assert outcome["content"] == "done"
            # compact 在 step 2 被调用一次
            assert comp.calls == 1
            # 事件流中出现 context_compacted
            assert any(n == "context_compacted" for n, _ in events)
            # 每 step 结束后 commit_anchor 都调了（2 step）
            assert meter.commits == 2
            # context_loader 被调用了两次：初始 + 压缩后重加载
            assert load_calls == 2
            # 事件持久化含 context.compacted
            all_events = TurnEventStore(db).events(outcome["turn_id"])
            assert any(e["type"] == "context.compacted" for e in all_events)
        finally:
            db.close()

    asyncio.run(scenario())


def test_no_compaction_no_reload(tmp_path: Path):
    """未触发压缩 → context_loader 只调一次（cold cache 复用）。"""
    async def scenario():
        db = _db(tmp_path)
        try:
            load_calls = 0

            async def context_loader(**_kw):
                nonlocal load_calls
                load_calls += 1
                return {"snap": _Provider(), "history": [],
                        "history_ids": [], "memory_count": 0}

            async def emit(_n, _d): pass

            runtime = TurnRuntime(
                db=db, config=_Config(agent_max_steps=3),
                sessions=_Sessions(), registry=ToolRegistry(),
                executor=_Executor(), llm=_LLM(), providers=_Providers(),
                system_prompt=lambda *_a: "sys",
                context_loader=context_loader,
                token_meter=_CountingMeter(), compaction_engine=_NoopCompaction())
            await runtime.run(session_id="s", message="hi",
                              reasoning_effort="high", emit=emit)
            assert load_calls == 1
        finally:
            db.close()

    asyncio.run(scenario())


def test_missing_engine_is_optional(tmp_path: Path):
    """compaction_engine=None 时降级为原有行为，不炸。"""
    async def scenario():
        db = _db(tmp_path)
        try:
            async def context_loader(**_kw):
                return {"snap": _Provider(), "history": [],
                        "history_ids": [], "memory_count": 0}
            async def emit(_n, _d): pass
            runtime = TurnRuntime(
                db=db, config=_Config(agent_max_steps=3),
                sessions=_Sessions(), registry=ToolRegistry(),
                executor=_Executor(), llm=_LLM(), providers=_Providers(),
                system_prompt=lambda *_a: "sys",
                context_loader=context_loader,
                token_meter=None, compaction_engine=None)
            outcome = await runtime.run(
                session_id="s", message="hi",
                reasoning_effort="high", emit=emit)
            assert outcome["content"] == "done"
        finally:
            db.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------- fallback text

def test_max_steps_fallback_appends_partial_when_content_exists(tmp_path: Path):
    """跑到 max_steps 且已有 content_parts → 触顶文案拼接部分结果。"""
    class _ChatterboxLLM:
        """每步都吐 content 但从不结束（一直 tool_calls）。"""
        async def stream_chat(self, _snap, _msgs, **_kw):
            yield "content", "partial "
            yield "done", {"content": "partial ",
                            "tool_calls": [{"id": "c", "type": "function",
                                             "function": {"name": "noop",
                                                          "arguments": "{}"}}],
                            "usage": {"input_tokens": 100, "output_tokens": 10}}

    async def scenario():
        db = _db(tmp_path)
        try:
            async def context_loader(**_kw):
                return {"snap": _Provider(), "history": [],
                        "history_ids": [], "memory_count": 0}
            async def emit(_n, _d): pass
            runtime = TurnRuntime(
                db=db, config=_Config(agent_max_steps=2),
                sessions=_Sessions(), registry=ToolRegistry(),
                executor=_Executor(), llm=_ChatterboxLLM(),
                providers=_Providers(),
                system_prompt=lambda *_a: "sys",
                context_loader=context_loader,
                token_meter=None, compaction_engine=None)
            outcome = await runtime.run(
                session_id="s", message="loop", reasoning_effort="high", emit=emit)
            # 触顶后应包含 partial + 提示
            assert "partial" in outcome["content"]
            assert "2 步上限" in outcome["content"]
        finally:
            db.close()

    asyncio.run(scenario())


def test_max_steps_fallback_gives_advice_when_no_content(tmp_path: Path):
    """没产出任何内容就触顶 → 提示拆分子问题。"""
    class _SilentLLM:
        async def stream_chat(self, _snap, _msgs, **_kw):
            yield "done", {"content": "",
                            "tool_calls": [{"id": "c", "type": "function",
                                             "function": {"name": "noop",
                                                          "arguments": "{}"}}],
                            "usage": {"input_tokens": 100, "output_tokens": 0}}

    async def scenario():
        db = _db(tmp_path)
        try:
            async def context_loader(**_kw):
                return {"snap": _Provider(), "history": [],
                        "history_ids": [], "memory_count": 0}
            async def emit(_n, _d): pass
            runtime = TurnRuntime(
                db=db, config=_Config(agent_max_steps=2),
                sessions=_Sessions(), registry=ToolRegistry(),
                executor=_Executor(), llm=_SilentLLM(), providers=_Providers(),
                system_prompt=lambda *_a: "sys",
                context_loader=context_loader,
                token_meter=None, compaction_engine=None)
            outcome = await runtime.run(
                session_id="s", message="loop", reasoning_effort="high", emit=emit)
            assert "拆分" in outcome["content"] or "新开一个会话" in outcome["content"]
        finally:
            db.close()

    asyncio.run(scenario())
