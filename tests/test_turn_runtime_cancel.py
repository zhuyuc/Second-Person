"""Turn-runtime: cancel mid-stream persists partial assistant reply."""
from __future__ import annotations

import asyncio
from pathlib import Path

from agent.turn_runtime import TurnRuntime
from infrastructure.db import Database
from tools.base import ToolRegistry

ROOT = Path(__file__).resolve().parent.parent


class _Config:
    def __init__(self, **v):
        self.values = v

    def get(self, k, default=None):
        return self.values.get(k, default)


class _Sessions:
    def __init__(self):
        self.messages: list[dict] = []

    def append_message(self, _sid, role, content, **kw):
        self.messages.append({"role": role, "content": content, **kw})
        return len(self.messages)


class _Provider:
    model_id = "test-model"


class _Providers:
    def snapshot_for(self, _slot):
        return _Provider()


class _SlowLLM:
    async def stream_chat(self, _snap, messages, **_kw):
        yield "content", "partial "
        yield "content", "answer"
        await asyncio.sleep(60)
        yield "done", {"content": "partial answer", "tool_calls": [], "usage": {}}


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "cancel.db")
    db.run_migrations(ROOT / "migrations")
    return db


def test_cancel_persists_partial_body(tmp_path: Path):
    async def scenario():
        db = _db(tmp_path)
        sessions = _Sessions()
        events: list[tuple[str, dict]] = []

        async def context_loader(**_kw):
            return {"snap": _Provider(), "history": [], "history_ids": [], "memory_count": 0}

        async def emit(name, data):
            events.append((name, data))

        runtime = TurnRuntime(
            db=db,
            config=_Config(agent_max_steps=3),
            sessions=sessions,
            registry=ToolRegistry(),
            executor=None,
            llm=_SlowLLM(),
            providers=_Providers(),
            system_prompt=lambda *_a: "sys",
            context_loader=context_loader,
        )
        task = asyncio.create_task(
            runtime.run(session_id="s", message="hi", reasoning_effort="high", emit=emit))
        await asyncio.sleep(0.05)
        task.cancel()
        with __import__("pytest").raises(asyncio.CancelledError):
            await task

        assert len(sessions.messages) == 2
        assistant = sessions.messages[-1]
        assert assistant["role"] == "assistant"
        assert "partial answer" in assistant["content"]
        assert "本回复未完成" in assistant["content"]
        assert any(name == "turn_completed" for name, _ in events)
        row = db.query_one("SELECT status FROM agent_turns WHERE session_id=?", ("s",))
        assert row["status"] == "cancelled"
        db.close()

    asyncio.run(scenario())
