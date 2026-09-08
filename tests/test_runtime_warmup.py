"""RuntimeWarmer 预热合并与失败隔离。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from infrastructure.runtime_warmup import RuntimeWarmer


class _FakeProviders:
    def __init__(self, snap="chat-snap"):
        self._snap = snap

    def snapshot_for(self, key):
        if key in ("chat", "agent") and self._snap:
            return self._snap
        return None


class _FakeLLM:
    def __init__(self):
        self.probe_calls = 0

    async def probe(self, snap):
        self.probe_calls += 1
        return {"ok": True, "protocol": "chat"}


class _FakeRetriever:
    def __init__(self, refine_fn):
        self.llm_refine_fn = refine_fn


class _FakeContainer:
    def __init__(self, *, embed_ok=True, refine_ok=True, chat_ok=True):
        self.embed_calls = 0
        self.refine_calls = 0

        async def embed_fn(texts):
            self.embed_calls += 1
            if not embed_ok:
                raise RuntimeError("embed down")
            return [[0.1, 0.2] for _ in texts]

        async def refine_fn(query, cands, session_id=None, context_text=None):
            self.refine_calls += 1
            if not refine_ok:
                raise RuntimeError("refine down")
            return []

        self.embed_fn = embed_fn
        self.retriever = _FakeRetriever(refine_fn)
        self.providers = _FakeProviders(snap="s" if chat_ok else None)
        self.llm = _FakeLLM()
        if not chat_ok:
            async def boom(snap):
                raise RuntimeError("chat down")
            self.llm.probe = boom  # type: ignore[method-assign]


def test_warmup_runs_three_paths():
    async def scenario():
        c = _FakeContainer()
        warmer = RuntimeWarmer(c, coalesce_seconds=0)
        out = await warmer.warm(reason="test")
        assert out["embed"]["ok"] is True
        assert out["refine"]["ok"] is True
        assert out["chat"]["ok"] is True
        assert c.embed_calls == 1
        assert c.refine_calls == 1
        assert c.llm.probe_calls == 1

    asyncio.run(scenario())


def test_warmup_failures_isolated():
    async def scenario():
        c = _FakeContainer(embed_ok=False, refine_ok=False, chat_ok=False)
        warmer = RuntimeWarmer(c, coalesce_seconds=0)
        out = await warmer.warm(reason="test")
        assert out["embed"]["ok"] is False
        assert out["refine"]["ok"] is False
        assert out["chat"]["ok"] is False

    asyncio.run(scenario())


def test_warmup_coalesces_within_window():
    async def scenario():
        c = _FakeContainer()
        warmer = RuntimeWarmer(c, coalesce_seconds=60)
        first = await warmer.warm(reason="a")
        second = await warmer.warm(reason="b")
        assert first.get("skipped") is None
        assert second.get("skipped") == "coalesced"
        assert c.embed_calls == 1

    asyncio.run(scenario())
