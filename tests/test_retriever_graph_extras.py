"""共引召回、争议提示、batch matmul 契约。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np  # noqa: E402

from agent.core import AgentCore  # noqa: E402
from memory.retriever import Retriever  # noqa: E402


# ---- 共引召回 ---------------------------------------------------------------

class _CitationDB:
    """支持 links + entity_links + citation_events 的 fake DB。"""

    def __init__(self, links=None, entity_links=None, citations=None):
        self._links = links or []
        self._entity_links = entity_links or []
        self._citations = citations or []  # [(memory_id, message_id, session_id, cited_at)]

    def query_all(self, sql, params=()):
        s = " ".join(sql.split())
        if "citation_events seed JOIN citation_events other" in s and "GROUP BY other.memory_id" in s:
            # co-occurrence 查询：params = seed_ids + seed_ids + (cutoff, cap)
            cap = params[-1]
            cutoff = params[-2]
            # seed_ids 长度 = (len(params)-2)/2
            n = (len(params) - 2) // 2
            seed_ids = set(params[:n])
            # 拉 seed 出现的消息
            seed_msgs = {(m_id, s_id) for mid, m_id, s_id, at in self._citations
                         if mid in seed_ids and at >= cutoff}
            # 找共出现的其它记忆
            counter = {}
            for mid, m_id, s_id, at in self._citations:
                if mid in seed_ids:
                    continue
                if (m_id, s_id) in seed_msgs:
                    counter[mid] = counter.get(mid, 0) + 1
            ordered = sorted(counter.items(), key=lambda kv: -kv[1])[:cap]
            return [{"memory_id": mid, "co_cnt": c} for mid, c in ordered]
        if "memory_links" in s:
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
            cap = params[-1]
            rest = params[:-1]
            seed_ids = {mid for mid, _ in self._entity_links if mid in rest}
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


class _VS:
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

    def cosine_to(self, mid, seed_vec):
        vec = self._id_to_vec.get(mid)
        if vec is None or seed_vec is None:
            return None
        a = np.asarray(vec, dtype=np.float32)
        b = np.asarray(seed_vec, dtype=np.float32)
        denom = (np.linalg.norm(a) + 1e-8) * (np.linalg.norm(b) + 1e-8)
        return float(a @ b / denom)


class _Palace:
    def __init__(self, rows):
        self._rows = rows

    def get(self, mid):
        return self._rows.get(mid)

    def get_many(self, ids):
        return {mid: self._rows[mid] for mid in ids if mid in self._rows}


class _Cfg(dict):
    def get(self, k, d=None):
        return super().get(k, d)


def _row(mid, confidence="medium"):
    return {"id": mid, "title": f"标题{mid}", "summary": f"摘要{mid}",
            "lifecycle": "active", "source_type": "memory",
            "confidence": confidence, "md_path": f"memories/x/{mid}.md",
            "verification_state": "direct", "freshness_state": "current",
            "is_important": 0, "retrieval_negative_count": 0,
            "created_at": "2026-01-01"}


def test_citation_graph_recall():
    """seed 与其它记忆过去被同一 message 引用 → 共引召回带回。"""
    async def scenario():
        tmp = Path(__file__).parent
        hits = [("mem_a", 0.6)]
        rows = {"mem_a": _row("mem_a"), "mem_x": _row("mem_x")}
        # mem_a 与 mem_x 曾在同一条 message 里一起被引用
        citations = [
            ("mem_a", 100, "s1", "2026-06-01T00:00:00"),
            ("mem_x", 100, "s1", "2026-06-01T00:00:00"),
        ]
        id_to_vec = {mid: [1.0, 0.0, 0.0, 0.0] for mid in rows}
        vs = _VS(hits, id_to_vec=id_to_vec)
        db = _CitationDB(citations=citations)
        palace = _Palace(rows)

        async def refine_all(query, cands, session_id=None, context_text=None):
            return [c["id"] for c in cands]

        async def embed(texts):
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

        r = Retriever(db, vs, palace,
                       _Cfg(graph_expand_seed_threshold=0.5,
                            graph_citation_window_days=90),
                       tmp, embed_fn=embed, llm_refine_fn=refine_all)
        # 冻结现在为一个较晚时间，让 window_days=90 覆盖 2026-06-01
        # （retriever 用 now_cst()，测试仅确保逻辑不因窗口失效；实际 CI 时钟
        #  多年后此测试仍需通过——把 window 拉到很大）
        r.config["graph_citation_window_days"] = 36500
        res = await r.retrieve("问题")
        assert "mem_x" in set(res.loaded_ids), \
            f"共引召回失效：loaded={res.loaded_ids}"

    asyncio.run(scenario())


def test_disputed_notice_propagates():
    """本轮候选池里的 disputed 记忆应被收集到 RetrievalResult.disputed。"""
    async def scenario():
        tmp = Path(__file__).parent
        hits = [("mem_a", 0.7), ("mem_d", 0.6)]
        rows = {"mem_a": _row("mem_a"),
                "mem_d": _row("mem_d", confidence="disputed")}
        id_to_vec = {mid: [1.0, 0.0, 0.0, 0.0] for mid in rows}
        vs = _VS(hits, id_to_vec=id_to_vec)

        async def refine_all(query, cands, session_id=None, context_text=None):
            return [c["id"] for c in cands]

        async def embed(texts):
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

        r = Retriever(_CitationDB(), vs, _Palace(rows),
                       _Cfg(graph_expand_seed_threshold=0.5),
                       tmp, embed_fn=embed, llm_refine_fn=refine_all)
        res = await r.retrieve("问题")
        # 主命中 mem_a 正常；disputed mem_d 不注入 hits/related 但在 disputed 里
        assert "mem_d" not in set(res.loaded_ids)
        assert any(d.get("id") == "mem_d" for d in res.disputed), \
            f"disputed 记忆未被收集：{res.disputed}"

    asyncio.run(scenario())


def test_batch_matmul_matches_pairwise():
    """batch matmul 结果与逐条 cosine_to 一致（数值等价性）。"""
    async def scenario():
        tmp = Path(__file__).parent
        # 造多样向量：让部分邻居过阈值、部分不过
        id_to_vec = {
            "seed_1": [1.0, 0.0, 0.0, 0.0],
            "seed_2": [0.0, 1.0, 0.0, 0.0],
            "n_1": [0.99, 0.14, 0.0, 0.0],   # 与 seed_1 高相似
            "n_2": [0.14, 0.99, 0.0, 0.0],   # 与 seed_2 高相似
            "n_3": [0.0, 0.0, 1.0, 0.0],     # 与两 seed 都正交
        }
        vs = _VS([], id_to_vec=id_to_vec)
        r = Retriever(_CitationDB(), vs, _Palace({}), _Cfg(), tmp)

        from memory.retriever import Candidate
        neighbors = [
            Candidate("n_1", "", "", "active", from_seed="seed_1"),
            Candidate("n_2", "", "", "active", from_seed="seed_2"),
            Candidate("n_3", "", "", "active", from_seed="seed_1"),
        ]
        kept = r._filter_by_seed_similarity(neighbors, threshold=0.5)
        kept_ids = {c.memory_id for c in kept}
        assert "n_1" in kept_ids  # 与 seed_1 ≈ 0.99 → 过
        assert "n_2" in kept_ids  # 与 seed_2 ≈ 0.99 → 过
        assert "n_3" not in kept_ids  # 与 seed_1 = 0 → 被砍

    asyncio.run(scenario())


def test_compose_context_includes_disputed_and_co_cited():
    """core._compose_memory_context 应拼进 [共引记忆] 与 [争议提醒]。"""
    hits = [{"id": "mem_a", "title": "偏好", "detail": "偏好直接沟通"}]
    related = [
        {"id": "mem_x", "title": "上季度回顾", "summary": "回顾内容",
         "relation": "co_cited", "from_seed": "mem_a"},
        {"id": "mem_y", "title": "旧观点", "summary": "旧内容",
         "relation": "evolved_from", "from_seed": "mem_a"},
    ]
    disputed = [{"id": "mem_z", "title": "有争议的偏好",
                 "summary": "内容"}]
    text = AgentCore._compose_memory_context(hits, related, disputed)
    assert "[核心记忆]" in text
    assert "[演变记忆]" in text
    assert "[共引记忆" in text
    assert "上季度回顾" in text
    assert "[争议提醒" in text
    assert "有争议的偏好" in text


if __name__ == "__main__":
    test_citation_graph_recall()
    test_disputed_notice_propagates()
    test_batch_matmul_matches_pairwise()
    test_compose_context_includes_disputed_and_co_cited()
    print("PASS")
