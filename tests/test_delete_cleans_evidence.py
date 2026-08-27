"""palace.delete_all_indexes 清 evidence/revisions/governance/candidates 契约。

用户"忘记我"必须真的忘：evidence excerpt / 版本快照 / 治理条目 / 候选池
的相关行都要一并清理。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from infrastructure.db import Database
from memory.file_writer import FileWriter
from memory.palace import Palace

ROOT = Path(__file__).resolve().parent.parent


class _FakeVS:
    def add(self, *a, **k): pass
    def remove(self, *a, **k): pass


def _fm(mid: str) -> dict:
    return {
        "id": mid, "title": "临时事实", "domain": "work",
        "confidence": "medium", "lifecycle": "active", "source_type": "memory",
        "access_count": 0, "created_at": "2026-08-19", "updated_at": "2026-08-19",
        "links": [], "entities": [], "created_by": "user_explicit",
        "verification_state": "direct", "freshness_state": "current",
    }


def test_delete_cleans_evidence_revisions_governance(tmp_path: Path):
    async def scenario():
        data_dir = tmp_path / "data"
        (data_dir / "memories").mkdir(parents=True)
        db = Database(data_dir / "sp.db")
        db.run_migrations(ROOT / "migrations")
        palace = Palace(db)
        writer = FileWriter(db, palace, _FakeVS(), data_dir)
        await writer.start()
        try:
            mid = "mem_000001"
            await writer.submit("memory", {
                "op": "create", "frontmatter": _fm(mid),
                "summary": "偏好项目沟通", "detail": "用户偏好直接沟通",
                "reason": "test",
                "evidence_refs": [{"source_type": "user_explicit",
                                   "excerpt": "喜欢直接沟通"}],
            }, wait=True)
            # 埋一条 governance 与 feedback
            db.execute(
                "INSERT INTO memory_governance_items(item_id,item_type,primary_memory_id,"
                "status,reason,created_at) VALUES('g1','test',?,'open','r','2026-01-01')",
                (mid,))
            db.execute(
                "INSERT INTO memory_feedback(memory_id,feedback_type,created_at) "
                "VALUES(?,?,?)", (mid, "irrelevant", "2026-01-01"))

            # 删除
            await writer.submit("memory", {"op": "delete", "memory_id": mid},
                                wait=True)

            # 四张表全部为空
            for table, where in (
                ("memory_evidence", "memory_id=?"),
                ("memory_revisions", "memory_id=?"),
                ("memory_governance_items", "primary_memory_id=?"),
                ("memory_feedback", "memory_id=?"),
            ):
                cnt = db.query_one(f"SELECT COUNT(*) c FROM {table} WHERE {where}",
                                    (mid,))["c"]
                assert cnt == 0, f"{table} 未清理，剩 {cnt} 行"
        finally:
            await writer.stop(drain_timeout=5)
            db.close()

    asyncio.run(scenario())
