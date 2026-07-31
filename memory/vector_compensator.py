"""
向量补偿协程（产品文档 §记忆写入 第 6 步 / 开发文档 §6.7）。

常驻协程轮询 vector_status='pending' 的记忆行，按批（最多 32 条）调 Embedding：
- 成功 → 写 BLOB + 置 'ready' + append 到 numpy 缓存
- 失败重试 3 次后置 'failed' 并推系统通知
实际只处理两类 pending：Distiller 取向量失败降级写入的条目、Embedding 迁移期间的新条目。
嵌入文本固定为 title + summary 拼接（不含 detail）。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Awaitable, Callable
from infrastructure.timeutil import now_cst

logger = logging.getLogger("second_person.vector_compensator")

BATCH = 32
MAX_RETRY = 3
POLL_INTERVAL = 5.0

EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]


class VectorCompensator:
    def __init__(self, db, vector_store, embed_fn: EmbedFn,
                 notifier: Callable[[str, str], None] | None = None,
                 embedding_version: str = "v1",
                 rededup_fn: Callable[[str], Awaitable[object]] | None = None):
        self.db = db
        self.vs = vector_store
        self.embed_fn = embed_fn
        self.notify = notifier or (lambda t, m: None)
        self.embedding_version = embedding_version
        # 向量补齐后的回溯去重回调（Distiller.rededup_memory）：修复提炼当刻
        # Embedding 不可用导致的重复记忆（dedup_pending 标记未被消费的缺口）。
        self.rededup_fn = rededup_fn
        self._running = False
        self._task: asyncio.Task | None = None
        self._retry_counts: dict[str, int] = {}

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while self._running:
            try:
                processed = await self.run_once()
                if processed == 0:
                    await asyncio.sleep(POLL_INTERVAL)
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                logger.exception("向量补偿轮询异常")
                await asyncio.sleep(POLL_INTERVAL)

    async def run_once(self) -> int:
        """处理一批 pending 向量。返回处理条数。"""
        rows = self.db.query_all(
            "SELECT v.memory_id, m.title, m.summary FROM vectors v "
            "JOIN memories m ON v.memory_id=m.id "
            "WHERE v.vector_status='pending' "
            "AND m.lifecycle IN ('active','stable','stale') LIMIT ?", (BATCH,))
        if not rows:
            return 0
        ids = [r["memory_id"] for r in rows]
        texts = [f"{r['title']} {r['summary'] or ''}".strip() for r in rows]
        try:
            vectors = await self.embed_fn(texts)
        except Exception as e:  # noqa: BLE001
            for mid in ids:
                self._retry_counts[mid] = self._retry_counts.get(mid, 0) + 1
                if self._retry_counts[mid] >= MAX_RETRY:
                    self.db.execute(
                        "UPDATE vectors SET vector_status='failed', updated_at=? "
                        "WHERE memory_id=?",
                        (now_cst().isoformat(timespec="seconds"), mid))
                    self.notify("vector_failed", f"记忆 {mid} 向量化失败：{e}")
            return len(ids)

        for mid, vec in zip(ids, vectors):
            self.vs.persist(mid, vec, self.embedding_version, status="ready")
            self.vs.add(mid, vec)
            self._retry_counts.pop(mid, None)
        # 向量已全部入缓存后再回溯去重（按 id 升序，保证候选已就绪且收敛）。
        if self.rededup_fn:
            for mid in sorted(ids):
                try:
                    await self.rededup_fn(mid)
                except Exception:  # noqa: BLE001 - 去重失败不影响向量补偿主流程
                    logger.exception("回溯去重异常：mid=%s", mid)
        return len(ids)
