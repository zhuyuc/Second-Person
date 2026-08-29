"""Δ1 + Δ2 回归：极短寒暄携带上下文时短路，refine 判空不再注入 extra_related。

覆盖场景：
1. "你好" + 上一话题 context_text → 整条链路短路，0 记忆
2. "你好" 但无 context_text → 走完整链路（不短路，保留 fresh 语义）
3. refine 判空 + 有图邻居 → 不再强行注入 extra_related（Δ2）
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np  # noqa: E402

from memory.retriever import Retriever  # noqa: E402


class _FakeDB:
    def __init__(self, links=None):
        self._links = links or []

    def query_all(self, sql, params=()):
        s = " ".join(sql.split())
        if "memory_links" in s:
            n = (len(params) or 0) // 2
            seed_ids = set(params[:n])
            return [{"source_id": src, "target_id": tgt, "link_type": ltype}
                    for src, tgt, ltype in self._links
                    if src in seed_ids or tgt in seed_ids]
        return []

    def query_one(self, sql, params=()):
        return None

    def execute(self, sql, params=()):
        # E7 负样本反馈会调 execute；这里静默接受
        return None


class _FakeVS:
    loaded = True

    def __init__(self, hits, id_to_vec=None):
        self._hits = hits
        self._id_to_vec = id_to_vec or {}
        self._id_to_index = {mid: i for i, mid in enumerate(self._id_to_vec)}
        if self._id_to_vec:
            self._matrix = np.vstack(list(self._id_to_vec.values())).astype(np.float32)
            self.dim = self._matrix.shape[1]
        else:
            self._matrix = None
            self.dim = 4

    def search(self, query_vec, top_k, threshold):
        return self._hits[:top_k]


class _FakePalace:
    def __init__(self, rows):
        self._rows = rows

    def get(self, mid):
        return self._rows.get(mid)

    def get_many(self, ids):
        return {mid: self._rows[mid] for mid in ids if mid in self._rows}


class _Cfg(dict):
    def get(self, k, d=None):
        return super().get(k, d)


def _row(mid, **overrides):
    base = {"id": mid, "title": f"标题{mid}", "summary": f"摘要{mid}",
            "lifecycle": "active", "source_type": "memory",
            "confidence": "medium", "md_path": f"memories/x/{mid}.md",
            "verification_state": "direct", "freshness_state": "current",
            "is_important": 0, "retrieval_negative_count": 0,
            "created_at": "2026-01-01"}
    base.update(overrides)
    return base


def _mk(tmp, vs, palace, refine, embed_calls, db=None, cfg=None):
    async def embed_fn(texts):
        embed_calls.extend(texts)
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]
    return Retriever(db or _FakeDB(), vs, palace, cfg or _Cfg(), tmp,
                     embed_fn=embed_fn, llm_refine_fn=refine)


async def main() -> int:
    tmp = Path(__file__).parent
    failures = []
    hits = [("mem_a", 0.61), ("mem_b", 0.58)]
    rows = {mid: _row(mid) for mid in ("mem_a", "mem_b", "mem_c")}

    # ---- 1. "你好" + context_text → 短路 ----
    async def refine_all(query, cands, session_id=None, context_text=None):
        return [c["id"] for c in cands]
    embed_calls: list[str] = []
    r = _mk(tmp, _FakeVS(hits), _FakePalace(rows), refine_all, embed_calls)
    res = await r.retrieve("你好",
                            context_text="用户：我们之前聊过 AI 模型配置...")
    if res.hits or res.related or res.loaded_ids:
        failures.append(f"极短寒暄携带上下文应短路，实际注入 {len(res.loaded_ids)} 条")
    if res.diagnostics.get("gate") != "short_query_shortcircuit":
        failures.append(
            f"短路时 gate 应为 short_query_shortcircuit，"
            f"实际 {res.diagnostics.get('gate')}")
    if embed_calls:
        failures.append("短路时不应触发 embedding 调用")

    # ---- 2. "你好" 无 context → 走完整链路 ----
    embed_calls = []
    r = _mk(tmp, _FakeVS(hits), _FakePalace(rows), refine_all, embed_calls)
    res = await r.retrieve("你好", context_text=None)
    if res.diagnostics.get("gate") == "short_query_shortcircuit":
        failures.append("无 context 时不应短路（fresh 寒暄需要走完整链路）")
    if not embed_calls:
        failures.append("无 context 时应正常调用 embedding")

    # ---- 3. Δ2：refine 判空 + 图邻居存在 → 不再强行注入 extra_related ----
    async def refine_empty(query, cands, session_id=None, context_text=None):
        return []
    id_to_vec = {mid: [1.0, 0.0, 0.0, 0.0] for mid in rows}
    vs = _FakeVS(hits, id_to_vec=id_to_vec)
    # 造 links：mem_a → mem_c（图扩展会拉出 mem_c）
    db = _FakeDB(links=[("mem_a", "mem_c", "related")])
    r = _mk(tmp, vs, _FakePalace(rows), refine_empty, [], db=db,
             cfg=_Cfg(graph_expand_seed_threshold=0.5))
    res = await r.retrieve("方案3，按上面的格式更新到文档")
    if res.related or res.loaded_ids:
        failures.append(
            f"Δ2：refine 判空时不应注入 extra_related，"
            f"实际注入 {len(res.loaded_ids)} 条 "
            f"(related_ids={[r['id'] for r in res.related]})")
    if res.diagnostics.get("gate") != "refine_empty":
        failures.append(
            f"refine 判空时 gate 应为 refine_empty，"
            f"实际 {res.diagnostics.get('gate')}")

    for f in failures:
        print("FAIL:", f)
    print("PASS" if not failures else f"{len(failures)} failures")
    return 1 if failures else 0


def test_short_query_shortcut():
    code = asyncio.run(main())
    assert code == 0, "见 stdout 的 FAIL 行"


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
