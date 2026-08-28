"""
Retriever —— 图谱检索（重构后：无开关分支，一条直路）。

链路：
  1. Hybrid 预筛     —— 向量 + FTS5 RRF 融合，得到 candidates
  2. 图扩展          —— 沿 memory_links (out+backlinks) 与 memory_entity_links
                       扩到 1 跳邻居；用与 seed 的语义相似度门槛过滤
  3. 状态标注        —— disputed 硬砍；inferred/expired/review_due 只打标签
                       并做温和降权，交由 LLM 精筛裁决
  4. LLM 精筛        —— 始终跑；异常/未配置才走基于相对得分的兜底
  5. 加载详情 + 拼装 —— 主命中 + 关联记忆按图关系分组返回

设计原则：
  - 无 llm_available / allow_related / is_continuation 之类的布尔分支
  - 状态过滤只在真的必要处（disputed / archived / missing）；软状态交给精筛
  - 图扩展常态化：related / evolved_from / contradicts / entity 共现 都参与
  - 关联记忆的相关性门槛用「vs seed 记忆向量」而不是「vs query 向量」
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from infrastructure.timeutil import now_cst

from .md_file import parse_memory_md

logger = logging.getLogger("second_person.retriever")

# 唯一保留的意图识别：明确回忆语（"你还记得/我之前说过"），仅用于兜底路径
# 调低阈值再跑一次。不用于任何"关门"（不再有 personal / knowledge 分流）。
RECALL_INTENT_PATTERNS = [
    r"你还记得", r"我之前(说过|提过|讲过)", r"我上次", r"还记不记得", r"之前(聊|谈)过",
]


def has_recall_intent(query: str) -> bool:
    return any(re.search(p, query) for p in RECALL_INTENT_PATTERNS)


@dataclass
class Candidate:
    memory_id: str
    title: str
    summary: str
    lifecycle: str
    vector_rank: int | None = None
    bm25_rank: int | None = None
    rrf_score: float = 0.0
    final_score: float = 0.0
    vector_score: float = 0.0
    bm25_score: float = 0.0
    source_type: str = "memory"
    confidence: str = "medium"
    verification_state: str = "unverified"
    freshness_state: str = "current"
    # 图扩展元数据：seed（哪条记忆带出来的）+ 关系类型
    from_seed: str | None = None
    relation: str | None = None       # related / evolved_from / contradicts / entity_shared


@dataclass
class RetrievalResult:
    hits: list[dict] = field(default_factory=list)
    related: list[dict] = field(default_factory=list)
    # F3：本轮候选池里被硬砍的争议记忆（不注入正文，但要让模型/UI 感知
    # 「这里有未裁决的争议」）
    disputed: list[dict] = field(default_factory=list)
    loaded_ids: list[str] = field(default_factory=list)
    degraded: str = ""
    diagnostics: dict = field(default_factory=dict)


@dataclass
class _PresearchResult:
    candidates: list[Candidate] = field(default_factory=list)
    disputed: list[dict] = field(default_factory=list)
    vector_hits: int = 0
    fts_hits: int = 0
    top_vector_score: float = 0.0


class Retriever:
    def __init__(self, db, vector_store, palace, config, data_dir,
                 embed_fn=None, llm_refine_fn=None):
        self.db = db
        self.vs = vector_store
        self.palace = palace
        self.config = config
        self.data_dir = Path(data_dir)
        self.embed_fn = embed_fn
        self.llm_refine_fn = llm_refine_fn

    # ---- 第 1 层 Hybrid 预筛 -------------------------------------------------
    async def hybrid_presearch(self, query: str, query_vec=None,
                               fallback: bool = False) -> _PresearchResult:
        cfg = self.config
        from . import _constants as _mem_const
        vthr = _mem_const.RECALL_FALLBACK_THRESHOLD if fallback \
            else cfg.get("vector_threshold", 0.55)
        bm25_floor = 0.15 if fallback else _mem_const.BM25_RELATIVE_FLOOR
        top_k = cfg.get("retrieval_top_k", 10)
        rrf_k = _mem_const.RRF_K

        vector_hits: list[tuple[str, float]] = []
        if query_vec is not None and self.vs.loaded and self.vs.dim:
            vector_hits = self.vs.search(
                query_vec, top_k=top_k, threshold=vthr)

        fts_hits: list[tuple[str, float]] = self._fts_search(
            query, top_k, bm25_floor)
        n_vec = len(vector_hits)
        n_fts = len(fts_hits)
        top_score = float(vector_hits[0][1]) if vector_hits else 0.0

        if not vector_hits and not fts_hits:
            return _PresearchResult(candidates=[], vector_hits=n_vec, fts_hits=n_fts,
                                    top_vector_score=top_score)

        # RRF 融合
        scores: dict[str, Candidate] = {}
        for rank, (mid, score) in enumerate(vector_hits, start=1):
            c = scores.setdefault(mid, Candidate(mid, "", "", "active"))
            c.vector_rank = rank
            c.vector_score = float(score)
            c.rrf_score += 1.0 / (rrf_k + rank)
        for rank, (mid, score) in enumerate(fts_hits, start=1):
            c = scores.setdefault(mid, Candidate(mid, "", "", "active"))
            c.bm25_rank = rank
            c.bm25_score = float(score)
            c.rrf_score += 1.0 / (rrf_k + rank)

        # Embedding 迁移期单路补偿
        if getattr(self.vs, "migrating", False):
            new_ids = {r["memory_id"] for r in self.db.query_all(
                "SELECT memory_id FROM vectors WHERE embedding_version='new'")}
            for mid, c in scores.items():
                if c.vector_rank is None and c.bm25_rank is not None and mid in new_ids:
                    c.rrf_score *= 1.5

        # 补齐元数据；只做**硬合规**过滤（archived/missing/disputed），
        # 软状态（inferred/expired/review_due）改为温和降权 + 打标签，交给 LLM 精筛
        rows_map = self.palace.get_many(list(scores.keys()))
        out, disputed = self._score_candidates(query, scores, rows_map)
        out.sort(key=lambda x: -x.final_score)
        return _PresearchResult(candidates=out, disputed=disputed,
                                vector_hits=n_vec, fts_hits=n_fts,
                                top_vector_score=top_score)

    def _score_candidates(self, query: str,
                          scores: dict[str, Candidate],
                          rows_map: dict) -> tuple[list[Candidate], list[dict]]:
        """把 RRF 融合得分转成 final_score，含来源/重要/新鲜/负反馈/软状态权重。

        软状态（inferred/expired/review_due）改为**降权**而非静默过滤，
        让 LLM 精筛看得到并自己裁决；disputed 仍硬砍（等待用户裁决），
        但会把 disputed 记忆的元信息收集起来供 F3 争议提醒使用。
        返回 (通过评分的候选, 被硬砍的 disputed 元信息)。
        """
        from . import _constants
        cfg = self.config
        stale_factor = _constants.STALE_SCORE_FACTOR
        important_factor = _constants.IMPORTANT_MEMORY_FACTOR
        freshness_boost = _constants.FRESHNESS_BOOST_FACTOR
        freshness_days = _constants.freshness_boost_days(cfg)
        # 软状态温和降权
        inferred_factor = float(cfg.get("inferred_soft_factor", 0.75))
        expired_factor = float(cfg.get("expired_soft_factor", 0.6))
        fresh_cutoff = (now_cst() - timedelta(days=freshness_days)
                        ).strftime("%Y-%m-%d")

        out: list[Candidate] = []
        disputed: list[dict] = []
        for mid, c in scores.items():
            row = rows_map.get(mid)
            if not row or row["lifecycle"] in ("archived", "missing"):
                continue
            c.title, c.summary, c.lifecycle = row["title"], row["summary"], row["lifecycle"]
            c.source_type = row["source_type"] or "memory"
            c.confidence = row["confidence"] or "medium"
            try:
                verification = row["verification_state"] or "unverified"
                freshness = row["freshness_state"] or "current"
            except (IndexError, KeyError):
                verification, freshness = "unverified", "current"
            c.verification_state, c.freshness_state = verification, freshness
            # 硬合规：disputed 不注入（等用户裁决）；但收集元信息供 F3 争议提醒
            if row["confidence"] == "disputed":
                disputed.append({"id": mid, "title": row["title"] or "",
                                 "summary": row["summary"] or ""})
                continue
            factor = 1.0
            if row["lifecycle"] == "stale":
                factor *= stale_factor
            # 软状态降权（不静默过滤）
            if verification == "inferred":
                factor *= inferred_factor
            if freshness in ("expired", "review_due"):
                factor *= expired_factor
            # H：不再按"问题类型"启发式给来源加权；LLM 精筛看得到 source_type
            # 标签会自己决定用/不用哪一类记忆
            # 重要 + 负反馈 + 新鲜度
            try:
                if row["is_important"]:
                    factor *= important_factor
                negative = int(row["retrieval_negative_count"] or 0)
                if negative:
                    factor *= max(0.35, 1.0 - min(0.65, negative * 0.15))
            except (IndexError, KeyError):
                pass
            try:
                if freshness_boost > 1.0 and row.get("created_at"):
                    if str(row["created_at"])[:10] >= fresh_cutoff:
                        factor *= freshness_boost
            except (IndexError, KeyError, TypeError):
                pass
            c.final_score = c.rrf_score * factor
            out.append(c)
        return out, disputed

    def _fts_search(self, query: str, top_k: int, floor: float) -> list[tuple[str, float]]:
        q = _fts_escape(query)
        if not q:
            return []
        try:
            rows = self.db.query_all(
                "SELECT memory_id, -bm25(memories_fts) AS score FROM memories_fts "
                "WHERE memories_fts MATCH ? ORDER BY score DESC LIMIT ?", (q, top_k))
        except Exception:  # noqa: BLE001
            logger.warning("FTS 查询失败，query=%s", query)
            return []
        if not rows:
            return []
        max_score = rows[0]["score"]
        threshold = max_score * floor if max_score > 0 else float("-inf")
        return [(r["memory_id"], r["score"]) for r in rows if r["score"] >= threshold]

    # ---- 完整链路 -----------------------------------------------------------
    EMBED_QUERY_MAX_CHARS = 2000

    async def retrieve(self, query: str,
                       session_id: str | None = None,
                       context_text: str | None = None) -> RetrievalResult:
        """检索入口 —— 无开关分支，一条直路。

        流程：embed 线索 → hybrid 预筛 → 图扩展（links + entities）→
        LLM 精筛 → 加载详情 + 按关系分组返回。
        """
        _start = time.perf_counter()
        result = RetrievalResult()
        diag_gate = "none"

        # 1) embed 线索：上下文 + 当轮，始终参与（embedding 自然过滤无关话题）
        q_part = query[:self.EMBED_QUERY_MAX_CHARS]
        budget = self.EMBED_QUERY_MAX_CHARS - len(q_part) - 1
        if context_text and budget > 0:
            embed_cue = context_text[-budget:] + "\n" + q_part
        else:
            embed_cue = q_part
        query_vec = None
        if self.embed_fn:
            try:
                query_vec = (await self.embed_fn([embed_cue]))[0]
            except Exception:  # noqa: BLE001
                result.degraded = "Embedding 不可用，检索降级 FTS5 单路"
                logger.info(result.degraded)

        # 2) 预筛
        pre = await self.hybrid_presearch(query, query_vec)
        candidates = pre.candidates
        # 未命中且明确回忆 → 调低阈值再跑一次
        if not candidates and has_recall_intent(query):
            pre = await self.hybrid_presearch(query, query_vec, fallback=True)
            candidates = pre.candidates
        # F3：把本轮候选池里被硬砍的争议记忆带回来，供上层提示
        if pre.disputed:
            result.disputed = list(pre.disputed)
        if not candidates:
            result.diagnostics = self._empty_diagnostics(
                query, pre, context_text, result.degraded, _start)
            return result

        # 3) 图扩展：从 candidates[:seed_pool_size] 出发做双向 links + 实体共现
        seed_pool = candidates[:int(self.config.get(
            "graph_expand_seed_pool", 10))]
        graph_neighbors = await self._expand_graph(
            [c.memory_id for c in seed_pool], query_vec)
        # 合并进候选池（图节点也送 LLM 精筛裁决）
        seen_ids = {c.memory_id for c in candidates}
        for neighbor in graph_neighbors:
            if neighbor.memory_id in seen_ids:
                continue
            candidates.append(neighbor)
            seen_ids.add(neighbor.memory_id)

        # 4) LLM 精筛（始终跑；异常/未配置走基于相对得分的兜底）
        chosen_ids = await self._refine(
            query, candidates, session_id, context_text, result)
        if not chosen_ids:
            diag_gate = "refine_empty"

        # 5) 加载详情（主命中 + 关联记忆分组）
        chosen_set = set(chosen_ids)
        for mid in chosen_ids:
            detail = await asyncio.to_thread(self._load_detail, mid)
            if detail:
                # 若该 id 是图扩展节点，把 relation/from_seed 塞进去
                cand = next((c for c in candidates if c.memory_id == mid), None)
                if cand and cand.relation:
                    detail["relation"] = cand.relation
                    detail["from_seed"] = cand.from_seed
                    result.related.append(detail)
                else:
                    result.hits.append(detail)
                result.loaded_ids.append(mid)

        # 若精筛只挑到 hits，把图扩展里 top-N 关联（未被选中的）也保留供上层参考
        # —— 这样即便 LLM 只给了 1 条主命中，也能顺带带回图上下文
        extra_related_cap = int(self.config.get(
            "graph_extra_related_cap", 3))
        if extra_related_cap > 0:
            neighbor_pool = [c for c in graph_neighbors
                             if c.memory_id not in chosen_set
                             and c.memory_id not in result.loaded_ids]
            neighbor_pool.sort(key=lambda x: -x.final_score)
            for cand in neighbor_pool[:extra_related_cap]:
                detail = await asyncio.to_thread(
                    self._load_detail, cand.memory_id)
                if detail:
                    detail["relation"] = cand.relation
                    detail["from_seed"] = cand.from_seed
                    result.related.append(detail)
                    result.loaded_ids.append(cand.memory_id)

        elapsed_ms = round((time.perf_counter() - _start) * 1000, 2)
        logger.info("检索 trace：candidates=%d hits=%d related=%d %.0fms degraded=%s",
                    len(candidates), len(result.hits), len(result.related),
                    elapsed_ms, result.degraded or "-")

        result.diagnostics = {
            "degraded": bool(result.degraded),
            "vector_hits": pre.vector_hits,
            "fts_hits": pre.fts_hits,
            "top_vector_score": round(pre.top_vector_score, 4),
            "gate": diag_gate,
            "context_chars": len(context_text or ""),
            "retrieval_time_ms": elapsed_ms,
            "refined_count": len(chosen_ids),
            "graph_neighbors": len(graph_neighbors),
            "candidate_ids": [c.memory_id for c in candidates[:20]],
            "selected_ids": list(chosen_ids),
            "rejected": [{"id": c.memory_id, "reason": "refine_rejected"}
                         for c in candidates[:20] if c.memory_id not in chosen_ids],
        }
        return result

    def _empty_diagnostics(self, query, pre, context_text, degraded,
                            start) -> dict:
        return {
            "degraded": bool(degraded),
            "vector_hits": pre.vector_hits,
            "fts_hits": pre.fts_hits,
            "top_vector_score": round(pre.top_vector_score, 4),
            "gate": "presearch_empty",
            "context_chars": len(context_text or ""),
            "graph_neighbors": 0,
            "candidate_ids": [], "selected_ids": [], "rejected": [],
            "retrieval_time_ms": round((time.perf_counter() - start) * 1000, 2),
            "refined_count": 0,
        }

    # ---- LLM 精筛（无 llm_available 开关；异常才降级） ---------------------
    async def _refine(self, query: str, candidates: list[Candidate],
                       session_id: str | None,
                       context_text: str | None,
                       result: RetrievalResult) -> list[str]:
        if not self.llm_refine_fn:
            return self._degrade_pick(candidates)
        try:
            timeout = self.config.get("retrieval_refine_timeout_seconds", 10)
            payload = [{"id": c.memory_id, "title": c.title,
                        "summary": c.summary, "source_type": c.source_type,
                        "confidence": c.confidence,
                        "verification_state": c.verification_state,
                        "freshness_state": c.freshness_state,
                        "relation": c.relation or "primary",
                        "from_seed": c.from_seed}
                       for c in candidates[:20]]
            chosen = await asyncio.wait_for(
                self.llm_refine_fn(query, payload,
                                   session_id=session_id,
                                   context_text=context_text),
                timeout=timeout)
            return list(chosen)[:int(self.config.get(
                "retrieval_refine_max", 5))]
        except asyncio.TimeoutError:
            result.degraded = "第 2 层精筛超时，按得分兜底"
            logger.warning("检索精筛超时")
            return self._degrade_pick(candidates)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            result.degraded = "第 2 层精筛不可用，按得分兜底"
            logger.info("检索精筛异常，按得分兜底", exc_info=True)
            return self._degrade_pick(candidates)

    def _degrade_pick(self, candidates: list[Candidate]) -> list[str]:
        """LLM 挂时按相对得分兜底 —— 不再问用户"意图"。

        取 top-K 中得分 ≥ top-1 × ratio 的候选（默认 0.5），K 由
        retrieval_refine_max 决定；空池返回空。
        """
        if not candidates:
            return []
        top = candidates[0].final_score or 0.0
        ratio = self.config.get("refine_degrade_score_ratio", 0.5)
        k = int(self.config.get("retrieval_refine_max", 5))
        picked = [c.memory_id for c in candidates[:k]
                  if top <= 0 or (c.final_score or 0.0) >= top * ratio]
        return picked or [candidates[0].memory_id]

    # ---- 图扩展：links + entities 双源，用 vs seed 相似度门槛 --------------
    async def _expand_graph(self, seed_ids: list[str],
                             query_vec) -> list[Candidate]:
        """从 seed 出发做双向 links（out+back）+ 实体共现，
        用 **与 seed 的余弦** 作为门槛（不是与 query）。"""
        if not seed_ids:
            return []
        neighbors: dict[str, Candidate] = {}
        # 1) 图边（out + back）
        for cand in self._expand_via_links(seed_ids):
            existing = neighbors.get(cand.memory_id)
            if existing is None or cand.final_score > existing.final_score:
                neighbors[cand.memory_id] = cand
        # 2) 实体共现
        for cand in self._expand_via_entities(seed_ids):
            existing = neighbors.get(cand.memory_id)
            if existing is None or cand.final_score > existing.final_score:
                neighbors[cand.memory_id] = cand
        # 3) 共引召回（协同过滤）：过去 N 天与 seed 一起被同一消息引用过的记忆
        for cand in self._expand_via_citations(seed_ids):
            existing = neighbors.get(cand.memory_id)
            if existing is None or cand.final_score > existing.final_score:
                neighbors[cand.memory_id] = cand
        # 4) 用 seed 相似度过滤（关联记忆的价值在与 seed 同主题）
        seed_threshold = float(self.config.get(
            "graph_expand_seed_threshold", 0.6))
        return self._filter_by_seed_similarity(
            list(neighbors.values()), seed_threshold)

    def _expand_via_links(self, seed_ids: list[str]) -> list[Candidate]:
        """双向 links 扩展：outlinks + backlinks，一次性批量取。"""
        if not seed_ids:
            return []
        ph = ",".join("?" * len(seed_ids))
        rows = self.db.query_all(
            f"SELECT source_id, target_id, link_type FROM memory_links "
            f"WHERE source_id IN ({ph}) OR target_id IN ({ph})",
            tuple(seed_ids) + tuple(seed_ids))
        seed_set = set(seed_ids)
        edges: list[tuple[str, str, str]] = []  # (seed, neighbor, relation)
        for r in rows:
            if r["source_id"] in seed_set and r["target_id"] not in seed_set:
                edges.append(
                    (r["source_id"], r["target_id"], r["link_type"] or "related"))
            elif r["target_id"] in seed_set and r["source_id"] not in seed_set:
                # backlinks：反向记录为"被 X 指向"，关系语义保持
                edges.append(
                    (r["target_id"], r["source_id"], r["link_type"] or "related"))
        if not edges:
            return []
        neighbor_ids = list({e[1] for e in edges})
        rows_map = self.palace.get_many(neighbor_ids)
        out: list[Candidate] = []
        for seed, nid, relation in edges:
            row = rows_map.get(nid)
            if not row or row["lifecycle"] not in ("active", "stable", "stale"):
                continue
            if row["confidence"] == "disputed":
                continue
            cand = Candidate(
                memory_id=nid, title=row["title"] or "",
                summary=row["summary"] or "", lifecycle=row["lifecycle"],
                source_type=row["source_type"] or "memory",
                confidence=row["confidence"] or "medium",
                verification_state=row.get("verification_state") or "unverified",
                freshness_state=row.get("freshness_state") or "current",
                from_seed=seed, relation=relation,
                # 图节点给一个基础分（约等于 candidates[10] 的量级）以便被
                # 精筛看得到；具体门槛由 seed 相似度决定
                rrf_score=1.0 / (60 + 10),
                final_score=1.0 / (60 + 10),
            )
            out.append(cand)
        return out

    def _expand_via_entities(self, seed_ids: list[str]) -> list[Candidate]:
        """实体共现：seed 提到的实体 → JOIN memory_entity_links → 拿其它记忆。

        只在实体不为空时启用；共实体门槛默认取 seed 实体数 ≥ 1 的记忆。
        """
        if not seed_ids:
            return []
        ph = ",".join("?" * len(seed_ids))
        # 1) 拿 seed 的实体
        rows = self.db.query_all(
            f"SELECT DISTINCT entity_id FROM memory_entity_links "
            f"WHERE memory_id IN ({ph})", tuple(seed_ids))
        entity_ids = [r["entity_id"] for r in rows]
        if not entity_ids:
            return []
        # 2) 拿这些实体关联的其它记忆（去掉 seed 自身）
        eph = ",".join("?" * len(entity_ids))
        cap = int(self.config.get("graph_entity_neighbor_cap", 30))
        rows = self.db.query_all(
            f"SELECT l.memory_id, COUNT(DISTINCT l.entity_id) AS shared "
            f"FROM memory_entity_links l "
            f"WHERE l.entity_id IN ({eph}) AND l.memory_id NOT IN ({ph}) "
            f"GROUP BY l.memory_id ORDER BY shared DESC LIMIT ?",
            tuple(entity_ids) + tuple(seed_ids) + (cap,))
        if not rows:
            return []
        neighbor_ids = [r["memory_id"] for r in rows]
        rows_map = self.palace.get_many(neighbor_ids)
        out: list[Candidate] = []
        for r in rows:
            nid = r["memory_id"]
            row = rows_map.get(nid)
            if not row or row["lifecycle"] not in ("active", "stable", "stale"):
                continue
            if row["confidence"] == "disputed":
                continue
            # 找一个"最能解释这个邻居"的 seed（简单取首个含有共实体的 seed）
            seed_for = self._pick_explanatory_seed(seed_ids, nid)
            cand = Candidate(
                memory_id=nid, title=row["title"] or "",
                summary=row["summary"] or "", lifecycle=row["lifecycle"],
                source_type=row["source_type"] or "memory",
                confidence=row["confidence"] or "medium",
                verification_state=row.get("verification_state") or "unverified",
                freshness_state=row.get("freshness_state") or "current",
                from_seed=seed_for, relation="entity_shared",
                # 共实体数越多分越高（作为进入精筛的起始 rrf 分量）
                rrf_score=1.0 / (60 + max(1, 15 - int(r["shared"] or 0))),
                final_score=1.0 / (60 + max(1, 15 - int(r["shared"] or 0))),
            )
            out.append(cand)
        return out

    def _expand_via_citations(self, seed_ids: list[str]) -> list[Candidate]:
        """共引召回（G4）：过去 N 天里，与 seed_ids 一起被同一 message 引用过的
        其它记忆 —— 天然的协同过滤信号。

        用法典型：用户问"再展开一下昨天那个方案"，就算 seed 命中的是"方案 X"，
        共引图也能把上次一起引用的"背景 Y / 决策 Z"一起带回。
        """
        if not seed_ids:
            return []
        window_days = int(self.config.get(
            "graph_citation_window_days", 30))
        cap = int(self.config.get("graph_citation_neighbor_cap", 15))
        cutoff = (now_cst() - timedelta(days=window_days)
                  ).isoformat(timespec="seconds")
        sph = ",".join("?" * len(seed_ids))
        try:
            rows = self.db.query_all(
                f"SELECT other.memory_id AS memory_id, "
                f"       COUNT(DISTINCT other.message_id) AS co_cnt "
                f"FROM citation_events seed "
                f"JOIN citation_events other "
                f"  ON seed.message_id = other.message_id "
                f" AND seed.session_id = other.session_id "
                f"WHERE seed.memory_id IN ({sph}) "
                f"  AND other.memory_id NOT IN ({sph}) "
                f"  AND seed.cited_at >= ? "
                f"GROUP BY other.memory_id "
                f"ORDER BY co_cnt DESC LIMIT ?",
                tuple(seed_ids) + tuple(seed_ids) + (cutoff, cap))
        except Exception:  # noqa: BLE001 - citation_events 未建/空表兜底
            return []
        if not rows:
            return []
        neighbor_ids = [r["memory_id"] for r in rows]
        rows_map = self.palace.get_many(neighbor_ids)
        out: list[Candidate] = []
        for r in rows:
            nid = r["memory_id"]
            row = rows_map.get(nid)
            if not row or row["lifecycle"] not in ("active", "stable", "stale"):
                continue
            if row["confidence"] == "disputed":
                continue
            seed_for = self._pick_citation_seed(seed_ids, nid, cutoff)
            co_cnt = int(r["co_cnt"] or 1)
            cand = Candidate(
                memory_id=nid, title=row["title"] or "",
                summary=row["summary"] or "", lifecycle=row["lifecycle"],
                source_type=row["source_type"] or "memory",
                confidence=row["confidence"] or "medium",
                verification_state=row.get("verification_state") or "unverified",
                freshness_state=row.get("freshness_state") or "current",
                from_seed=seed_for, relation="co_cited",
                # 共引次数越多起始分越高
                rrf_score=1.0 / (60 + max(1, 15 - co_cnt)),
                final_score=1.0 / (60 + max(1, 15 - co_cnt)),
            )
            out.append(cand)
        return out

    def _pick_citation_seed(self, seed_ids: list[str],
                             neighbor_id: str, cutoff: str) -> str | None:
        """挑一个"共引最多"的 seed 作为 from_seed 标注。"""
        try:
            sph = ",".join("?" * len(seed_ids))
            row = self.db.query_one(
                f"SELECT seed.memory_id AS seed_id, "
                f"       COUNT(DISTINCT seed.message_id) AS co_cnt "
                f"FROM citation_events seed "
                f"JOIN citation_events other "
                f"  ON seed.message_id = other.message_id "
                f" AND seed.session_id = other.session_id "
                f"WHERE seed.memory_id IN ({sph}) "
                f"  AND other.memory_id = ? "
                f"  AND seed.cited_at >= ? "
                f"GROUP BY seed.memory_id ORDER BY co_cnt DESC LIMIT 1",
                tuple(seed_ids) + (neighbor_id, cutoff))
            return row["seed_id"] if row else (seed_ids[0] if seed_ids else None)
        except Exception:  # noqa: BLE001
            return seed_ids[0] if seed_ids else None

    def _pick_explanatory_seed(self, seed_ids: list[str],
                                neighbor_id: str) -> str | None:
        """挑一个与 neighbor 共享实体最多的 seed，用于 UI/精筛 的"从 X 带出"标注。"""
        try:
            ph = ",".join("?" * len(seed_ids))
            row = self.db.query_one(
                f"SELECT s.memory_id AS seed_id, COUNT(*) AS shared "
                f"FROM memory_entity_links s "
                f"JOIN memory_entity_links n ON s.entity_id=n.entity_id "
                f"WHERE s.memory_id IN ({ph}) AND n.memory_id=? "
                f"GROUP BY s.memory_id ORDER BY shared DESC LIMIT 1",
                tuple(seed_ids) + (neighbor_id,))
            return row["seed_id"] if row else (seed_ids[0] if seed_ids else None)
        except Exception:  # noqa: BLE001
            return seed_ids[0] if seed_ids else None

    def _filter_by_seed_similarity(self, neighbors: list[Candidate],
                                    threshold: float) -> list[Candidate]:
        """关联记忆的相关性门槛：与其 from_seed 的向量余弦 ≥ threshold。

        E2 优化：改用矩阵乘。一次性拉出所有 seed 与所有 neighbor 的向量，
        计算 (N, S) 相似度矩阵，对每个 neighbor 取其 from_seed 那一列。
        规模 N×S 都不大时也不比原来慢（numpy 单核 vec@vec 已经足够快），
        但省掉 N 次 Python→C 上下文切换与 N 次锁开销。

        vs 不可用/维度未知时保守不过滤（交 LLM 精筛裁决）。
        """
        if not neighbors or not self.vs.loaded or self.vs.dim is None:
            return neighbors
        try:
            import numpy as np
        except ImportError:  # 极端环境兜底
            return neighbors
        # 1) 收集需要计算的 (seed_id, neighbor_id) 对
        idx_map = getattr(self.vs, "_id_to_index", {}) or {}
        matrix = getattr(self.vs, "_matrix", None)
        if matrix is None or not idx_map:
            return neighbors
        # 只保留 idx_map 中存在的 seed（保证 seed_pos 与 seed_rows 顺序一致）
        seed_ids = sorted({c.from_seed for c in neighbors
                            if c.from_seed and c.from_seed in idx_map})
        seed_pos = {sid: i for i, sid in enumerate(seed_ids)}
        # 缺失索引的邻居保守放行（无法比对）
        computable = [c for c in neighbors
                      if c.from_seed in seed_pos
                      and c.memory_id in idx_map]
        uncomputable = [c for c in neighbors if c not in computable]
        if not computable or not seed_ids:
            return neighbors
        # 2) 抽取矩阵行（seed_ids 与 seed_pos 一一对应）
        seed_rows = np.vstack([matrix[idx_map[s]] for s in seed_ids])
        neigh_rows = np.vstack([matrix[idx_map[c.memory_id]] for c in computable])
        # 3) 归一化 + 矩阵乘
        seed_norms = np.linalg.norm(seed_rows, axis=1, keepdims=True) + 1e-8
        neigh_norms = np.linalg.norm(neigh_rows, axis=1, keepdims=True) + 1e-8
        seed_unit = seed_rows / seed_norms
        neigh_unit = neigh_rows / neigh_norms
        sim_matrix = neigh_unit @ seed_unit.T  # (N, S)
        # 4) 取每个邻居对应 seed 的相似度
        kept: list[Candidate] = list(uncomputable)  # 无法计算的一律放行
        for i, cand in enumerate(computable):
            sid = cand.from_seed
            j = seed_pos.get(sid)
            if j is None or j >= sim_matrix.shape[1]:
                kept.append(cand)
                continue
            sim = float(sim_matrix[i, j])
            if sim >= threshold:
                kept.append(cand)
        return kept

    # ---- 详情加载 -----------------------------------------------------------
    def _load_detail(self, mid: str) -> dict | None:
        row = self.palace.get(mid)
        if not row:
            return None
        f = self.data_dir / row["md_path"]
        if not f.exists():
            logger.warning("md 文件缺失，summary 兜底：%s", mid)
            return {"id": mid, "title": row["title"], "summary": row["summary"],
                    "detail": row["summary"], "confidence": row["confidence"],
                    "lifecycle": row["lifecycle"],
                    "source_type": row.get("source_type", "memory"),
                    "verification_state": row.get("verification_state", "unverified"),
                    "freshness_state": row.get("freshness_state", "current"),
                    "degraded": True}
        doc = parse_memory_md(f.read_text(encoding="utf-8"))
        return {"id": mid, "title": doc.title, "summary": doc.summary,
                "detail": doc.detail, "confidence": row["confidence"],
                "lifecycle": row["lifecycle"],
                "source_type": row.get("source_type", "memory"),
                "domain": row.get("domain", "general"), "links": doc.links,
                "verification_state": row.get("verification_state", "unverified"),
                "freshness_state": row.get("freshness_state", "current")}


def _fts_escape(query: str) -> str:
    """将用户查询转为 FTS5 安全的 MATCH 表达式（分词后 AND 连接，加双引号防语法）。"""
    tokens = re.findall(r"[\w一-鿿]+", query)
    if not tokens:
        return ""
    return " AND ".join(f'"{t}"' for t in tokens[:8])
