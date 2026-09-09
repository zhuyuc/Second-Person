"""
VectorStore —— numpy 内存向量缓存 + vectors 表 BLOB 持久化。

对齐产品文档 §存储层 vectors / 开发文档 §6.7：
- 启动分批异步加载 active+stable+stale（每批 1000），加载期间检索降级 FTS5 单路
- 增量：append 到数组尾部 + 更新 id->index 映射
- 删除/归档：不物理删除数组元素（避免重排），只从映射移除并标记 tombstone
- tombstone 超总量 20% 触发双缓冲紧凑重建（无锁，替换引用原子）
- 维度绑定：数组维度由当前 Embedding 模型决定，切换维度必须走完整迁移
- 内存上限 vector_cache_max_mb；超过上限时释放整份向量缓存，检索单路
  回退 FTS，避免返回只覆盖部分记忆的不完整向量结果
"""
from __future__ import annotations

import logging
import threading

import numpy as np
from infrastructure.timeutil import now_cst

try:
    from usearch.index import Index as UsearchIndex
except ImportError:  # pragma: no cover - optional deploy-time fallback
    UsearchIndex = None

logger = logging.getLogger("second_person.vector_store")

TOMBSTONE_RATIO = 0.20
_MEBIBYTE = 1024 * 1024
# HNSW graph/link bookkeeping varies with vector dimension and build settings.
# This conservative planning figure is used for cache admission, not reporting
# an exact allocator footprint.
_ANN_ESTIMATED_BYTES_PER_VECTOR = 256


