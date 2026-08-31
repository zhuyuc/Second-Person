"""memory_progress SSE + retrieve on_progress 回归。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.retriever import Retriever, short_circuit_gate
from memory.retriever_progress import done_summary, skip_summary


class _Cfg(dict):
    def get(self, k, d=None):
        return super().get(k, d)


class _FakeDB:
    def query_all(self, sql, params=()):
        return []

    def query_one(self, sql, params=()):
        return None

    def execute(self, sql, params=()):
        return None


class _FakeVS:
    loaded = False
    dim = 0


class _FakePalace:
    def get_many(self, ids):
        return {}


def test_short_circuit_gate_ack_and_history_ref():
    assert short_circuit_gate("谢谢", "some context", 3) == "ack_shortcut"
    assert short_circuit_gate("我的偏好是什么", "ctx", 3) is None
    assert short_circuit_gate("你好", "ctx", 3) == "short_query_shortcircuit"
    assert short_circuit_gate("你还记得吗", "ctx", 3) is None


def test_done_summary_allows_zero_hits():
    assert "0" not in done_summary(0)
    assert "不注入" in done_summary(0)
    assert "3 条相关记忆" in done_summary(3)


def test_skip_summary_contains_query_snippet():
    s = skip_summary("ack_shortcut", "谢谢")
    assert "谢谢" in s
    assert "ack_shortcut" not in s or "确认" in s


def test_retrieve_emits_real_progress_on_short_circuit():
    events: list[dict] = []

    async def on_progress(payload):
        events.append(dict(payload))

    async def scenario():
        r = Retriever(_FakeDB(), _FakeVS(), _FakePalace(), _Cfg(), Path("."))
        result = await r.retrieve(
            "谢谢", session_id="s1", context_text="prior talk",
            on_progress=on_progress)
        assert result.diagnostics["gate"] == "ack_shortcut"
        assert result.hits == []
        assert len(events) == 1
        assert events[0]["stage"] == "skipped"
        assert events[0]["hit_count"] == 0
        assert "谢谢" in events[0]["summary"]

    asyncio.run(scenario())
