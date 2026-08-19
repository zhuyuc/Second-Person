"""记忆使用体验闭环集成测试。

覆盖：写入证据与版本、实际引用才更新使用、无关反馈进入治理队列。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from infrastructure.db import Database
from memory.file_writer import FileWriter
from memory.lifecycle import LifecycleManager
from memory.palace import Palace

ROOT = Path(__file__).resolve().parent.parent


class _FakeVS:
    def add(self, *args, **kwargs):
        pass

    def remove(self, *args, **kwargs):
        pass


def _frontmatter(memory_id: str) -> dict:
    return {
        "id": memory_id, "title": "用户的工作偏好", "domain": "work",
        "confidence": "medium", "lifecycle": "active", "source_type": "memory",
        "access_count": 0, "created_at": "2026-08-19", "updated_at": "2026-08-19",
        "links": [], "entities": [], "created_by": "user_explicit",
        "verification_state": "direct", "freshness_state": "current",
        "usefulness_score": 0, "valid_from": "2026-08-19",
        "review_after": "2020-01-01",
    }


def test_memory_experience_evidence_revision_feedback_loop(tmp_path: Path):
    async def scenario():
        data_dir = tmp_path / "data"
        (data_dir / "memories").mkdir(parents=True)
        db = Database(data_dir / "second-person.db")
        db.run_migrations(ROOT / "migrations")
        palace = Palace(db)
        writer = FileWriter(db, palace, _FakeVS(), data_dir)
        lifecycle = LifecycleManager(db, palace, writer, data_dir, {})
        await writer.start()
        try:
            mid = "mem_000001"
            fm = _frontmatter(mid)
            await writer.submit("memory", {
                "op": "create", "frontmatter": fm, "summary": "偏好直接的项目沟通",
                "detail": "用户明确要求项目沟通直截了当。", "entities": [],
                "reason": "用户主动保存",
                "evidence_refs": [{"source_type": "user_explicit",
                                   "source_ref": "session_1/message_1",
                                   "excerpt": "请记住我喜欢直接的项目沟通。"}],
            }, wait=True)
            row = palace.get(mid)
            assert row["verification_state"] == "direct"
            assert row["freshness_state"] == "current"
            assert db.query_one("SELECT COUNT(*) c FROM memory_evidence WHERE memory_id=?", (mid,))["c"] == 1
            assert db.query_one("SELECT COUNT(*) c FROM memory_revisions WHERE memory_id=?", (mid,))["c"] == 1

            # 被加载但没有正文引用时，不应刷新使用时间或访问次数。
            lifecycle.update_access_stats([mid], [])
            row = palace.get(mid)
            assert row["last_accessed"] is None
            assert row["access_count"] == 0

            # 用户标记无关后，下一次检索会获得负向权重，并进入治理队列。
            lifecycle.record_feedback(mid, "irrelevant", query_text="解释 Python 装饰器")
            row = palace.get(mid)
            assert row["retrieval_negative_count"] == 1
            item = db.query_one(
                "SELECT item_type,reason FROM memory_governance_items WHERE primary_memory_id=?",
                (mid,))
            assert item["item_type"] == "retrieval_irrelevant"
            assert "无关" in item["reason"]

            # 到达复核日期后，后台巡检会将记忆降为待复核并进入治理队列。
            assert mid in lifecycle.detect_review_due()
            assert await lifecycle.mark_review_due(mid) is True
            assert palace.get(mid)["freshness_state"] == "review_due"
            review_item = db.query_one(
                "SELECT item_type FROM memory_governance_items "
                "WHERE primary_memory_id=? AND item_type='freshness_review'", (mid,))
            assert review_item["item_type"] == "freshness_review"
        finally:
            await writer.stop(drain_timeout=5)
            db.close()

    asyncio.run(scenario())
