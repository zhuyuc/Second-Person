"""SSE 断线重连集成测试。

保护 _follow(buf) 的核心契约：
- 首连从头回放已缓冲事件，持续跟读到 done
- 断线重连走同一路径，从现存最早事件回放
- 缓冲被裁剪（dropped>0）时跳到现存最早事件，不报错
- 读者断开只终止本生成器，不影响后台生成任务（buf 状态不被破坏）
"""
from __future__ import annotations

import asyncio

import pytest

from app.routes.chat import _follow


def _mkbuf(events=None, *, dropped=0, done=False):
    return {
        "events": events or [],
        "dropped": dropped,
        "done": done,
        "nudge": asyncio.Event(),
    }


def test_follow_replays_buffered_events_then_terminates_on_done():
    """首连：先读到已缓冲事件，done 后自然终止。"""

    async def scenario():
        buf = _mkbuf([
            {"event": "a", "data": {"i": 1}},
            {"event": "b", "data": {"i": 2}},
        ])
        gen = _follow(buf)
        first = await gen.__anext__()
        assert first["event"] == "a"
        assert '"i"' in first["data"]
        second = await gen.__anext__()
        assert second["event"] == "b"
        # 此时 _follow 应在等 nudge；标记 done 并 nudge 让它退出
        buf["done"] = True
        buf["nudge"].set()
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

    asyncio.run(scenario())


def test_follow_reconnect_replays_from_start_when_already_done():
    """断线重连：buf 已 done，新 _follow 从头回放全部事件后终止。"""

    async def scenario():
        buf = _mkbuf([
            {"event": "turn_started", "data": {"turn_id": "t1"}},
            {"event": "step_started", "data": {"step": 1}},
            {"event": "done", "data": {}},
        ], done=True)
        gen = _follow(buf)
        events = []
        async for e in gen:
            events.append(e["event"])
        assert events == ["turn_started", "step_started", "done"]

    asyncio.run(scenario())


def test_follow_handles_dropped_buffer():
    """缓冲被裁剪（dropped>0）：跳到现存最早事件，不报错。"""

    async def scenario():
        buf = _mkbuf([{"event": "late", "data": {}}], dropped=5, done=True)
        gen = _follow(buf)
        events = []
        async for e in gen:
            events.append(e["event"])
        assert events == ["late"]

    asyncio.run(scenario())


def test_follow_picks_up_events_appended_mid_stream():
    """跟读过程中生产者追加事件：nudge 唤醒后能读到新事件。"""

    async def scenario():
        buf = _mkbuf([{"event": "first", "data": {}}])
        gen = _follow(buf)
        first = await gen.__anext__()
        assert first["event"] == "first"
        # 模拟生产者追加 + nudge
        buf["events"].append({"event": "second", "data": {}})
        buf["nudge"].set()
        second = await gen.__anext__()
        assert second["event"] == "second"
        # 再 nudge + done 收尾
        buf["done"] = True
        buf["nudge"].set()
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

    asyncio.run(scenario())


def test_follow_reader_disconnect_does_not_corrupt_buffer():
    """读者断开（gen.aclose）只终止本生成器，buf 状态不被破坏。"""

    async def scenario():
        buf = _mkbuf([
            {"event": "a", "data": {}},
            {"event": "b", "data": {}},
        ], done=False)
        gen = _follow(buf)
        await gen.__anext__()  # 读 a
        # 模拟客户端断开
        await gen.aclose()
        # buf 仍可被新读者重连回放
        assert len(buf["events"]) == 2
        assert buf["done"] is False
        # 新读者从头回放
        gen2 = _follow(buf)
        events = []
        buf["done"] = True
        buf["nudge"].set()
        async for e in gen2:
            events.append(e["event"])
        assert events == ["a", "b"]

    asyncio.run(scenario())
