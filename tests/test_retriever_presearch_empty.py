"""预筛 0 候选时必须跳过图扩展与精筛；有候选时才走精筛。"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from memory.retriever import Retriever


class _FakeDB:
    def query_all(self, sql, params=()):
        return []

    def query_one(self, sql, params=()):
        return None

    def execute(self, sql, params=()):
        return None


class _FakeVS:
    loaded = True
    dim = 4
    migrating = False

    def __init__(self, hits=None):
        self._hits = hits or []
        self._matrix = None
        self._id_to_index = {}

    def search(self, query_vec, top_k, threshold):
        return list(self._hits)[:top_k]


class _FakePalace:
    def get_many(self, ids):
        return {}

    def get(self, mid):
        return None


class _Cfg(dict):
    def get(self, k, d=None):
        return super().get(k, d)


@pytest.mark.asyncio
async def test_presearch_empty_skips_graph_and_refine(tmp_path: Path):
    refine_calls = []
    expand_calls = []

    async def refine_fn(query, cands, session_id=None, context_text=None):
        refine_calls.append(len(cands))
        return [c["id"] for c in cands]

    async def embed_fn(texts):
        return [[0.0, 0.0, 0.0, 0.0] for _ in texts]

    r = Retriever(
        _FakeDB(), _FakeVS(hits=[]), _FakePalace(),
        _Cfg(), tmp_path,
        embed_fn=embed_fn, llm_refine_fn=refine_fn,
    )
    r._fts_search = lambda *a, **k: []  # noqa: E731
    orig_expand = r._expand_graph

    async def tracked_expand(*a, **k):
        expand_calls.append(1)
        return await orig_expand(*a, **k)

    r._expand_graph = tracked_expand

    stages = []

    async def on_progress(payload):
        stages.append(payload.get("stage"))

    res = await r.retrieve("你好", context_text=None, on_progress=on_progress)
    assert res.hits == []
    assert res.related == []
    assert res.diagnostics.get("gate") == "presearch_empty"
    assert refine_calls == [], "预筛空时不得调用精筛"
    assert expand_calls == [], "预筛空时不得图扩展"
    assert "refine" not in stages
    assert "graph" not in stages
    assert stages[-1] == "done"

    payloads = []

    async def capture(payload):
        payloads.append(payload)

    await r.retrieve("你好", context_text=None, on_progress=capture)
    done = [p for p in payloads if p.get("stage") == "done"][-1]
    assert done.get("gate") == "presearch_empty"
    assert "跳过图扩展与精筛" in done.get("summary", "")


@pytest.mark.asyncio
async def test_presearch_hit_still_runs_refine(tmp_path: Path):
    refine_calls = []

    async def refine_fn(query, cands, session_id=None, context_text=None):
        refine_calls.append(len(cands))
        return [cands[0]["id"]] if cands else []

    async def embed_fn(texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    row = {
        "id": "mem_a", "title": "标题", "summary": "摘要",
        "lifecycle": "active", "source_type": "memory",
        "confidence": "medium", "md_path": "memories/x/mem_a.md",
        "verification_state": "direct", "freshness_state": "current",
        "project_id": None, "importance": "normal",
        "is_important": 0, "retrieval_negative_count": 0,
        "updated_at": "2099-01-01", "created_at": "2099-01-01",
    }

    class Palace:
        def get_many(self, ids):
            return {"mem_a": row} if "mem_a" in ids else {}

        def get(self, mid):
            return row if mid == "mem_a" else None

    vs = _FakeVS(hits=[("mem_a", 0.92)])
    vs._id_to_index = {"mem_a": 0}
    vs._matrix = np.array([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32)

    r = Retriever(
        _FakeDB(), vs, Palace(),
        _Cfg(vector_threshold=0.5, retrieval_refine_min_candidates=1),
        tmp_path,
        embed_fn=embed_fn, llm_refine_fn=refine_fn,
    )
    r._fts_search = lambda *a, **k: []  # noqa: E731
    r._load_detail = lambda mid: dict(row, detail="正文")  # noqa: E731

    stages = []

    async def on_progress(payload):
        stages.append(payload)

    res = await r.retrieve("关于标题的问题", on_progress=on_progress)
    # 预筛选中后进入精筛阶段（full / fast_path / degrade 均可）
    assert any(p.get("stage") == "refine" for p in stages), "预筛选中后应进入精筛阶段"
    assert any(p.get("stage") == "graph" for p in stages), "预筛选中后应图扩展"
    assert res.diagnostics.get("gate") != "presearch_empty"
    assert refine_calls or res.diagnostics.get("refine_path") == "fast_path"
