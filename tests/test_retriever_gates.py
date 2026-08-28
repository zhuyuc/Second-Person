"""Retriever 新契约测试（图谱化检索，无开关分支）。

保护契约：
1. 精筛判空 → 0 注入 (gate=refine_empty)
2. 精筛异常 → 按相对得分兜底（不问意图）
3. 图扩展 links：related/evolved/contradicts 全类边常态化参与
4. 图扩展实体：seed 与邻居共享实体 → 邻居进候选池
5. 扩散门槛：与 seed 的余弦而不是与 query 的余弦
6. 上下文常态化参与 embed（不依赖意图关键词）
7. hits + related 分组返回
运行：python tests/test_retriever_gates.py 或 pytest tests/test_retriever_gates.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np  # noqa: E402

from memory.retriever import Retriever  # noqa: E402


class _FakeDB:
    """按 SQL 前缀分派的最小 DB fake，供图扩展测试用。"""

    def __init__(self, links=None, entity_links=None, entities=None):
        self._links = links or []               # [(source, target, link_type), ...]
        self._entity_links = entity_links or [] # [(memory_id, entity_id), ...]
        self._entities = entities or []          # 可选实体元表

    def query_all(self, sql, params=()):
        s = " ".join(sql.split())
        if "memory_links" in s:
            # SELECT source_id, target_id, link_type FROM memory_links
            #   WHERE source_id IN (...) OR target_id IN (...)
            n = (len(params) or 0) // 2
            seed_ids = set(params[:n])
            out = []
            for src, tgt, ltype in self._links:
                if src in seed_ids or tgt in seed_ids:
                    out.append({"source_id": src, "target_id": tgt,
                                "link_type": ltype})
            return out
        if "SELECT DISTINCT entity_id FROM memory_entity_links" in s:
            seed_ids = set(params or ())
            out = {mid_e[1] for mid_e in self._entity_links if mid_e[0] in seed_ids}
            return [{"entity_id": e} for e in out]
        if "GROUP BY l.memory_id" in s:
            # 邻居 JOIN + 排除 seed
            # params = entity_ids + seed_ids + (cap,)
            cap = params[-1]
            rest = params[:-1]
            # 反解不易，用简单办法：从右往左先取 seed，再取 entity
            # entity_ids 长度 = len(_entities in seed) 我们不知道；
            # 用另一策略：直接扫全表按 seed 排除、按 shared 分组
            # 这里 seed_ids 在 params 中间到末尾；把已在 _entity_links 里的 seed 集合算出来
            seed_ids = {
                mid for mid, _ in self._entity_links
                if mid in rest
            }
            # 简化：取所有 entity_links 中不在 seed 里的 memory 作为邻居
            counter = {}
            for mid, eid in self._entity_links:
                if mid in seed_ids:
                    continue
                counter[mid] = counter.get(mid, 0) + 1
            ordered = sorted(counter.items(), key=lambda kv: -kv[1])[:cap]
            return [{"memory_id": mid, "shared": n} for mid, n in ordered]
        return []

    def query_one(self, sql, params=()):
        return None


class _FakeVS:
    """支持 _id_to_index / _matrix 以走 seed 相似度门槛的最小 VS。"""

    loaded = True

    def __init__(self, hits, id_to_vec=None):
        self._hits = hits              # [(mid, score)]
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

    def cosine_to(self, mid, seed_vec):
        vec = self._id_to_vec.get(mid)
        if vec is None or seed_vec is None:
            return None
        a = np.asarray(vec, dtype=np.float32)
        b = np.asarray(seed_vec, dtype=np.float32)
        denom = (np.linalg.norm(a) + 1e-8) * (np.linalg.norm(b) + 1e-8)
        return float(a @ b / denom)


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


def _row(mid, lifecycle="active", **overrides):
    base = {"id": mid, "title": f"标题{mid}", "summary": f"摘要{mid}",
            "lifecycle": lifecycle, "source_type": "memory",
            "confidence": "medium", "md_path": f"memories/x/{mid}.md",
            "verification_state": "direct", "freshness_state": "current",
            "is_important": 0, "retrieval_negative_count": 0,
            "created_at": "2026-01-01"}
    base.update(overrides)
    return base


def _mk_retriever(tmp, vs, palace, refine_fn, embed_calls,
                   db=None, cfg=None):
    async def embed_fn(texts):
        embed_calls.extend(texts)
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]
    return Retriever(db or _FakeDB(), vs, palace, cfg or _Cfg(), tmp,
                     embed_fn=embed_fn, llm_refine_fn=refine_fn)


async def main() -> int:
    tmp = Path(__file__).parent
    failures = []
    hits = [("mem_a", 0.61), ("mem_b", 0.58)]
    rows = {mid: _row(mid) for mid in ("mem_a", "mem_b", "mem_c", "mem_d")}

    # ---- 1. 精筛判空 → 0 注入 + gate=refine_empty ----
    async def refine_empty(query, cands, session_id=None, context_text=None):
        return []
    embed_calls: list[str] = []
    r = _mk_retriever(tmp, _FakeVS(hits), _FakePalace(rows),
                      refine_empty, embed_calls)
    res = await r.retrieve("方案3，按上面的格式更新到文档",
                            context_text="用户：备份方案讨论")
    if res.hits or res.related or res.loaded_ids:
        failures.append(f"精筛判空仍注入 {len(res.loaded_ids)} 条")
    if res.diagnostics.get("gate") != "refine_empty":
        failures.append(
            f"gate 应为 refine_empty，实际 {res.diagnostics.get('gate')}")

    # ---- 2. 精筛异常 → 按相对得分兜底（不问意图）----
    async def refine_boom(query, cands, session_id=None, context_text=None):
        raise RuntimeError("llm down")
    r = _mk_retriever(tmp, _FakeVS(hits), _FakePalace(rows), refine_boom, [])
    res = await r.retrieve("随便问点什么")  # 普通提问也要兜底
    if not res.hits and not res.related:
        failures.append(
            f"精筛异常应按得分兜底注入 ≥1 条，实际 hits=0 related=0")
    if "兜底" not in res.degraded and "不可用" not in res.degraded:
        failures.append(f"精筛异常应打降级标记：{res.degraded!r}")

    # ---- 3. 图扩展 links：全类边常态化 ----
    async def refine_all(query, cands, session_id=None, context_text=None):
        return [c["id"] for c in cands]  # 精筛全放行以观察扩展效果
    # 造边：mem_a -> mem_c (related), mem_b -> mem_d (evolved_from)
    links = [("mem_a", "mem_c", "related"),
             ("mem_b", "mem_d", "evolved_from")]
    # 造向量让 seed 相似度门槛能过：seed 与邻居向量强相关
    id_to_vec = {mid: [1.0, 0.0, 0.0, 0.0] for mid in rows}
    vs = _FakeVS(hits, id_to_vec=id_to_vec)
    db = _FakeDB(links=links)
    r = _mk_retriever(tmp, vs, _FakePalace(rows), refine_all, [],
                      db=db, cfg=_Cfg(graph_expand_seed_threshold=0.5))
    res = await r.retrieve("问题")
    ids = set(res.loaded_ids)
    if "mem_c" not in ids:
        failures.append("图扩展 related 边未生效（mem_c 未注入）")
    if "mem_d" not in ids:
        failures.append("图扩展 evolved_from 边未生效（mem_d 未注入）")

    # ---- 4. 扩散门槛：与 seed 相似度，低于阈值 → 不注入 ----
    id_to_vec_low = {"mem_a": [1.0, 0.0, 0.0, 0.0],
                     "mem_b": [1.0, 0.0, 0.0, 0.0],
                     "mem_c": [0.0, 1.0, 0.0, 0.0],  # 与 mem_a 正交
                     "mem_d": [0.99, 0.14, 0.0, 0.0]}
    vs = _FakeVS(hits, id_to_vec=id_to_vec_low)
    r = _mk_retriever(tmp, vs, _FakePalace(rows), refine_all, [],
                      db=_FakeDB(links=links),
                      cfg=_Cfg(graph_expand_seed_threshold=0.6))
    res = await r.retrieve("问题")
    ids = set(res.loaded_ids)
    if "mem_c" in ids:
        failures.append("seed 相似度门槛失效：正交邻居被注入")
    if "mem_d" not in ids:
        failures.append("seed 相似度门槛误伤：高相似邻居未注入")

    # ---- 5. 上下文常态化参与 embed（不看意图）----
    embed_calls = []
    r = _mk_retriever(tmp, _FakeVS(hits), _FakePalace(rows),
                      refine_empty, embed_calls)
    await r.retrieve("完全不含意图关键词的普通问题",
                      context_text="上下文历史")
    if not embed_calls:
        failures.append("embed 未被调用")
    elif "上下文历史" not in embed_calls[0]:
        failures.append(f"上下文应常态化参与 embed：{embed_calls[0]!r}")

    # ---- 6. 图扩展实体：共实体邻居被拉进候选 ----
    entity_links = [("mem_a", "ent_alice"), ("mem_e", "ent_alice")]
    rows_with_e = dict(rows)
    rows_with_e["mem_e"] = _row("mem_e")
    id_to_vec_ent = {mid: [1.0, 0.0, 0.0, 0.0]
                     for mid in list(rows_with_e) + ["mem_e"]}
    vs = _FakeVS(hits, id_to_vec=id_to_vec_ent)
    db = _FakeDB(entity_links=entity_links)
    r = _mk_retriever(tmp, vs, _FakePalace(rows_with_e), refine_all, [],
                      db=db, cfg=_Cfg(graph_expand_seed_threshold=0.5))
    res = await r.retrieve("张三")
    ids = set(res.loaded_ids)
    if "mem_e" not in ids:
        failures.append("实体图未召回共实体邻居 mem_e")

    # ---- 7. hits vs related 分组 ----
    async def refine_a_only(query, cands, session_id=None, context_text=None):
        return ["mem_a", "mem_c"]  # mem_c 是图扩展节点
    id_to_vec = {mid: [1.0, 0.0, 0.0, 0.0] for mid in rows}
    vs = _FakeVS(hits, id_to_vec=id_to_vec)
    r = _mk_retriever(tmp, vs, _FakePalace(rows), refine_a_only, [],
                      db=_FakeDB(links=[("mem_a", "mem_c", "related")]),
                      cfg=_Cfg(graph_expand_seed_threshold=0.5))
    res = await r.retrieve("问题")
    hits_ids = {h["id"] for h in res.hits}
    rel_ids = {r["id"] for r in res.related}
    if "mem_a" not in hits_ids:
        failures.append("主命中未进 hits")
    if "mem_c" not in rel_ids:
        failures.append("图扩展节点未进 related")
    if "mem_c" in hits_ids:
        failures.append("图扩展节点错进 hits")

    for f in failures:
        print("FAIL:", f)
    print("PASS" if not failures else f"{len(failures)} failures")
    return 1 if failures else 0


def test_retriever_gates_contract():
    """pytest 入口。"""
    code = asyncio.run(main())
    assert code == 0, "见 stdout 的 FAIL 行"


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