def serialize_vector(vec: list[float] | np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def deserialize_vector(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


class VectorStore:
    def __init__(self, db, cache_max_mb: float = 512, *,
                 ann_min_vectors: int = 10_000,
                 ann_candidate_multiplier: int = 5):
        self.db = db
        self.cache_max_mb = cache_max_mb
        self._lock = threading.RLock()
        self._matrix: np.ndarray | None = None       # (N, dim) float32
        self._id_to_index: dict[str, int] = {}
        self._index_to_id: list[str | None] = []      # None = tombstone
        self._tombstones = 0
        self._dim: int | None = None
        # 缓存预算不足时不保留部分向量。search() 返回空，由 Retriever 的
        # FTS 分支完整接管；重新 load()（例如调高预算后）可恢复向量检索。
        self._cache_disabled = False
        self._ann_min_vectors = max(1, int(ann_min_vectors))
        self._ann_candidate_multiplier = max(1, int(ann_candidate_multiplier))
        self._ann_index = None
        self.loaded = False
        # ---- Embedding 迁移双缓冲 ----
        self._staging: dict[str, np.ndarray] = {}   # memory_id -> 新模型向量
        self._migrating = False

    @property
    def dim(self) -> int | None:
        return self._dim

    @property
    def cache_degraded(self) -> bool:
        """是否因缓存预算而关闭了内存向量检索。"""
        return self._cache_disabled

    @property
    def ann_enabled(self) -> bool:
        """Whether the active cache is served by a HNSW candidate index."""
        return self._ann_index is not None

    def _budget_bytes(self) -> int:
        """缓存矩阵允许使用的最大字节数。无效配置按 0 处理。"""
        try:
            return max(0, int(float(self.cache_max_mb) * _MEBIBYTE))
        except (TypeError, ValueError):
            logger.warning("无效的向量缓存上限 %r，按 0MB 处理", self.cache_max_mb)
            return 0

    def _disable_cache(self, dim: int | None) -> None:
        """丢弃整个缓存而非保留前缀，确保不会产生部分向量召回。"""
        self._matrix = None
        self._id_to_index = {}
        self._index_to_id = []
        self._tombstones = 0
        self._dim = dim
        self._ann_index = None
        self._cache_disabled = True

    def _ann_estimated_bytes(self, vector_count: int) -> int:
        if UsearchIndex is None or vector_count < self._ann_min_vectors:
            return 0
        return vector_count * _ANN_ESTIMATED_BYTES_PER_VECTOR

    def _projected_cache_bytes(self, matrix_bytes: int, vector_count: int) -> int:
        return matrix_bytes + self._ann_estimated_bytes(vector_count)

    def _rebuild_ann(self) -> None:
        """Build a copy-owned HNSW index after a cache snapshot is finalized.

        Callers hold ``_lock``.  The optional dependency never prevents exact
        vector search: an index build failure leaves ``_ann_index`` unset.
        """
        self._ann_index = None
        if (UsearchIndex is None or self._cache_disabled or self._matrix is None
                or self._dim is None or len(self._id_to_index) < self._ann_min_vectors):
            return
        valid_indices = np.fromiter(
            (idx for idx, memory_id in enumerate(self._index_to_id)
             if memory_id is not None), dtype=np.uint64, count=len(self._id_to_index))
        if not len(valid_indices):
            return
        try:
            index = UsearchIndex(ndim=self._dim, metric="cos", dtype="f32",
                                connectivity=32, expansion_add=256,
                                expansion_search=128)
            index.add(valid_indices, self._matrix[valid_indices.astype(np.intp)])
            self._ann_index = index
            logger.info("HNSW 向量索引就绪：%d 条，dim=%d", len(valid_indices), self._dim)
        except Exception:  # noqa: BLE001 - exact numpy retrieval remains available
            logger.warning("HNSW 索引构建失败，回退精确向量检索", exc_info=True)

    def _ann_add(self, index: int, vector: np.ndarray) -> None:
        if self._ann_index is None:
            if len(self._id_to_index) >= self._ann_min_vectors:
                self._rebuild_ann()
            return
        try:
            self._ann_index.add(np.uint64(index), vector.reshape(1, -1))
        except Exception:  # noqa: BLE001
            logger.warning("HNSW 增量写入失败，重建索引", exc_info=True)
            self._rebuild_ann()

    def _ann_replace(self, index: int, vector: np.ndarray) -> None:
        if self._ann_index is None:
            return
        try:
            self._ann_index.remove(np.uint64(index))
            self._ann_index.add(np.uint64(index), vector.reshape(1, -1))
        except Exception:  # noqa: BLE001
            logger.warning("HNSW 向量更新失败，重建索引", exc_info=True)
            self._rebuild_ann()

    # ---- 启动加载 ---------------------------------------------------------
    def load(self) -> int:
        """分批加载 ready 向量到内存（每批 1000）。返回加载条数。"""
        with self._lock:
            self._matrix = None
            self._id_to_index.clear()
            self._index_to_id = []
            self._tombstones = 0
            self._dim = None
            self._cache_disabled = False

            # 在取出 BLOB 前先用 dim 估算矩阵大小。超过预算时完全跳过加载，
            # 避免为了判定超额反而瞬时分配一整份不可承受的向量矩阵。
            stats = self.db.query_one(
                "SELECT COUNT(*) AS n, COALESCE(SUM(COALESCE("
                "v.dim, length(v.embedding) / 4)), 0) AS components, "
                "MIN(v.dim) AS min_dim, MAX(v.dim) AS max_dim FROM vectors v "
                "JOIN memories m ON v.memory_id=m.id "
                "WHERE v.vector_status='ready' AND v.embedding IS NOT NULL "
                "AND m.lifecycle IN ('active','stable','stale')")
            component_count = int(stats["components"] or 0)
            vector_count = int(stats["n"] or 0)
            estimate_bytes = self._projected_cache_bytes(
                component_count * np.dtype(np.float32).itemsize, vector_count)
            min_dim = stats["min_dim"]
            max_dim = stats["max_dim"]
            cache_dim = int(min_dim) if min_dim is not None and min_dim == max_dim else None
            if estimate_bytes > self._budget_bytes():
                self._disable_cache(cache_dim)
                self.loaded = True
                logger.warning(
                    "向量缓存预计 %.1fMB 超过上限 %sMB，已回退 FTS 检索",
                    estimate_bytes / _MEBIBYTE, self.cache_max_mb)
                return 0

            rows = self.db.query_all(
                "SELECT v.memory_id, v.embedding, v.dim FROM vectors v "
                "JOIN memories m ON v.memory_id=m.id "
                "WHERE v.vector_status='ready' AND v.embedding IS NOT NULL "
                "AND m.lifecycle IN ('active','stable','stale')")
            batches: list[np.ndarray] = []
            buf: list[np.ndarray] = []
            BATCH = 1000
            for r in rows:
                arr = deserialize_vector(r["embedding"])
                if self._dim is None:
                    self._dim = int(r["dim"] or arr.shape[0])
                if arr.shape[0] != self._dim:
                    logger.warning("向量维度不一致，跳过 %s", r["memory_id"])
                    continue
                self._id_to_index[r["memory_id"]] = len(self._index_to_id)
                self._index_to_id.append(r["memory_id"])
                buf.append(arr)
                if len(buf) >= BATCH:
                    batches.append(np.vstack(buf))
                    buf = []
            if buf:
                batches.append(np.vstack(buf))
            self._matrix = np.vstack(batches) if batches else None
            self._tombstones = 0
            self._rebuild_ann()
            self.loaded = True
            n = len(self._index_to_id)
            logger.info("向量缓存加载完成：%d 条（分 %d 批），dim=%s",
                        n, len(batches), self._dim)
            return n

    # ---- 增量 -------------------------------------------------------------
    def add(self, memory_id: str, vec: list[float] | np.ndarray) -> None:
        with self._lock:
            if self._cache_disabled:
                return
            # 缓存持有自己的副本，调用方之后修改输入数组不会破坏检索快照。
            arr = np.array(vec, dtype=np.float32, copy=True)
            if self._dim is None:
                self._dim = arr.shape[0]
            if arr.shape[0] != self._dim:
                logger.error("拒绝写入维度不符的向量：%s", memory_id)
                return
            if memory_id in self._id_to_index:
                idx = self._id_to_index[memory_id]
                if self._matrix is not None:
                    # Copy-on-write：并发 search() 持有旧矩阵引用时可获得稳定快照。
                    updated = self._matrix.copy()
                    updated[idx] = arr
                    self._matrix = updated
                    self._ann_replace(idx, arr)
                return
            row = arr.reshape(1, -1)
            next_count = len(self._index_to_id) + 1
            next_matrix_bytes = row.nbytes if self._matrix is None \
                else self._matrix.nbytes + row.nbytes
            next_bytes = self._projected_cache_bytes(next_matrix_bytes, next_count)
            if next_bytes > self._budget_bytes():
                self._disable_cache(self._dim)
                logger.warning(
                    "向量缓存预计 %.1fMB 超过上限 %sMB，已回退 FTS 检索",
                    next_bytes / _MEBIBYTE, self.cache_max_mb)
                return
            self._matrix = row if self._matrix is None else np.vstack(
                [self._matrix, row])
            self._id_to_index[memory_id] = len(self._index_to_id)
            self._index_to_id.append(memory_id)
            self._ann_add(self._id_to_index[memory_id], arr)
            self._check_memory_limit()

    def remove(self, memory_id: str) -> None:
        """删除/归档：只标 tombstone，不重排。"""
        with self._lock:
            idx = self._id_to_index.pop(memory_id, None)
            if idx is None:
                return
            self._index_to_id[idx] = None
            self._tombstones += 1
            if self._ann_index is not None:
                try:
                    self._ann_index.remove(np.uint64(idx))
                except Exception:  # noqa: BLE001
                    logger.warning("HNSW 删除失败，重建索引", exc_info=True)
                    self._rebuild_ann()
            if self._matrix is not None and len(self._index_to_id) and \
                    self._tombstones / len(self._index_to_id) > TOMBSTONE_RATIO:
                self._compact()

    # ---- 检索 -------------------------------------------------------------
    def search(self, query_vec: list[float] | np.ndarray, top_k: int,
               threshold: float) -> list[tuple[str, float]]:
        """余弦相似度 ≥ threshold 过滤后取 top_k。返回 [(memory_id, score)]。
        锁内只做不可变矩阵引用和 ID 元组快照，矩阵运算在锁外执行，
        避免大规模向量下持锁期间阻塞 add/remove/load。"""
        q = np.asarray(query_vec, dtype=np.float32)
        with self._lock:
            if self._cache_disabled or self._matrix is None or self._matrix.shape[0] == 0:
                return []
            mat = self._matrix
            index_to_id = tuple(self._index_to_id)
            dim = self._dim
            ann_candidates = None
            if self._ann_index is not None and top_k > 0 and q.shape[0] == dim:
                try:
                    count = min(len(self._id_to_index), max(
                        top_k, top_k * self._ann_candidate_multiplier))
                    matches = self._ann_index.search(q, count=count)
                    ann_candidates = np.asarray(matches.keys, dtype=np.intp)
                except Exception:  # noqa: BLE001
                    logger.warning("HNSW 查询失败，回退精确向量检索", exc_info=True)
        if top_k <= 0:
            return []
        if dim is None or q.shape[0] != dim:
            return []
        qn = q / (np.linalg.norm(q) + 1e-8)
        if ann_candidates is not None:
            candidates = ann_candidates[
                (ann_candidates >= 0) & (ann_candidates < len(index_to_id))]
            candidates = np.asarray([
                idx for idx in candidates if index_to_id[int(idx)] is not None], dtype=np.intp)
            if not len(candidates):
                return []
            candidate_vectors = mat[candidates]
            scores = (candidate_vectors @ qn) / (
                np.linalg.norm(candidate_vectors, axis=1) + 1e-8)
            eligible = np.isfinite(scores) & (scores >= threshold)
            selected = np.flatnonzero(eligible)
            selected = sorted(selected, key=lambda i: (-float(scores[i]), int(candidates[i])))[:top_k]
            return [(index_to_id[int(candidates[i])], float(scores[i])) for i in selected]
        norms = np.linalg.norm(mat, axis=1) + 1e-8
        sims = (mat @ qn) / norms
        # 相似度数组是本次查询的新分配，可原地屏蔽 tombstone、阈值外和非有限值。
        # 这样 argpartition 只需选择有效候选，无须对 N 个分数全量排序。
        valid = np.fromiter((mid is not None for mid in index_to_id),
                            dtype=np.bool_, count=len(index_to_id))
        eligible = valid & np.isfinite(sims) & (sims >= threshold)
        candidate_count = int(np.count_nonzero(eligible))
        if candidate_count == 0:
            return []
        sims[~eligible] = -np.inf
        selected_count = min(top_k, candidate_count)
        if selected_count == candidate_count:
            selected = np.flatnonzero(eligible)
        else:
            selected = np.argpartition(-sims, selected_count - 1)[:selected_count]
        # 仅排序已选中的 K 个结果，保持输出按分数递减；索引作为稳定的并列分数裁决。
        ordered = sorted(selected, key=lambda i: (-float(sims[i]), int(i)))
        return [(index_to_id[int(i)], float(sims[i])) for i in ordered]

    def top_similar(self, query_vec, n: int = 20) -> list[tuple[str, float]]:
        """取相似度最高的前 n 条（不设阈值），供 Distiller 去重候选集。"""
        return self.search(query_vec, top_k=n, threshold=-1.0)

    def cosine_to(self, memory_id: str, query_vec) -> float | None:
        """单条记忆与查询向量的余弦相似度；未缓存/维度不符返回 None。
        供检索 1 跳扩展做相关性门槛（扩散衰减）。"""
        with self._lock:
            idx = self._id_to_index.get(memory_id)
            if idx is None or self._matrix is None:
                return None
            q = np.asarray(query_vec, dtype=np.float32)
            if q.shape[0] != self._dim:
                return None
            v = self._matrix[idx]
            denom = (np.linalg.norm(v) + 1e-8) * (np.linalg.norm(q) + 1e-8)
            return float(v @ q / denom)

    # ---- 紧凑重建（持锁重建有效行） ------------------------------------
    def _compact(self) -> None:
        if self._cache_disabled:
            return
        valid_ids = [mid for mid in self._index_to_id if mid is not None]
        if not valid_ids:
            self._matrix, self._index_to_id, self._id_to_index, self._tombstones = \
                None, [], {}, 0
            return
        # 在新数组中按现有映射抽取有效行（调用方已持锁）
        new_matrix = np.vstack([
            self._matrix[i] for i, mid in enumerate(self._index_to_id) if mid is not None
        ])
        self._matrix = new_matrix
        self._index_to_id = valid_ids
        self._id_to_index = {mid: i for i, mid in enumerate(valid_ids)}
        self._tombstones = 0
        self._rebuild_ann()
        logger.info("向量缓存紧凑重建完成，有效 %d 条", len(valid_ids))

    # ---- 内存监控 ---------------------------------------------------------
    def memory_mb(self) -> float:
        if self._matrix is None:
            return 0.0
        return self._projected_cache_bytes(
            self._matrix.nbytes, len(self._id_to_index)) / _MEBIBYTE

    def _check_memory_limit(self) -> None:
        mb = self.memory_mb()
        if mb > self.cache_max_mb:
            logger.warning("向量缓存 %.1fMB 超过上限 %dMB", mb, self.cache_max_mb)

    def consistency_check(self) -> dict[str, int]:
        """numpy 数组条数 vs vectors 表 ready 行数比对。
        口径与 load() 一致：只统计可检索生命周期（active/stable/stale）的记忆，
        否则 archived 记忆的向量会被计入 db_ready 造成永久性不一致误报。"""
        db_count = self.db.query_one(
            "SELECT count(*) c FROM vectors v JOIN memories m ON v.memory_id=m.id "
            "WHERE v.vector_status='ready' "
            "AND m.lifecycle IN ('active','stable','stale')")["c"]
        mem_count = len(self._id_to_index)
        return {"memory": mem_count, "db_ready": db_count,
                "consistent": int(mem_count == db_count)}

    # ---- 持久化到 vectors 表 ----------------------------------------------
    def persist(self, memory_id: str, vec: list[float] | np.ndarray,
                embedding_version: str, status: str = "ready") -> None:
        arr = np.asarray(vec, dtype=np.float32)
        self.db.execute(
            "INSERT INTO vectors(memory_id,embedding,vector_status,dim,"
            "embedding_version,is_stale,updated_at) VALUES(?,?,?,?,?,0,?) "
            "ON CONFLICT(memory_id) DO UPDATE SET embedding=excluded.embedding, "
            "vector_status=excluded.vector_status, dim=excluded.dim, "
            "embedding_version=excluded.embedding_version, updated_at=excluded.updated_at",
            (memory_id, serialize_vector(arr), status, arr.shape[0],
             embedding_version, now_cst().isoformat(timespec="seconds")))

    # ---- Embedding 迁移双缓冲（产品文档 §存储层 vectors / §LLM Provider） ----
    def begin_migration(self) -> None:
        """进入迁移态：检索仍读旧数组，新向量写入独立 staging。"""
        with self._lock:
            self._staging = {}
            self._migrating = True

    @property
    def migrating(self) -> bool:
        return self._migrating

    def stage_vector(self, memory_id: str, vec: list[float] | np.ndarray) -> None:
        """迁移期间把新模型向量写入 staging（不影响旧数组的检索）。"""
        arr = np.asarray(vec, dtype=np.float32)
        with self._lock:
            if self._staging:
                expected_dim = next(iter(self._staging.values())).shape[0]
                if arr.shape[0] != expected_dim:
                    logger.error("stage_vector 维度不一致：%s 期望 %d 实际 %d",
                                 memory_id, expected_dim, arr.shape[0])
                    return
            self._staging[memory_id] = arr

    def commit_migration(self, new_dim: int) -> int:
        """完成迁移：用 staging 原子替换主数组与映射（引用赋值原子）。"""
        with self._lock:
            ids = list(self._staging.keys())
            staged_matrix_bytes = sum(vec.nbytes for vec in self._staging.values())
            staged_bytes = self._projected_cache_bytes(staged_matrix_bytes, len(ids))
            if staged_bytes > self._budget_bytes():
                self._disable_cache(new_dim)
                self._staging = {}
                self._migrating = False
                self.loaded = True
                logger.warning(
                    "迁移后向量缓存预计 %.1fMB 超过上限 %sMB，已回退 FTS 检索",
                    staged_bytes / _MEBIBYTE, self.cache_max_mb)
                return 0
            if ids:
                new_matrix = np.vstack([self._staging[i] for i in ids])
                self._matrix = new_matrix
                self._index_to_id = list(ids)
                self._id_to_index = {mid: i for i, mid in enumerate(ids)}
            else:
                self._matrix, self._index_to_id, self._id_to_index = None, [], {}
            self._dim = new_dim
            self._tombstones = 0
            self._cache_disabled = False
            self._rebuild_ann()
            self._staging = {}
            self._migrating = False
            return len(ids)

    def abort_migration(self) -> None:
        with self._lock:
            self._staging = {}
            self._migrating = False
