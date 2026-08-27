"""lifecycle.update_access_stats 跨会话去重 + is_important 衰减 契约。"""
from __future__ import annotations

import asyncio
from pathlib import Path

from infrastructure.db import Database
from memory.file_writer import FileWriter
from memory.lifecycle import LifecycleManager
from memory.palace import Palace

ROOT = Path(__file__).resolve().parent.parent


class _FakeVS:
    def add(self, *a, **k): pass
    def remove(self, *a, **k): pass


def _fm(mid: str) -> dict:
    return {
        "id": mid, "title": "工作偏好", "domain": "work",
        "confidence": "strong", "lifecycle": "active", "source_type": "memory",
        "access_count": 0, "created_at": "2026-08-19", "updated_at": "2026-08-19",
        "links": [], "entities": [], "created_by": "user_explicit",
        "verification_state": "direct", "freshness_state": "current",
        "is_important": True,
    }


def test_access_stats_same_session_counts_once(tmp_path: Path):
    async def scenario():
        data_dir = tmp_path / "data"
        (data_dir / "memories").mkdir(parents=True)
        db = Database(data_dir / "sp.db")
        db.run_migrations(ROOT / "migrations")
        palace = Palace(db)
        writer = FileWriter(db, palace, _FakeVS(), data_dir)
        lifecycle = LifecycleManager(db, palace, writer, data_dir, {})
        await writer.start()
        try:
            mid = "mem_000001"
            await writer.submit("memory", {
                "op": "create", "frontmatter": _fm(mid),
                "summary": "偏好直接沟通", "detail": "偏好直接沟通", "reason": "t"},
                wait=True)

            # 第一次引用：计数 +1；同时落 citation_events 作为去重锚点
            lifecycle.update_access_stats([mid], [mid], session_id="s1")
            lifecycle.record_citations([mid], message_id=1, session_id="s1")
            assert palace.get(mid)["access_count"] == 1

            # 同一会话再引用两次 → 计数不变
            lifecycle.update_access_stats([mid], [mid], session_id="s1")
            lifecycle.update_access_stats([mid], [mid], session_id="s1")
            assert palace.get(mid)["access_count"] == 1

            # 换会话 → 计数 +1
            lifecycle.update_access_stats([mid], [mid], session_id="s2")
            lifecycle.record_citations([mid], message_id=2, session_id="s2")
            assert palace.get(mid)["access_count"] == 2
        finally:
            await writer.stop(drain_timeout=5)
            db.close()

    asyncio.run(scenario())


def test_is_important_decays_after_no_access(tmp_path: Path):
    async def scenario():
        data_dir = tmp_path / "data"
        (data_dir / "memories").mkdir(parents=True)
        db = Database(data_dir / "sp.db")
        db.run_migrations(ROOT / "migrations")
        palace = Palace(db)
        writer = FileWriter(db, palace, _FakeVS(), data_dir)
        lifecycle = LifecycleManager(db, palace, writer, data_dir,
                                     {"important_memory_decay_days": 30})
        await writer.start()
        try:
            mid = "mem_000001"
            await writer.submit("memory", {
                "op": "create", "frontmatter": _fm(mid),
                "summary": "偏好直接沟通", "detail": "偏好直接沟通", "reason": "t"},
                wait=True)
            assert palace.get(mid)["is_important"] == 1

            # 手动把 last_accessed 拨到 60 天前
            db.execute(
                "UPDATE memories SET last_accessed='2020-01-01' WHERE id=?", (mid,))
            cleared = lifecycle.decay_is_important(days=30)
            # 排空 file_writer 队列
            await writer.drain()
            assert cleared == 1
            assert palace.get(mid)["is_important"] == 0
        finally:
            await writer.stop(drain_timeout=5)
            db.close()

    asyncio.run(scenario())
