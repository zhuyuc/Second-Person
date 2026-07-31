"""
VectorStore —— numpy 内存向量缓存 + vectors 表 BLOB 持久化。

对齐产品文档 §存储层 vectors / 开发文档 §6.7：
- 启动分批异步加载 active+stable+stale（每批 1000），加载期间检索降级 FTS5 单路
- 增量：append 到数组尾部 + 更新 id->index 映射
- 删除/归档：不物理删除数组元素（避免重排），只从映射移除并标记 tombstone
- tombstone 超总量 20% 触发双缓冲紧凑重建（无锁，替换引用原子）
- 维度绑定：数组维度由当前 Embedding 模型决定，切换维度必须走完整迁移
- 内存上限 vector_cache_max_mb，超出推系统通知告警（不阻断写入）
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime

import numpy as np
from infrastructure.timeutil import now_cst

logger = logging.getLogger("second_person.vector_store")

TOMBSTONE_RATIO = 0.20


def serialize_vector(vec: list[float] | np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def deserialize_vector(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


class VectorStore:
    def __init__(self, db, cache_max_mb: int = 512):
        self.db = db
        self.cache_max_mb = cache_max_mb
        self._lock = threading.RLock()
        self._matrix: np.ndarray | None = None       # (N, dim) float32
        self._id_to_index: dict[str, int] = {}
        self._index_to_id: list[str | None] = []      # None = tombstone
        self._tombstones = 0
        self._dim: int | None = None
        self.loaded = False
        # ---- Embedding 迁移双缓冲 ----
        self._staging: dict[str, np.ndarray] = {}   # memory_id -> 新模型向量
        self._migrating = False

    @property
    def dim(self) -> int | None:
        return self._dim

    # ---- 启动加载 ---------------------------------------------------------
    def load(self) -> int:
        """分批加载 ready 向量到内存（每批 1000）。返回加载条数。"""
        with self._lock:
            rows = self.db.query_all(
                "SELECT v.memory_id, v.embedding, v.dim FROM vectors v "
                "JOIN memories m ON v.memory_id=m.id "
                "WHERE v.vector_status='ready' AND v.embedding IS NOT NULL "
                "AND m.lifecycle IN ('active','stable','stale')")
            self._id_to_index.clear()
            self._index_to_id = []
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
            self.loaded = True
            n = len(self._index_to_id)
            logger.info("向量缓存加载完成：%d 条（分 %d 批），dim=%s",
                        n, len(batches), self._dim)
            return n

    # ---- 增量 -------------------------------------------------------------
    def add(self, memory_id: str, vec: list[float] | np.ndarray) -> None:
        with self._lock:
            arr = np.asarray(vec, dtype=np.float32)
            if self._dim is None:
                self._dim = arr.shape[0]
            if arr.shape[0] != self._dim:
                logger.error("拒绝写入维度不符的向量：%s", memory_id)
                return
            if memory_id in self._id_to_index:
                idx = self._id_to_index[memory_id]
                if self._matrix is not None:
                    self._matrix[idx] = arr
                return
            row = arr.reshape(1, -1)
            self._matrix = row if self._matrix is None else np.vstack(
                [self._matrix, row])
            self._id_to_index[memory_id] = len(self._index_to_id)
            self._index_to_id.append(memory_id)
            self._check_memory_limit()

    def remove(self, memory_id: str) -> None:
        """删除/归档：只标 tombstone，不重排。"""
        with self._lock:
            idx = self._id_to_index.pop(memory_id, None)
            if idx is None:
                return
            self._index_to_id[idx] = None
            self._tombstones += 1
            if self._matrix is not None and len(self._index_to_id) and \
                    self._tombstones / len(self._index_to_id) > TOMBSTONE_RATIO:
                self._compact()

    # ---- 检索 -------------------------------------------------------------
    def search(self, query_vec: list[float] | np.ndarray, top_k: int,
               threshold: float) -> list[tuple[str, float]]:
        """余弦相似度 ≥ threshold 过滤后取 top_k。返回 [(memory_id, score)]。
        锁内只做快照（矩阵引用 + id 映射拷贝），矩阵运算在锁外执行，
        避免大规模向量下持锁期间阻塞 add/remove/load。"""
        with self._lock:
            if self._matrix is None or self._matrix.shape[0] == 0:
                return []
            mat = self._matrix
            index_to_id = list(self._index_to_id)   # 快照，与 mat 同一时刻一致
            dim = self._dim
        q = np.asarray(query_vec, dtype=np.float32)
        if dim is None or q.shape[0] != dim:
            return []
        qn = q / (np.linalg.norm(q) + 1e-8)
        norms = np.linalg.norm(mat, axis=1) + 1e-8
        sims = (mat @ qn) / norms
        order = np.argsort(-sims)
        out: list[tuple[str, float]] = []
        for i in order:
            if i >= len(index_to_id):
                continue
            mid = index_to_id[i]
            if mid is None:      # tombstone
                continue
            score = float(sims[i])
            if score < threshold:
                break
            out.append((mid, score))
            if len(out) >= top_k:
                break
        return out

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
        logger.info("向量缓存紧凑重建完成，有效 %d 条", len(valid_ids))

    # ---- 内存监控 ---------------------------------------------------------
    def memory_mb(self) -> float:
        if self._matrix is None:
            return 0.0
        return self._matrix.nbytes / (1024 * 1024)

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
        with self._lock:
            self._staging[memory_id] = np.asarray(vec, dtype=np.float32)

    def commit_migration(self, new_dim: int) -> int:
        """完成迁移：用 staging 原子替换主数组与映射（引用赋值原子）。"""
        with self._lock:
            ids = list(self._staging.keys())
            if ids:
                new_matrix = np.vstack([self._staging[i] for i in ids])
                self._matrix = new_matrix
                self._index_to_id = list(ids)
                self._id_to_index = {mid: i for i, mid in enumerate(ids)}
            else:
                self._matrix, self._index_to_id, self._id_to_index = None, [], {}
            self._dim = new_dim
            self._tombstones = 0
            self._staging = {}
            self._migrating = False
            return len(ids)

    def abort_migration(self) -> None:
        with self._lock:
            self._staging = {}
            self._migrating = False
