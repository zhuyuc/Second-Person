"""
Retriever —— 三级联动检索（产品文档 §记忆检索 / 开发文档 §6.8）。

第 1 层 Hybrid 预筛（~5ms，零 LLM）：
  向量路：余弦 ≥ vector_threshold 过滤取 top-K
  FTS5 路：BM25 得分 ≥ 本次最高分 × bm25_relative_floor 过滤取 top-K
  RRF 融合：rank 从 1 起，score = Σ 1/(rrf_k + rank)，仅排序不设阈值
  两路均空 → 跳过整条链路
  stale：RRF 融合后最终得分 × stale_score_factor；archived/missing 不参与
第 2 层 目录定位（agent_model 轻量 LLM）：候选 summary 结合最近对话精筛至
  0-3 条——无一相关时判空（宁缺毋滥，对应人类"熟悉感过不了核验即放下"）
第 3 层 详情加载：读 md 全文 + 沿交叉引用追踪 1 跳（最多 2 条，标注"关联记忆"，
  且须与本轮线索余弦 ≥ vector_threshold——扩散衰减）
检索线索：向量路 = 最近对话上下文 + 当轮提问（编码特异性）；FTS 路仅当轮提问
  （分词 OR 掺上下文会关键词爆炸）
兜底重试：问句含明确检索意图 → 向量阈值降 recall_fallback、BM25 下限降 0.15 重跑
降级链：Embedding 挂 → FTS5 单路；第 2 层 LLM 挂 → 注入 top-3 未精筛；
        第 3 层 md 损坏 → summary 兜底；LLM 全挂 → 不检索
"""
from __future__ import annotations

import logging
import re
import time
import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from .md_file import parse_memory_md

logger = logging.getLogger("second_person.retriever")

RECALL_INTENT_PATTERNS = [
    r"你还记得", r"我之前(说过|提过|讲过)", r"我上次", r"还记不记得", r"之前(聊|谈)过",
]

# 问题类型启发式分类（零 LLM，与第 1 层同级）：用于 memory/knowledge 权重调整，
# 避免知识噪音淹没个人记忆（产品文档 §记忆存储）
PERSONAL_QUERY_PATTERNS = [
    r"我的", r"我(喜欢|偏好|习惯|讨厌|常用)", r"你记得我", r"我(是|在|做)什么",
    r"我(说过|提过|告诉过)", r"帮我回忆", r"关于我",
]
KNOWLEDGE_QUERY_PATTERNS = [
    r"什么是", r"是什么", r"如何(实现|配置|使用|部署)", r"怎么(实现|配置|用|部署)",
    r"(文档|资料|知识库)(里|中)", r"原理", r"定义", r"区别是", r"介绍一下",
]


def classify_query(query: str) -> str:
    """问题类型：personal / knowledge / neutral（个人信号优先）。"""
    if any(re.search(p, query) for p in PERSONAL_QUERY_PATTERNS):
        return "personal"
    if any(re.search(p, query) for p in KNOWLEDGE_QUERY_PATTERNS):
        return "knowledge"
    return "neutral"


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


@dataclass
class RetrievalResult:
    hits: list[dict] = field(default_factory=list)          # 主命中（含 detail）
    related: list[dict] = field(default_factory=list)       # 1 跳关联记忆
    loaded_ids: list[str] = field(default_factory=list)
    degraded: str = ""                                       # 降级说明
    diagnostics: dict = field(default_factory=dict)          # 检索诊断数据


@dataclass
class _PresearchResult:
    """hybrid_presearch 内部返回结构，携带各路命中数供诊断使用。"""
    candidates: list[Candidate] = field(default_factory=list)
    vector_hits: int = 0
    fts_hits: int = 0
    top_vector_score: float = 0.0    # 向量路 top-1 余弦分（校准闭环观测用）


