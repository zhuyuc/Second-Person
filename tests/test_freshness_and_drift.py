"""P3-D freshness boost + P4-C fix_index_drift 保护治理字段。"""
from __future__ import annotations

import asyncio
from pathlib import Path

from infrastructure.db import Database
from memory.file_writer import FileWriter
from memory.lint import LintEngine
from memory.palace import Palace

ROOT = Path(__file__).resolve().parent.parent


class _FakeVS:
    loaded = False
    dim = None
    def add(self, *a, **k): pass
    def remove(self, *a, **k): pass
    def top_similar(self, *a, **k): return []


def _fm(mid, created_at="2026-08-19"):
    return {
        "id": mid, "title": "偏好", "domain": "work",
        "confidence": "medium", "lifecycle": "active", "source_type": "memory",
        "access_count": 0, "created_at": created_at, "updated_at": created_at,
        "links": [], "entities": [], "created_by": "user_explicit",
        "verification_state": "direct", "freshness_state": "current",
    }


def test_fix_index_drift_preserves_negative_count(tmp_path: Path):
    async def scenario():
        (tmp_path / "memories").mkdir(exist_ok=True)
        db = Database(tmp_path / "sp.db")
        db.run_migrations(ROOT / "migrations")
        palace = Palace(db)
        fw = FileWriter(db, palace, _FakeVS(), tmp_path)
        lint = LintEngine(db, palace, _FakeVS(), {})
        await fw.start()
        try:
            mid = "mem_000001"
            await fw.submit("memory", {
                "op": "create", "frontmatter": _fm(mid),
                "summary": "旧摘要", "detail": "d", "reason": "t"},
                wait=True)
            # 累计运行时反馈
            db.execute(
                "UPDATE memories SET retrieval_negative_count=3,"
                "access_count=5,is_important=1 WHERE id=?", (mid,))

            # 手动改 md 的 summary/title（模拟外部编辑 md）
            row = palace.get(mid)
            md_path = tmp_path / row["md_path"]
            content = md_path.read_text(encoding="utf-8")
            content = content.replace("旧摘要", "新摘要").replace("偏好", "新偏好", 1)
            md_path.write_text(content, encoding="utf-8")

            # 跑 drift 修复
            fixed = lint.fix_index_drift(palace, tmp_path)
            assert fixed == 1
            after = palace.get(mid)
            assert after["summary"] == "新摘要"
            # 治理字段不动
            assert after["retrieval_negative_count"] == 3
            assert after["access_count"] == 5
            assert after["is_important"] == 1
        finally:
            await fw.stop(drain_timeout=5)
            db.close()
    asyncio.run(scenario())
