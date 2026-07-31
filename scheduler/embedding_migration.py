"""
Embedding 迁移执行器（产品文档 §LLM Provider 抽象层 / §存储层 vectors）。

- 迁移状态锁：embedding_migration 存在 running 记录时拒绝重复触发（路由层已校验）
- 双缓冲：迁移期间旧向量留在 VectorStore 主数组供检索；新向量写入 staging + vectors 表
- 后台队列串行调用新模型 Embedding，尊重 rate limit，可暂停/续跑
- 完成后原子 commit：VectorStore 指针切到新数组
- 旧向量迁移前备份到 vectors_old_backup，保留 30 天可回滚
- 单路补偿：迁移未完成期间 embedding_version=new 的记忆只走 FTS5，
  RRF 融合按 1.5 倍系数补偿（见 Retriever）
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from infrastructure.event_bus import (EVT_EMBEDDING_MIGRATION_COMPLETED,
                                      EVT_TASK_PROGRESS)
from memory.vector_store import deserialize_vector

logger = logging.getLogger("second_person.emb_migration")

BATCH = 32
# 备份/回滚批量写分块：控制单次持写锁时长，避免与事件循环线程的 db 写互斥长等
WRITE_CHUNK = 500


class MigrationRunner:
    def __init__(self, db, vector_store, providers, llm_client, event_bus=None,
                 notifier=None):
        self.db = db
        self.vs = vector_store
        self.providers = providers
        self.llm = llm_client
        self.bus = event_bus
        self.notify = notifier or (lambda t, m: None)
        self._task: asyncio.Task | None = None

    def start(self, migration_id: int, target_provider_id: str) -> None:
        self._task = asyncio.create_task(
            self._run(migration_id, target_provider_id))

    async def _run(self, migration_id: int, target_provider_id: str) -> None:
        try:
            self.vs.begin_migration()
            # 全表向量 BLOB 备份为同步重写，丢工作线程执行
            await asyncio.to_thread(self._backup_old_vectors)
            snap = self.providers.snapshot(target_provider_id)
            if snap is None:
                self._fail(migration_id, "目标 Provider 不存在")
                return
            rows = self.db.query_all(
                "SELECT v.memory_id, m.title, m.summary FROM vectors v "
                "JOIN memories m ON v.memory_id=m.id "
                "WHERE m.lifecycle IN ('active','stable','stale')")
            total = len(rows)
            new_dim = None
            done = 0
            for i in range(0, total, BATCH):
                # 暂停/放弃控制
                st = self.db.query_one(
                    "SELECT status FROM embedding_migration WHERE id=?", (migration_id,))
                if not st:
                    self.vs.abort_migration()
                    return
                while st and st["status"] == "paused":
                    await asyncio.sleep(2)
                    st = self.db.query_one(
                        "SELECT status FROM embedding_migration WHERE id=?", (migration_id,))
                if not st or st["status"] == "failed":
                    self.vs.abort_migration()
                    return

                batch = rows[i:i + BATCH]
                texts = [f"{r['title']} {r['summary'] or ''}".strip()
                         for r in batch]
                try:
                    vecs = await self.llm.embed(snap, texts)
                except Exception as e:  # noqa: BLE001
                    self._fail(migration_id, f"向量化失败：{e}")
                    self.vs.abort_migration()
                    return
                for r, vec in zip(batch, vecs):
                    new_dim = len(vec)
                    self.vs.stage_vector(r["memory_id"], vec)
                    self.vs.persist(r["memory_id"], vec, "new", status="ready")
                    self.db.execute("UPDATE vectors SET dim=? WHERE memory_id=?",
                                    (new_dim, r["memory_id"]))
                done += len(batch)
                self.db.execute(
                    "UPDATE embedding_migration SET done_count=? WHERE id=?",
                    (done, migration_id))
                if self.bus:
                    await self.bus.publish(EVT_TASK_PROGRESS, {
                        "task_id": f"embedding_migration_{migration_id}",
                        "progress": int(done / max(1, total) * 100)})

            # 原子提交
            committed = self.vs.commit_migration(new_dim or self.vs.dim or 0)
            # 切换 embedding 分配到新 Provider，后续写入/检索均用新模型
            self.providers.set_assignment("embedding", target_provider_id)
            self.db.execute("UPDATE vectors SET embedding_version='current'")
            self.db.execute(
                "UPDATE embedding_migration SET status='completed', done_count=? WHERE id=?",
                (committed, migration_id))
            if self.bus:
                await self.bus.publish(EVT_EMBEDDING_MIGRATION_COMPLETED,
                                       {"migration_id": migration_id})
            self.notify("embedding_migration",
                        f"Embedding 迁移完成，共 {committed} 条")
        except Exception as e:  # noqa: BLE001
            logger.exception("迁移执行异常")
            self._fail(migration_id, str(e))
            self.vs.abort_migration()

    def _backup_old_vectors(self) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        rows = self.db.query_all(
            "SELECT memory_id, embedding, dim, embedding_version FROM vectors "
            "WHERE embedding IS NOT NULL")
        params = [(r["memory_id"], r["embedding"], r["dim"],
                   r["embedding_version"], now) for r in rows]
        # 分块提交，避免单次长事务持写锁
        for i in range(0, len(params), WRITE_CHUNK):
            self.db.executemany(
                "INSERT OR REPLACE INTO vectors_old_backup(memory_id,embedding,dim,"
                "embedding_version,backed_at) VALUES(?,?,?,?,?)",
                params[i:i + WRITE_CHUNK])

    def _fail(self, migration_id: int, reason: str) -> None:
        self.db.execute("UPDATE embedding_migration SET status='failed' WHERE id=?",
                        (migration_id,))
        self.notify("embedding_migration", f"Embedding 迁移失败：{reason}")

    def rollback(self, migration_id: int) -> int:
        """从 vectors_old_backup 恢复旧向量（迁移放弃/回滚）。"""
        rows = self.db.query_all("SELECT * FROM vectors_old_backup")
        for r in rows:
            self.db.execute(
                "UPDATE vectors SET embedding=?, dim=?, embedding_version=?, "
                "vector_status='ready' WHERE memory_id=?",
                (r["embedding"], r["dim"], r["embedding_version"], r["memory_id"]))
        self.db.execute("UPDATE embedding_migration SET status='failed' WHERE id=?",
                        (migration_id,))
        self.vs.load()
        return len(rows)

    def purge_old_backups(self, days: int = 30) -> int:
        cutoff = (datetime.now() - timedelta(days=days)
                  ).isoformat(timespec="seconds")
        cur = self.db.execute(
            "DELETE FROM vectors_old_backup WHERE backed_at < ?", (cutoff,))
        return cur.rowcount if cur else 0
