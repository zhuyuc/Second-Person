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


class _SlowLLM:
    async def stream_chat(self, _snap, messages, **_kw):
        yield "content", "partial "
        yield "content", "answer"
        await asyncio.sleep(60)
        yield "done", {"content": "partial answer", "tool_calls": [], "usage": {}}


class _NoContentLLM:
    """首字未到就挂住：本轮只有 step_progress 进度，正文为空。"""

    async def stream_chat(self, _snap, messages, **_kw):
        await asyncio.sleep(60)
        yield "done", {"content": "", "tool_calls": [], "usage": {}}


class _RaisingLLM:
    """吐出一段正文后抛错，模拟 provider 中途断流。"""

    async def stream_chat(self, _snap, messages, **_kw):
        yield "content", "已输出的前半段"
        await asyncio.sleep(0)
        raise RuntimeError("上游模型连接中断")


class _FinishingLLM:
    """正常完整回答。"""

    async def stream_chat(self, _snap, messages, **_kw):
        yield "content", "完整回答"
        yield "done", {"content": "完整回答", "tool_calls": [], "usage": {}}


def _make_runtime(tmp_path: Path, sessions, llm):
    db = _db(tmp_path)

    async def context_loader(**_kw):
        return {"snap": _Provider(), "history": [], "history_ids": [], "memory_count": 0}

    async def emit(_name, _data):
        return None

    runtime = TurnRuntime(
        db=db,
        config=_Config(agent_max_steps=3),
        sessions=sessions,
        registry=ToolRegistry(),
        executor=None,
        llm=llm,
        providers=_Providers(),
        system_prompt=lambda *_a: "sys",
        context_loader=context_loader,
    )
    return db, runtime, emit


async def _run_and_cancel(db, runtime, sessions, emit, delay=0.05):
    task = asyncio.create_task(
        runtime.run(session_id="s", message="hi", reasoning_effort="high", emit=emit))
    await asyncio.sleep(delay)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return sessions


def _assistant_msgs(sessions):
    return [m for m in sessions.messages if m["role"] == "assistant"]


def test_progress_only_cancel_still_persists(tmp_path: Path):
    """正文未开始、只有进度事件时取消：屏上有气泡，库里也必须有消息。"""
    async def scenario():
        sessions = _Sessions()
        db, runtime, emit = _make_runtime(tmp_path, sessions, _NoContentLLM())
        await _run_and_cancel(db, runtime, sessions, emit)
        assistant = _assistant_msgs(sessions)
        assert len(assistant) == 1, "仅有 step_progress 进度时也必须收口落库"
        assert "本回复未完成" in assistant[0]["content"]
        # 进度要落到 system_progress 车道，刷新后与实时所见一致
        assert assistant[0]["analysis_metadata"]["system_progress"]
        db.close()

    asyncio.run(scenario())


def test_exception_mid_stream_keeps_partial_body(tmp_path: Path):
    """流式中抛异常：已输出正文不能丢，且要带失败标记。"""
    async def scenario():
        sessions = _Sessions()
        db, runtime, emit = _make_runtime(tmp_path, sessions, _RaisingLLM())
        task = asyncio.create_task(
            runtime.run(session_id="s", message="hi",
                        reasoning_effort="high", emit=emit))
        with __import__("pytest").raises(RuntimeError):
            await task
        assistant = _assistant_msgs(sessions)
        assert len(assistant) == 1
        assert "已输出的前半段" in assistant[0]["content"]
        assert "本回复未完成" in assistant[0]["content"]
        assert assistant[0]["analysis_metadata"]["end_reason"] == "error"
        db.close()

    asyncio.run(scenario())


def test_streaming_checkpoint_is_durable_before_terminal_state(tmp_path: Path):
    """增量落库：未收口前就该有一条 streaming 在途行（抗进程硬杀）。"""
    async def scenario():
        sessions = _Sessions()
        db, runtime, emit = _make_runtime(tmp_path, sessions, _SlowLLM())
        task = asyncio.create_task(
            runtime.run(session_id="s", message="hi",
                        reasoning_effort="high", emit=emit))
        await asyncio.sleep(0.05)  # 只喂增量，不取消
        assistant = _assistant_msgs(sessions)
        assert len(assistant) == 1, "流式期间应已落下在途行"
        assert assistant[0]["analysis_metadata"]["end_reason"] == "streaming"
        assert "partial" in assistant[0]["content"]
        # 节流窗口内的后续增量不重复写库：仍是同一条在途行
        await asyncio.sleep(0.05)
        assert len(_assistant_msgs(sessions)) == 1
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        db.close()

    asyncio.run(scenario())


def test_normal_completion_writes_single_assistant_row(tmp_path: Path):
    """正常完成：在途行被覆盖收口，不得多出一条重复 assistant 消息。"""
    async def scenario():
        sessions = _Sessions()
        db, runtime, emit = _make_runtime(tmp_path, sessions, _FinishingLLM())
        await runtime.run(session_id="s", message="hi",
                          reasoning_effort="high", emit=emit)
        assistant = _assistant_msgs(sessions)
        assert len(assistant) == 1
        assert assistant[0]["content"] == "完整回答"
        assert "本回复未完成" not in assistant[0]["content"]
        assert assistant[0]["analysis_metadata"]["end_reason"] == "final_answer"
        db.close()

    asyncio.run(scenario())


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
