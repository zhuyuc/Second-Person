"""时间线语义事件契约：FileWriter 写入 timeline_event 必须落库。"""
from __future__ import annotations

import asyncio
from pathlib import Path

from infrastructure.db import Database
from memory.file_writer import FileWriter
from memory.palace import Palace

ROOT = Path(__file__).resolve().parent.parent


class _FakeVS:
    def add(self, *a, **kw):
        pass

    def remove(self, *a, **kw):
        pass


def _fm(mid: str) -> dict:
    return {
        "id": mid, "title": "测试记忆", "domain": "general",
        "confidence": "medium", "lifecycle": "active",
        "source_type": "memory", "access_count": 0,
        "created_at": "2026-01-01", "updated_at": "2026-01-01",
        "links": [], "entities": [], "created_by": "test",
    }


def test_timeline_event_semantics(tmp_path: Path):
    """create/update 缺省与显式 timeline_event 均按契约落库。"""
    async def scenario():
        (tmp_path / "memories").mkdir(parents=True)
        db = Database(tmp_path / "test.db")
        db.run_migrations(ROOT / "migrations")
        palace = Palace(db)
        fw = FileWriter(db, palace, _FakeVS(), tmp_path)
        await fw.start()
        try:
            cases = [
                ("mem-t1", "create", None, "created"),
                ("mem-t2", "create", "imported", "imported"),
                ("mem-t1", "update", None, "updated"),
                ("mem-t1", "update", "evolved", "evolved"),
                ("mem-t1", "update", "merged", "merged"),
            ]
            for mid, op, evt, expect in cases:
                payload = {
                    "op": op, "memory_id": mid, "frontmatter": _fm(mid),
                    "summary": "摘要", "detail": f"详情 {op} {evt}",
                    "reason": f"test-{expect}",
                }
                if evt:
                    payload["timeline_event"] = evt
                await fw.submit("memory", payload, wait=True)
                row = db.query_one(
                    "SELECT 1 FROM memory_timeline WHERE memory_id=? "
                    "AND event_type=? AND detail=?",
                    (mid, expect, f"test-{expect}"),
                )
                assert row, f"op={op} timeline_event={evt} → 期望 {expect}"
        finally:
            await fw.stop(drain_timeout=5)
            db.close()

    asyncio.run(scenario())
