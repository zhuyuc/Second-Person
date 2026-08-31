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
import hashlib
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from infrastructure.fts import fts_escape as _fts_escape
from infrastructure.timeutil import now_cst

from .md_file import parse_memory_md
from .retriever_gates import (
    ACK_ONLY_PATTERNS,
    HISTORY_REF_PATTERNS,
    RECALL_INTENT_PATTERNS,
    has_history_reference,
    has_recall_intent,
    is_ack_only,
    short_circuit_gate,
)
from .retriever_progress import (
    build_progress_payload,
    done_summary,
    presearch_summary,
    refine_start_summary,
    skip_summary,
)

logger = logging.getLogger("second_person.retriever")

__all__ = [
    "ACK_ONLY_PATTERNS",
    "Candidate",
    "HISTORY_REF_PATTERNS",
    "RECALL_INTENT_PATTERNS",
    "RetrievalResult",
    "Retriever",
    "has_history_reference",
    "has_recall_intent",
    "is_ack_only",
    "short_circuit_gate",
]


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
        # v7 精筛 LRU cache：key=(session_id, query, candidate_ids_hash) → (ids, ts)
        # 覆盖重生成/异常重试场景，一 turn 内重复调用直接命中省 2-3s LLM 精筛。
        self._refine_cache: OrderedDict[tuple, tuple[list[str], float]] = OrderedDict()

    @staticmethod
    async def _notify_progress(
            on_progress,
            payload: dict,
    ) -> None:
        if on_progress is not None:
            await on_progress(payload)

    def _try_fast_path_refine(self, candidates: list[Candidate]) -> list[str] | None:
        """High-confidence paths that skip LLM refine."""
        from . import _constants as _mem_const
        if not candidates:
            return None
        if len(candidates) == 1:
            score = float(candidates[0].vector_score or 0.0)
            if score >= _mem_const.REFINE_FAST_PATH_MIN_SCORE:
                return [candidates[0].memory_id]
            return None
        top, second = candidates[0], candidates[1]
        top_score = float(top.vector_score or 0.0)
        second_score = float(second.vector_score or 0.0)
        if (top_score >= _mem_const.REFINE_FAST_PATH_GAP_MIN_SCORE
                and second_score > 0
                and top_score >= second_score * _mem_const.REFINE_FAST_PATH_GAP_RATIO):
            return [top.memory_id]
        return None

    def _refine_cache_key(self, session_id: str | None, query: str,
                          candidate_ids: list[str]) -> tuple:
        """Stable key across attempts: candidate order can shift so we sort ids."""
        ids_hash = hashlib.sha1(
            ",".join(sorted(candidate_ids)).encode("utf-8")).hexdigest()[:16]
        return (session_id or "", query, ids_hash)

    def _refine_cache_get(self, key: tuple) -> list[str] | None:
        from . import _constants as _mem_const
        ttl = int(self.config.get(
            "retriever_refine_cache_ttl_seconds",
            _mem_const.RETRIEVER_REFINE_CACHE_TTL_SECONDS))
        entry = self._refine_cache.get(key)
        if entry is None:
            return None
        ids, ts = entry
        if time.monotonic() - ts > ttl:
            # 过期：drop 后视同 miss
            self._refine_cache.pop(key, None)
            return None
        # LRU 移到末尾
        self._refine_cache.move_to_end(key)
        return list(ids)

    def _refine_cache_put(self, key: tuple, ids: list[str]) -> None:
        from . import _constants as _mem_const
        size = int(self.config.get(
            "retriever_refine_cache_size",
            _mem_const.RETRIEVER_REFINE_CACHE_SIZE))
        self._refine_cache[key] = (list(ids), time.monotonic())
        self._refine_cache.move_to_end(key)
        while len(self._refine_cache) > max(1, size):
            self._refine_cache.popitem(last=False)

    def _archived_project_ids(self) -> set[str]:
        """归档项目的 id 集合，用于 M2 硬过滤。"""
        try:
            return {r["id"] for r in self.db.query_all(
                "SELECT id FROM projects WHERE status='archived'")}
        except Exception:  # noqa: BLE001 - 老库无此表时降级
            return set()

    # ---- 第 1 层 Hybrid 预筛 -------------------------------------------------
    async def hybrid_presearch(self, query: str, query_vec=None,
                               fallback: bool = False,
                               project_id: str | None = None,
                               fts_hits: list[tuple[str, float]] | None = None
                               ) -> _PresearchResult:
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

        if fts_hits is None:
            fts_hits = self._fts_search(query, top_k, bm25_floor)
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
        out, disputed = self._score_candidates(
            query, scores, rows_map,
            project_id=project_id,
            archived_project_ids=self._archived_project_ids())
        out.sort(key=lambda x: -x.final_score)
        return _PresearchResult(candidates=out, disputed=disputed,
                                vector_hits=n_vec, fts_hits=n_fts,
                                top_vector_score=top_score)

    def _score_candidates(self, query: str,
                          scores: dict[str, Candidate],
                          rows_map: dict,
                          *,
                          project_id: str | None = None,
                          archived_project_ids: set | None = None,
                          ) -> tuple[list[Candidate], list[dict]]:
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
        archived_project_ids = archived_project_ids or set()
        for mid, c in scores.items():
            row = rows_map.get(mid)
            if not row or row["lifecycle"] in ("archived", "missing"):
                continue
            # M2 硬过滤：项目会话只见本项目 + 全局；无项目会话只见全局；
            # 归档项目的记忆一律不可见（冷藏）
            try:
                row_proj = row["project_id"]
            except (IndexError, KeyError):
                row_proj = None
            if row_proj and row_proj in archived_project_ids:
                continue
            if project_id is None:
                if row_proj is not None:
                    continue
            else:
                if row_proj is not None and row_proj != project_id:
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
                       context_text: str | None = None,
                       project_id: str | None = None,
                       on_progress=None) -> RetrievalResult:
        """检索入口 —— 无开关分支，一条直路。

        流程：embed 线索 → hybrid 预筛 → 图扩展（links + entities）→
        LLM 精筛 → 加载详情 + 按关系分组返回。
        on_progress：可选 async 回调，推送 memory_progress 真实进度 payload。
        """
        _start = time.perf_counter()
        result = RetrievalResult()
        diag_gate = "none"
        refine_path = "full"

        from . import _constants as _mem_const
        min_chars = int(self.config.get(
            "min_query_chars_for_context", _mem_const.MIN_QUERY_CHARS_FOR_CONTEXT))
        gate = short_circuit_gate(query, context_text, min_chars)
        if gate is not None:
            result.diagnostics = self._empty_diagnostics(
                query, _PresearchResult(), context_text, "", _start)
            result.diagnostics["gate"] = gate
            elapsed = round((time.perf_counter() - _start) * 1000)
            await self._notify_progress(on_progress, build_progress_payload(
                stage="skipped", status="skipped", summary=skip_summary(gate, query),
                gate=gate, hit_count=0, candidates=0, elapsed_ms=elapsed,
            ))
            return result

        await self._notify_progress(on_progress, build_progress_payload(
            stage="embed", status="running",
            summary="正在生成检索向量并扫描记忆库",
        ))

        # 1) embed 线索 + FTS 并行
        q_part = query[:self.EMBED_QUERY_MAX_CHARS]
        budget = self.EMBED_QUERY_MAX_CHARS - len(q_part) - 1
        if context_text and budget > 0:
            embed_cue = context_text[-budget:] + "\n" + q_part
        else:
            embed_cue = q_part
        query_vec = None
        top_k = self.config.get("retrieval_top_k", 10)
        bm25_floor = _mem_const.BM25_RELATIVE_FLOOR
        fts_task = asyncio.create_task(asyncio.to_thread(
            self._fts_search, query, top_k, bm25_floor))
        if self.embed_fn:
            try:
                query_vec = (await self.embed_fn([embed_cue]))[0]
            except Exception:  # noqa: BLE001
                result.degraded = "Embedding 不可用，检索降级 FTS5 单路"
                logger.info(result.degraded)
        fts_hits = await fts_task

        await self._notify_progress(on_progress, build_progress_payload(
            stage="presearch", status="running", summary="Hybrid 预筛进行中",
        ))

        pre = await self.hybrid_presearch(
            query, query_vec, project_id=project_id, fts_hits=fts_hits)
        candidates = pre.candidates
        if not candidates and has_recall_intent(query):
            pre = await self.hybrid_presearch(
                query, query_vec, fallback=True, project_id=project_id,
                fts_hits=fts_hits)
            candidates = pre.candidates
        if pre.disputed:
            result.disputed = list(pre.disputed)

        await self._notify_progress(on_progress, build_progress_payload(
            stage="presearch", status="ok" if candidates else "skipped",
            summary=presearch_summary(len(candidates), pre.vector_hits, pre.fts_hits),
            candidates=len(candidates),
            vector_hits=pre.vector_hits,
            fts_hits=pre.fts_hits,
            elapsed_ms=round((time.perf_counter() - _start) * 1000),
        ))

        if not candidates:
            result.diagnostics = self._empty_diagnostics(
                query, pre, context_text, result.degraded, _start)
            await self._notify_progress(on_progress, build_progress_payload(
                stage="done", status="ok", summary=done_summary(0),
                candidates=0, hit_count=0, gate="presearch_empty",
                elapsed_ms=round((time.perf_counter() - _start) * 1000),
            ))
            return result

        await self._notify_progress(on_progress, build_progress_payload(
            stage="graph", status="running",
            summary=f"从 {min(len(candidates), int(self.config.get('graph_expand_seed_pool', _mem_const.GRAPH_EXPAND_SEED_POOL)))} 条 seed 做图扩展",
        ))

        seed_pool = candidates[:int(self.config.get(
            "graph_expand_seed_pool", _mem_const.GRAPH_EXPAND_SEED_POOL))]
        graph_neighbors = await self._expand_graph(
            [c.memory_id for c in seed_pool], query_vec)
        seen_ids = {c.memory_id for c in candidates}
        for neighbor in graph_neighbors:
            if neighbor.memory_id in seen_ids:
                continue
            candidates.append(neighbor)
            seen_ids.add(neighbor.memory_id)

        pool_cap = int(self.config.get(
            "candidate_pool_hard_cap", _mem_const.CANDIDATE_POOL_HARD_CAP))
        picked = candidates[:pool_cap]
        refine_path = "full"
        fast_ids = self._try_fast_path_refine(picked)
        if fast_ids is not None:
            refine_path = "fast_path"
            chosen_ids = fast_ids
            await self._notify_progress(on_progress, build_progress_payload(
                stage="refine", status="ok",
                summary=refine_start_summary(len(picked), refine_path),
                candidates=len(picked), refine_path=refine_path,
            ))
        else:
            await self._notify_progress(on_progress, build_progress_payload(
                stage="refine", status="running",
                summary=refine_start_summary(len(picked)),
                candidates=len(picked),
            ))
            chosen_ids, refine_path = await self._refine(
                query, candidates, session_id, context_text, result,
                on_progress=on_progress)
        if not chosen_ids:
            diag_gate = "refine_empty"

        chosen_set = set(chosen_ids)
        detail_rows = await asyncio.gather(*[
            asyncio.to_thread(self._load_detail, mid) for mid in chosen_ids
        ])
        # 建 memory_id → candidate 字典，避免每个 chosen_id 都做 O(n) 线性扫描
        candidate_by_id = {c.memory_id: c for c in candidates}
        for mid, detail in zip(chosen_ids, detail_rows, strict=True):
            if detail:
                cand = candidate_by_id.get(mid)
                if cand and cand.relation:
                    detail["relation"] = cand.relation
                    detail["from_seed"] = cand.from_seed
                    result.related.append(detail)
                else:
                    result.hits.append(detail)
                result.loaded_ids.append(mid)

        extra_related_cap = int(self.config.get(
            "graph_extra_related_cap", _mem_const.GRAPH_EXTRA_RELATED_CAP))
        if chosen_ids and extra_related_cap > 0:
            neighbor_pool = [c for c in graph_neighbors
                             if c.memory_id not in chosen_set
                             and c.memory_id not in result.loaded_ids]
            neighbor_pool.sort(key=lambda x: -x.final_score)
            extra_ids = [c.memory_id for c in neighbor_pool[:extra_related_cap]]
            extra_details = await asyncio.gather(*[
                asyncio.to_thread(self._load_detail, mid) for mid in extra_ids
            ])
            for cand, detail in zip(neighbor_pool[:extra_related_cap], extra_details,
                                    strict=True):
                if detail:
                    detail["relation"] = cand.relation
                    detail["from_seed"] = cand.from_seed
                    result.related.append(detail)
                    result.loaded_ids.append(cand.memory_id)

        if not chosen_ids and candidates:
            self._record_negative_feedback(candidates)

        elapsed_ms = round((time.perf_counter() - _start) * 1000, 2)
        logger.info("检索 trace：candidates=%d hits=%d related=%d %.0fms degraded=%s",
                    len(candidates), len(result.hits), len(result.related),
                    elapsed_ms, result.degraded or "-")

        hit_count = len(result.hits)
        # 面板展开时按「注入顺序」列出：主记忆在前、关联记忆在后
        injected_hits = list(result.hits) + list(result.related)
        await self._notify_progress(on_progress, build_progress_payload(
            stage="done", status="ok",
            summary=done_summary(hit_count, len(result.related)),
            candidates=len(candidates), hit_count=hit_count + len(result.related),
            gate=diag_gate if diag_gate != "none" else None,
            refine_path=refine_path,
            elapsed_ms=int(elapsed_ms),
            hits=injected_hits or None,
        ))

        result.diagnostics = {
            "degraded": bool(result.degraded),
            "vector_hits": pre.vector_hits,
            "fts_hits": pre.fts_hits,
            "top_vector_score": round(pre.top_vector_score, 4),
            "gate": diag_gate,
            "refine_path": refine_path,
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
                       result: RetrievalResult,
                       on_progress=None) -> tuple[list[str], str]:
        from . import _constants as _mem_const
        if not self.llm_refine_fn:
            return self._degrade_pick(candidates), "degrade_pick"
        min_cands = int(self.config.get(
            "retrieval_refine_min_candidates",
            _mem_const.RETRIEVAL_REFINE_MIN_CANDIDATES))
        if len(candidates) < max(1, min_cands):
            path = "degrade_pick"
            await self._notify_progress(on_progress, build_progress_payload(
                stage="refine", status="ok",
                summary=refine_start_summary(len(candidates), path),
                candidates=len(candidates), refine_path=path,
            ))
            return self._degrade_pick(candidates), path
        try:
            timeout = self.config.get(
                "retrieval_refine_timeout_seconds",
                _mem_const.RETRIEVAL_REFINE_TIMEOUT_SECONDS)
            pool_cap = int(self.config.get(
                "candidate_pool_hard_cap", _mem_const.CANDIDATE_POOL_HARD_CAP))
            picked = candidates[:pool_cap]
            cache_key = self._refine_cache_key(
                session_id, query, [c.memory_id for c in picked])
            cached = self._refine_cache_get(cache_key)
            if cached is not None:
                result.degraded = "第 2 层精筛 cache 命中"
                logger.debug("refine cache hit sid=%s query=%r", session_id, query[:40])
                path = "refine_cache"
                await self._notify_progress(on_progress, build_progress_payload(
                    stage="refine", status="ok",
                    summary=refine_start_summary(len(picked), path),
                    candidates=len(picked), refine_path=path,
                ))
                return cached[:int(self.config.get(
                    "retrieval_refine_max", _mem_const.RETRIEVAL_REFINE_MAX))], path
            payload = [{"id": c.memory_id, "title": c.title,
                        "summary": c.summary, "source_type": c.source_type,
                        "confidence": c.confidence,
                        "verification_state": c.verification_state,
                        "freshness_state": c.freshness_state,
                        "relation": c.relation or "primary",
                        "from_seed": c.from_seed}
                       for c in picked]
            chosen = await asyncio.wait_for(
                self.llm_refine_fn(query, payload,
                                   session_id=session_id,
                                   context_text=context_text),
                timeout=timeout)
            chosen_list = list(chosen)
            self._refine_cache_put(cache_key, chosen_list)
            await self._notify_progress(on_progress, build_progress_payload(
                stage="refine", status="ok",
                summary=refine_start_summary(len(picked), "full"),
                candidates=len(picked), refine_path="full",
            ))
            return chosen_list[:int(self.config.get(
                "retrieval_refine_max", _mem_const.RETRIEVAL_REFINE_MAX))], "full"
        except asyncio.TimeoutError:
            result.degraded = "第 2 层精筛超时，按得分兜底"
            logger.warning("检索精筛超时")
            path = "degrade_pick"
            await self._notify_progress(on_progress, build_progress_payload(
                stage="refine", status="ok",
                summary=refine_start_summary(len(candidates), path),
                candidates=len(candidates), refine_path=path,
            ))
            return self._degrade_pick(candidates), path
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            result.degraded = "第 2 层精筛不可用，按得分兜底"
            logger.info("检索精筛异常，按得分兜底", exc_info=True)
            path = "degrade_pick"
            await self._notify_progress(on_progress, build_progress_payload(
                stage="refine", status="ok",
                summary=refine_start_summary(len(candidates), path),
                candidates=len(candidates), refine_path=path,
            ))
            return self._degrade_pick(candidates), path

    def _degrade_pick(self, candidates: list[Candidate]) -> list[str]:
        """LLM 挂时按相对得分兜底 —— 不再问用户"意图"。

        取 top-K 中得分 ≥ top-1 × ratio 的候选（默认 0.5），K 由
        retrieval_refine_max 决定；空池返回空。
        """
        from . import _constants as _mem_const
        if not candidates:
            return []
        top = candidates[0].final_score or 0.0
        ratio = self.config.get(
            "refine_degrade_score_ratio", _mem_const.REFINE_DEGRADE_SCORE_RATIO)
        k = int(self.config.get(
            "retrieval_refine_max", _mem_const.RETRIEVAL_REFINE_MAX))
        picked = [c.memory_id for c in candidates[:k]
                  if top <= 0 or (c.final_score or 0.0) >= top * ratio]
        return picked or [candidates[0].memory_id]

    def _record_negative_feedback(self, candidates: list[Candidate]) -> None:
        """E7：LLM 精筛判空时，把候选池 top-K 的 retrieval_negative_count +1。

        DB 出错不影响主链路（检索链路一等公民：数据观测失败不阻塞回答）。
        """
        from . import _constants as _mem_const
        top_k = _mem_const.REFINE_NEGATIVE_FEEDBACK_TOP_K
        ids = [c.memory_id for c in candidates[:top_k]]
        if not ids:
            return
        try:
            ph = ",".join("?" * len(ids))
            self.db.execute(
                f"UPDATE memories SET retrieval_negative_count = "
                f"COALESCE(retrieval_negative_count, 0) + 1 "
                f"WHERE id IN ({ph})", tuple(ids))
        except Exception:  # noqa: BLE001
            logger.debug("负样本反馈写入失败（忽略）", exc_info=True)

    # ---- 图扩展：links + entities 双源，用 vs seed 相似度门槛 --------------
    async def _expand_graph(self, seed_ids: list[str],
                             query_vec) -> list[Candidate]:
        """从 seed 出发做双向 links（out+back）+ 实体共现，
        用 **与 seed 的余弦** 作为门槛（不是与 query）。"""
        from . import _constants as _mem_const
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
            "graph_expand_seed_threshold", _mem_const.GRAPH_EXPAND_SEED_THRESHOLD))
        filtered = self._filter_by_seed_similarity(
            list(neighbors.values()), seed_threshold)
        # 5) Δ8：三源合并后总量硬帽，按 final_score 保留 top-N，避免大账户失控
        hard_cap = int(self.config.get(
            "graph_neighbor_hard_cap", _mem_const.GRAPH_NEIGHBOR_HARD_CAP))
        if hard_cap > 0 and len(filtered) > hard_cap:
            filtered.sort(key=lambda c: -c.final_score)
            filtered = filtered[:hard_cap]
        return filtered

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
        from . import _constants as _mem_const
        eph = ",".join("?" * len(entity_ids))
        cap = int(self.config.get(
            "graph_entity_neighbor_cap", _mem_const.GRAPH_ENTITY_NEIGHBOR_CAP))
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
        from . import _constants as _mem_const
        if not seed_ids:
            return []
        window_days = int(self.config.get(
            "graph_citation_window_days", _mem_const.GRAPH_CITATION_WINDOW_DAYS))
        cap = int(self.config.get(
            "graph_citation_neighbor_cap", _mem_const.GRAPH_CITATION_NEIGHBOR_CAP))
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
        Δ7：缺向量的 neighbor（老库/迁移期）不再无脑放行，按 rrf_score
             保留 top-N（graph_uncomputable_keep 条），避免语义门槛失效。
        """
        from . import _constants as _mem_const
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
        computable = [c for c in neighbors
                      if c.from_seed in seed_pos
                      and c.memory_id in idx_map]
        # Δ7：无向量邻居不再全放行，按 rrf_score 降序保留 top-N
        uncomputable_all = [c for c in neighbors if c not in computable]
        uncomputable_keep = int(self.config.get(
            "graph_uncomputable_keep", _mem_const.GRAPH_UNCOMPUTABLE_KEEP))
        uncomputable_all.sort(key=lambda c: -c.final_score)
        uncomputable = uncomputable_all[:max(0, uncomputable_keep)]
        if not computable or not seed_ids:
            return uncomputable
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
        kept: list[Candidate] = list(uncomputable)
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
