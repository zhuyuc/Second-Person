"""v7 精筛提速三项改动的单元覆盖：

1. 候选池硬帽 CANDIDATE_POOL_HARD_CAP=10 生效（送 LLM 的 payload 只有前 10 条）
2. 精筛结果 LRU cache：同 (session_id, query, candidate_ids) 命中不再调 LLM
3. TTL 过期后自动重跑；LRU 上限触发时最老项被淘汰
4. cache key 对候选顺序不敏感（sorted），对不同 session/query 敏感

这些改动的直接收益：turn 内异常重试 + 用户 regenerate 场景下省 2-3s LLM 精筛。
"""
from __future__ import annotations

import asyncio
import time as _time
from unittest.mock import patch

import pytest

from memory import _constants as _mem_const
from memory.retriever import Candidate, RetrievalResult, Retriever


class _Config:
    def __init__(self, **overrides):
        self.overrides = overrides
    def get(self, k, default=None):
        return self.overrides.get(k, default)


def _make_candidate(mid: str, score: float = 0.9) -> Candidate:
    return Candidate(memory_id=mid, title=f"标题 {mid}", summary=f"内容 {mid}",
                      lifecycle="active", final_score=score)


def _retriever(refine_fn=None, **cfg_overrides):
    return Retriever(db=None, vector_store=None, palace=None,
                      config=_Config(**cfg_overrides), data_dir=".",
                      llm_refine_fn=refine_fn)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ------------------------------------------------------------- pool cap

def test_pool_cap_limits_payload_to_first_ten():
    """候选池 15 条 → 精筛只看到前 10 条（硬帽 10 生效）。"""
    seen_payloads: list[list[dict]] = []

    async def refine(query, payload, session_id=None, context_text=None):
        seen_payloads.append(payload)
        return [p["id"] for p in payload[:3]]

    r = _retriever(refine)
    cands = [_make_candidate(f"m{i}", 1.0 - i * 0.05) for i in range(15)]
    result = RetrievalResult()
    picked, _path = _run(r._refine("q", cands, session_id="s", context_text=None, result=result))

    assert len(seen_payloads) == 1
    assert len(seen_payloads[0]) == 10   # ← 硬帽起作用
    assert seen_payloads[0][0]["id"] == "m0"
    assert seen_payloads[0][-1]["id"] == "m9"
    assert set(picked) == {"m0", "m1", "m2"}


def test_pool_cap_config_override():
    """支持 config 覆盖 candidate_pool_hard_cap（运维可以微调）。"""
    seen: list[list[dict]] = []
    async def refine(query, payload, session_id=None, context_text=None):
        seen.append(payload); return []
    r = _retriever(refine, candidate_pool_hard_cap=3)
    cands = [_make_candidate(f"m{i}") for i in range(15)]
    _run(r._refine("q", cands, session_id="s", context_text=None, result=RetrievalResult()))
    assert len(seen[0]) == 3


# ------------------------------------------------------------- cache hit

def test_cache_hit_skips_llm_call():
    """同 session + 同 query + 同候选池 → 第二次不再调 LLM。"""
    call_count = 0
    async def refine(query, payload, session_id=None, context_text=None):
        nonlocal call_count
        call_count += 1
        return [payload[0]["id"], payload[1]["id"]]

    r = _retriever(refine)
    cands = [_make_candidate(f"m{i}") for i in range(5)]
    first_ids, first_path = _run(r._refine("q1", cands, session_id="s1", context_text=None, result=RetrievalResult()))
    second_ids, second_path = _run(r._refine("q1", cands, session_id="s1", context_text=None, result=RetrievalResult()))
    assert first_ids == second_ids == ["m0", "m1"]
    assert first_path == "full"
    assert second_path == "refine_cache"
    assert call_count == 1    # ← 第二次命中 cache，不再调 LLM


def test_cache_miss_on_different_query():
    """不同 query → 不同 cache key → 两次都调 LLM。"""
    count = 0
    async def refine(query, payload, session_id=None, context_text=None):
        nonlocal count; count += 1
        return [payload[0]["id"]]
    r = _retriever(refine)
    cands = [_make_candidate("m0"), _make_candidate("m1")]
    _run(r._refine("query A", cands, session_id="s", context_text=None, result=RetrievalResult()))
    _run(r._refine("query B", cands, session_id="s", context_text=None, result=RetrievalResult()))
    assert count == 2