class Retriever:
    def __init__(self, db, vector_store, palace, config, data_dir,
                 embed_fn=None, llm_refine_fn=None):
        self.db = db
        self.vs = vector_store
        self.palace = palace
        self.config = config
        self.data_dir = Path(data_dir)
        self.embed_fn = embed_fn          # async (list[str]) -> list[vec]
        # async (query, candidates, session_id) -> list[memory_id]
        self.llm_refine_fn = llm_refine_fn

    # ---- 第 1 层 Hybrid 预筛 ---------------------------------------------
    async def hybrid_presearch(self, query: str, query_vec=None,
                               fallback: bool = False) -> _PresearchResult:
        cfg = self.config
        vthr = cfg.get("recall_fallback_threshold", 0.35) if fallback \
            else cfg.get("vector_threshold", 0.55)
        bm25_floor = 0.15 if fallback else cfg.get("bm25_relative_floor", 0.3)
        top_k = cfg.get("retrieval_top_k", 10)
        rrf_k = cfg.get("rrf_k", 60)

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
            # 未命中：跳过整条链路
            return _PresearchResult(candidates=[], vector_hits=n_vec, fts_hits=n_fts,
                                    top_vector_score=top_score)

        # RRF 融合：rank 从 1 起
        scores: dict[str, Candidate] = {}
        for rank, (mid, _) in enumerate(vector_hits, start=1):
            c = scores.setdefault(mid, Candidate(mid, "", "", "active"))
            c.vector_rank = rank
            c.rrf_score += 1.0 / (rrf_k + rank)
        for rank, (mid, _) in enumerate(fts_hits, start=1):
            c = scores.setdefault(mid, Candidate(mid, "", "", "active"))
            c.bm25_rank = rank
            c.rrf_score += 1.0 / (rrf_k + rank)

        # 单路补偿：迁移未完成期间 embedding_version=new 的记忆只有 FTS 一路得分，
        # 按 1.5 倍系数补偿其 FTS 排名分（迁移完成后自动失效）
        if getattr(self.vs, "migrating", False):
            new_ids = {r["memory_id"] for r in self.db.query_all(
                "SELECT memory_id FROM vectors WHERE embedding_version='new'")}
            for mid, c in scores.items():
                if c.vector_rank is None and c.bm25_rank is not None and mid in new_ids:
                    c.rrf_score *= 1.5

        # 补齐元数据 + stale 降权 + 按问题类型调整 memory/knowledge 权重
        # （均在 RRF 后作用于排序得分，不做准入判据）
        stale_factor = cfg.get("stale_score_factor", 0.7)
        qtype = classify_query(query)
        pk_factor = cfg.get("personal_query_knowledge_factor", 0.7)
        km_factor = cfg.get("knowledge_query_memory_factor", 0.85)
        rows_map = self.palace.get_many(list(scores.keys()))
        out: list[Candidate] = []
        for mid, c in scores.items():
            row = rows_map.get(mid)
            if not row or row["lifecycle"] in ("archived", "missing"):
                continue
            c.title, c.summary, c.lifecycle = row["title"], row["summary"], row["lifecycle"]
            factor = stale_factor if row["lifecycle"] == "stale" else 1.0
            stype = row["source_type"] or "memory"
            if qtype == "personal" and stype == "knowledge":
                factor *= pk_factor      # 个人问题：知识库条目降权
            elif qtype == "knowledge" and stype == "memory":
                factor *= km_factor      # 知识问题：个人记忆轻度降权
            c.final_score = c.rrf_score * factor
            out.append(c)
        out.sort(key=lambda x: -x.final_score)
        return _PresearchResult(candidates=out, vector_hits=n_vec, fts_hits=n_fts,
                                top_vector_score=top_score)

    def _fts_search(self, query: str, top_k: int, floor: float) -> list[tuple[str, float]]:
        q = _fts_escape(query)
        if not q:
            return []
        try:
            rows = self.db.query_all(
                "SELECT memory_id, -bm25(memories_fts) AS score FROM memories_fts "
                "WHERE memories_fts MATCH ? ORDER BY score DESC LIMIT ?", (q, top_k))
        except Exception:  # noqa: BLE001 - FTS 查询语法异常兜底
            logger.warning("FTS 查询失败，query=%s", query)
            return []
        if not rows:
            return []
        max_score = rows[0]["score"]
        threshold = max_score * floor if max_score > 0 else float("-inf")
        return [(r["memory_id"], r["score"]) for r in rows if r["score"] >= threshold]

    # ---- 完整三级联动 -----------------------------------------------------
    # embed 输入上限：本地 CPU 版 BGE-M3 处理超长文本会进入分钟级计算
    # （实测 38K 字符约 88s，超过 HTTP 120s 超时后重试雪崩）；且模型只看
    # 前 8192 token，超出部分纯浪费。调用方已剥离附件正文，此处为兜底。
    EMBED_QUERY_MAX_CHARS = 2000

    async def retrieve(self, query: str, llm_available: bool = True,
                       session_id: str | None = None,
                       context_text: str | None = None) -> RetrievalResult:
        _start = time.perf_counter()
        result = RetrievalResult()
        # 诊断数据收集
        diag_vector_hits = 0
        diag_fts_hits = 0
        diag_refined_count = 0
        diag_gate = "none"
        # 检索线索（编码特异性）：向量路用"最近对话上下文 + 当轮提问"，
        # 当轮提问放末尾优先保留，上下文取尾部（最新）填剩余预算；
        # 指代型跟进消息（"按上面说的改"）由此获得可用的语义线索
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

        pre = await self.hybrid_presearch(query, query_vec)
        candidates = pre.candidates
        diag_vector_hits = pre.vector_hits
        diag_fts_hits = pre.fts_hits
        if not candidates and self._has_recall_intent(query):
            pre = await self.hybrid_presearch(query, query_vec, fallback=True)
            candidates = pre.candidates
            diag_vector_hits = pre.vector_hits
            diag_fts_hits = pre.fts_hits
        if not candidates:
            result.diagnostics = {
                "degraded": bool(result.degraded),
                "vector_hits": diag_vector_hits,
                "fts_hits": diag_fts_hits,
                "top_vector_score": round(pre.top_vector_score, 4),
                "gate": "presearch_empty",
                "context_chars": len(context_text or ""),
                "retrieval_time_ms": round((time.perf_counter() - _start) * 1000, 2),
                "refined_count": 0,
            }
            return result

        # 第 2 层：LLM 结合上下文精筛至 0-3 条（否决门：判空即本轮无相关记忆，
        # 不再强制兜底 top-2；异常降级路径仍注入 top-3 未精筛）
        chosen_ids: list[str]
        if llm_available and self.llm_refine_fn:
            try:
                chosen_ids = await self.llm_refine_fn(
                    query, [{"id": c.memory_id, "title": c.title,
                             "summary": c.summary} for c in candidates[:10]],
                    session_id=session_id, context_text=context_text)
                chosen_ids = chosen_ids[:3]
                diag_refined_count = len(chosen_ids)
                if not chosen_ids:
                    diag_gate = "refine_empty"
            except Exception:  # noqa: BLE001
                chosen_ids = self._degrade_pick(candidates)
                result.degraded = "第 2 层精筛不可用，按得分降级注入（已过滤弱尾）"
                diag_refined_count = len(chosen_ids)
        else:
            chosen_ids = self._degrade_pick(candidates)

        # 第 3 层：加载详情 + 1 跳交叉引用（chosen_ids 为空时自然跳过）
        # _load_detail 含同步读 md 文件，丢工作线程避免磁盘忙时阻塞事件循环
        for mid in chosen_ids:
            detail = await asyncio.to_thread(self._load_detail, mid)
            if detail:
                result.hits.append(detail)
                result.loaded_ids.append(mid)

        related = self._expand_one_hop(
            chosen_ids, limit=2, query_vec=query_vec)
        for rmid in related:
            detail = await asyncio.to_thread(self._load_detail, rmid)
            if detail:
                detail["relation"] = "关联记忆"
                result.related.append(detail)
                result.loaded_ids.append(rmid)

        _elapsed_ms = round((time.perf_counter() - _start) * 1000, 2)
        # 检索 trace：候选数 / 命中数 / 耗时
        logger.info("检索 trace：candidates=%d hits=%d related=%d %.0fms degraded=%s",
                    len(candidates), len(result.hits), len(result.related),
                    _elapsed_ms, result.degraded or "-")

        result.diagnostics = {
            "degraded": bool(result.degraded),
            "vector_hits": diag_vector_hits,
            "fts_hits": diag_fts_hits,
            "top_vector_score": round(pre.top_vector_score, 4),
            "gate": diag_gate,
            "context_chars": len(context_text or ""),
            "retrieval_time_ms": _elapsed_ms,
            "refined_count": diag_refined_count,
        }
        return result

    def _degrade_pick(self, candidates: list) -> list[str]:
        """精筛不可用时的降级挑选：候选已经预筛阀值准入，此处再按相对得分
        过滤明显弱于首条的尾部，最多取 3 条，至少保留最相关的 1 条。"""
        if not candidates:
            return []
        top = candidates[0].final_score or 0.0
        ratio = self.config.get("refine_degrade_score_ratio", 0.5)
        picked = [c.memory_id for c in candidates[:3]
                  if top <= 0 or (c.final_score or 0.0) >= top * ratio]
        return picked or [candidates[0].memory_id]

    def _expand_one_hop(self, seed_ids: list[str], limit: int,
                        query_vec=None) -> list[str]:
        vthr = self.config.get("vector_threshold", 0.55)
        seen = set(seed_ids)
        # 先收集候选目标再批量取元数据，避免逐条 palace.get 的 N+1
        targets: list[str] = []
        for mid in seed_ids:
            for link in self.palace.outlinks(mid):
                t = link["target_id"]
                if t not in seen:
                    seen.add(t)
                    targets.append(t)
        rows_map = self.palace.get_many(targets)
        out: list[str] = []
        for t in targets:
            row = rows_map.get(t)
            if not row or row["lifecycle"] not in ("active", "stable", "stale"):
                continue
            # 扩散衰减：关联记忆须与本轮线索余弦过准入线才注入；
            # query_vec 不可用（embedding 降级）时维持原行为不额外收紧
            if query_vec is not None:
                sim = self.vs.cosine_to(t, query_vec)
                if sim is not None and sim < vthr:
                    continue
            out.append(t)
            if len(out) >= limit:
                return out
        return out

    def _load_detail(self, mid: str) -> dict | None:
        row = self.palace.get(mid)
        if not row:
            return None
        f = self.data_dir / row["md_path"]
        if not f.exists():
            # 第 3 层降级：md 损坏用 summary 兜底 + 记一致性告警
            logger.warning("md 文件缺失，summary 兜底：%s", mid)
            return {"id": mid, "title": row["title"], "summary": row["summary"],
                    "detail": row["summary"], "confidence": row["confidence"],
                    "degraded": True}
        doc = parse_memory_md(f.read_text(encoding="utf-8"))
        return {"id": mid, "title": doc.title, "summary": doc.summary,
                "detail": doc.detail, "confidence": row["confidence"],
                "lifecycle": row["lifecycle"], "links": doc.links}

    @staticmethod
    def _has_recall_intent(query: str) -> bool:
        return any(re.search(p, query) for p in RECALL_INTENT_PATTERNS)


def _fts_escape(query: str) -> str:
    """将用户查询转为 FTS5 安全的 MATCH 表达式（分词后 OR 连接，加双引号防语法）。"""
    tokens = re.findall(r"[\w\u4e00-\u9fff]+", query)
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens[:20])
