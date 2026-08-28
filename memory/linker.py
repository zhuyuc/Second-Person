"""
Linker —— 交叉引用与孤立记忆关联（产品文档 §交叉引用图谱 / §记忆维护 孤立检测）。

- 5 类关系：related / evolved_from / superseded_by / contradicts / supports
- 自动建立时机：提炼时（相似度落建引用区间）、矛盾检测时、Lint 健康检查时
- 孤立记忆采纳：按语义相似度自动建立 related 引用
- 建链提交 FileWriter 专用 add_link op，由消费时读最新 md 原子追加，
  避免提交时读磁盘快照的两类丢边：md 尚未落盘被静默跳过、
  多个全量快照 update 相互覆盖（lost update）
- 知识图谱边由 memory_entity_links 共现聚合（见 Palace / API 层）
"""
from __future__ import annotations

from pathlib import Path


class Linker:
    def __init__(self, db, palace, vector_store, file_writer, data_dir, config):
        self.db = db
        self.palace = palace
        self.vs = vector_store
        self.fw = file_writer
        self.data_dir = Path(data_dir)
        self.config = config

    async def add_link(self, source: str, target: str, link_type: str = "related",
                       bidirectional: bool = True) -> None:
        """在 source→target 建立引用：提交 add_link 专用写请求，FIFO 队列保证
        消费时此前排队的 create/update 均已落盘，再读最新 md 追加边。"""
        for a, b in ([(source, target)] + ([(target, source)] if bidirectional else [])):
            await self.fw.submit("memory", {
                "op": "add_link", "memory_id": a, "target": b,
                "link_type": link_type})

    async def suggest_and_link_orphan(self, orphan_id: str) -> str | None:
        """孤立记忆采纳：找最相似记忆建 related。返回关联到的 memory_id。"""
        row = self.palace.get(orphan_id)
        if not row:
            return None
        vrow = self.db.query_one(
            "SELECT embedding FROM vectors WHERE memory_id=? AND vector_status='ready'",
            (orphan_id,))
        if not vrow or not vrow["embedding"]:
            return None
        from .vector_store import deserialize_vector
        vec = deserialize_vector(vrow["embedding"])
        candidates = self.vs.top_similar(vec, n=5)
        for mid, score in candidates:
            from . import _constants as _mem_const
            _, link_thr, _ = _mem_const.dedup_thresholds(self.config)
            if mid != orphan_id and score >= link_thr:
                await self.add_link(orphan_id, mid, "related")
                return mid
        return None