def test_cache_miss_on_different_session():
    """不同 session_id → 不同 cache key。"""
    count = 0
    async def refine(query, payload, session_id=None, context_text=None):
        nonlocal count; count += 1
        return [payload[0]["id"]]
    r = _retriever(refine)
    cands = [_make_candidate("m0"), _make_candidate("m1")]
    _run(r._refine("q", cands, session_id="s1", context_text=None, result=RetrievalResult()))
    _run(r._refine("q", cands, session_id="s2", context_text=None, result=RetrievalResult()))
    assert count == 2


def test_cache_key_is_order_insensitive():
    """候选顺序不同（预筛排序抖动）不应影响 cache key。"""
    count = 0
    async def refine(query, payload, session_id=None, context_text=None):
        nonlocal count; count += 1
        return [payload[0]["id"]]
    r = _retriever(refine)
    ca = [_make_candidate("a"), _make_candidate("b"), _make_candidate("c")]
    cb = [_make_candidate("c"), _make_candidate("a"), _make_candidate("b")]
    _run(r._refine("q", ca, session_id="s", context_text=None, result=RetrievalResult()))
    _run(r._refine("q", cb, session_id="s", context_text=None, result=RetrievalResult()))
    assert count == 1   # 顺序不同但 id 集合相同 → 命中


# ------------------------------------------------------------- TTL

def test_cache_expires_after_ttl():
    """TTL 过后 cache 失效，重新调用 LLM。"""
    count = 0
    async def refine(query, payload, session_id=None, context_text=None):
        nonlocal count; count += 1
        return [payload[0]["id"]]
    r = _retriever(refine, retriever_refine_cache_ttl_seconds=1)
    cands = [_make_candidate("m0"), _make_candidate("m1")]
    _run(r._refine("q", cands, session_id="s", context_text=None, result=RetrievalResult()))
    # 模拟时间跳跃：直接篡改 cache 里的 ts（避免真的 sleep 慢跑测试）
    key = next(iter(r._refine_cache))
    ids, _ = r._refine_cache[key]
    r._refine_cache[key] = (ids, _time.monotonic() - 3600)   # 一小时前
    _run(r._refine("q", cands, session_id="s", context_text=None, result=RetrievalResult()))
    assert count == 2


def test_cache_lru_evicts_oldest():
    """超过 cache size 时，最老 entry 被 pop。"""
    async def refine(query, payload, session_id=None, context_text=None):
        return [payload[0]["id"]]
    r = _retriever(refine, retriever_refine_cache_size=3)
    for i in range(5):
        cands = [_make_candidate(f"m{i}a"), _make_candidate(f"m{i}b")]
        _run(r._refine(f"q{i}", cands, session_id="s",
                        context_text=None, result=RetrievalResult()))
    # 只留最近 3 条
    assert len(r._refine_cache) == 3
    keys = [k for k in r._refine_cache]
    # 最老的 q0/q1 被淘汰
    assert not any(k[1] == "q0" for k in keys)
    assert not any(k[1] == "q1" for k in keys)


# ------------------------------------------------------------- fail path

def test_cache_not_populated_on_timeout():
    """LLM 精筛超时 → 走降级，cache 不记录（下次仍需重跑，避免污染）。"""
    call_count = 0
    async def slow_refine(query, payload, session_id=None, context_text=None):
        nonlocal call_count; call_count += 1
        await asyncio.sleep(0.5)
        return ["m0"]

    r = _retriever(slow_refine, retrieval_refine_timeout_seconds=0.05)
    cands = [_make_candidate(f"m{i}") for i in range(3)]
    result = RetrievalResult()
    _run(r._refine("q", cands, session_id="s", context_text=None, result=result))
    assert "超时" in result.degraded or "兜底" in result.degraded
    assert len(r._refine_cache) == 0   # ← 超时不落 cache


def test_cache_not_populated_on_exception():
    """LLM 抛异常 → 降级 + cache 不污染。"""
    async def boom(query, payload, session_id=None, context_text=None):
        raise RuntimeError("provider down")

    r = _retriever(boom)
    cands = [_make_candidate("m0")]
    result = RetrievalResult()
    _run(r._refine("q", cands, session_id="s", context_text=None, result=result))
    assert len(r._refine_cache) == 0
