"""memories_fts 触发器兜底同步验证（migration 049）。

场景：
1. UPDATE memories 的 title/summary/domain/project_id 时，即便调用方**忘了**
   调 palace.sync_fts，触发器也会自动更新 memories_fts，且不覆盖 detail。
2. DELETE memories 会级联清理 memories_fts 行。
3. 未变化字段的 UPDATE 不会误触发。
"""
from __future__ import annotations

from pathlib import Path

from infrastructure.db import Database
from memory.palace import Palace

ROOT = Path(__file__).resolve().parent.parent


def _mk_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "sp.db")
    db.run_migrations(ROOT / "migrations")
    return db


def _insert_memory(db: Database, mid: str, title: str, summary: str,
                   domain: str = "general", project_id: str | None = None,
                   detail: str = "") -> None:
    palace = Palace(db)
    with db.transaction() as conn:
        palace.upsert_index(conn, {
            "id": mid, "title": title, "domain": domain,
            "project_id": project_id,
        }, summary=summary, md_path=f"memories/{mid}.md")
        palace.sync_fts(conn, mid, title, summary, detail, domain, project_id)


def _fts_row(db: Database, mid: str) -> dict | None:
    row = db.query_one(
        "SELECT memory_id, project_id, title, summary, detail, domain "
        "FROM memories_fts WHERE memory_id=?", (mid,))
    return dict(row) if row else None


def test_update_title_triggers_fts_sync(tmp_path: Path):
    db = _mk_db(tmp_path)
    try:
        _insert_memory(db, "mem_001", "旧标题", "旧摘要",
                       detail="正文内容 - 不该被触发器覆盖")
        # 绕过 palace.sync_fts 直接改 memories
        db.execute("UPDATE memories SET title=? WHERE id=?",
                   ("新标题", "mem_001"))
        row = _fts_row(db, "mem_001")
        assert row is not None
        assert row["title"] == "新标题"
        # 关键：detail 保留（触发器只 UPDATE 展示字段）
        assert row["detail"] == "正文内容 - 不该被触发器覆盖"
    finally:
        db.close()


def test_update_summary_and_domain_and_project(tmp_path: Path):
    db = _mk_db(tmp_path)
    try:
        _insert_memory(db, "mem_002", "标题", "旧摘要", domain="work",
                       project_id=None, detail="d")
        db.execute(
            "UPDATE memories SET summary=?, domain=?, project_id=? WHERE id=?",
            ("新摘要", "life", "proj_x", "mem_002"))
        row = _fts_row(db, "mem_002")
        assert row["summary"] == "新摘要"
        assert row["domain"] == "life"
        assert row["project_id"] == "proj_x"
        assert row["detail"] == "d"
    finally:
        db.close()


def test_update_unrelated_column_no_op(tmp_path: Path):
    """更新非展示字段（如 lifecycle）不触发 fts UPDATE。"""
    db = _mk_db(tmp_path)
    try:
        _insert_memory(db, "mem_003", "T", "S", detail="X")
        db.execute("UPDATE memories SET lifecycle='stale' WHERE id=?",
                   ("mem_003",))
        row = _fts_row(db, "mem_003")
        assert row["title"] == "T"
        assert row["detail"] == "X"
    finally:
        db.close()


def test_delete_memory_cascades_fts(tmp_path: Path):
    db = _mk_db(tmp_path)
    try:
        _insert_memory(db, "mem_004", "T", "S")
        db.execute("DELETE FROM memories WHERE id=?", ("mem_004",))
        assert _fts_row(db, "mem_004") is None
    finally:
        db.close()


def test_null_summary_stored_as_empty_string(tmp_path: Path):
    """summary 为 NULL 时 fts 应存空字符串，避免匹配异常。"""
    db = _mk_db(tmp_path)
    try:
        _insert_memory(db, "mem_005", "T", "S")
        db.execute("UPDATE memories SET summary=NULL WHERE id=?",
                   ("mem_005",))
        row = _fts_row(db, "mem_005")
        assert row["summary"] == ""
    finally:
        db.close()
