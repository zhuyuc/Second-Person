"""SQLite migration 链契约测试。

保护契约：新增迁移必须可在干净库顺序重放；030_doc_dedup 提供的
raw_docs.content_hash 必须能被 031_backend_contract_indexes 的索引/触发器使用；
历史 NULL/空值不应阻塞迁移，非空哈希的新写入/更新必须去重。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from infrastructure.db import Database

ROOT = Path(__file__).resolve().parent.parent
_RAW_DOC_INSERT = (
    "INSERT INTO raw_docs(id,filename,file_path,file_size,imported_at,content_hash) "
    "VALUES(?,?,?,?,?,?)"
)


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "test.db")
    database.run_migrations(ROOT / "migrations")
    try:
        yield database
    finally:
        database.close()


def _insert_raw_doc(db: Database, doc_id: str, content_hash: str | None) -> None:
    db.execute(
        _RAW_DOC_INSERT,
        (doc_id, f"{doc_id}.txt", doc_id, 1, "2026-01-01T00:00:00",
         content_hash),
    )


def test_doc_dedup_and_backend_contract_migrations_replay_together(db: Database):
    cols = {r["name"] for r in db.query_all("PRAGMA table_info(raw_docs)")}
    assert "content_hash" in cols

    indexes = {r["name"] for r in db.query_all("PRAGMA index_list(raw_docs)")}
    assert "idx_raw_docs_content_hash" in indexes
    assert "idx_raw_docs_source_url" in indexes

    triggers = {r["name"] for r in db.query_all(
        "SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert "trg_raw_docs_content_hash_unique_insert" in triggers
    assert "trg_raw_docs_content_hash_unique_update" in triggers


def test_content_hash_trigger_allows_null_and_empty_history(db: Database):
    for doc_id, content_hash in (("doc_null_a", None), ("doc_null_b", None),
                                 ("doc_empty_a", ""), ("doc_empty_b", "")):
        _insert_raw_doc(db, doc_id, content_hash)

    count = db.query_one("SELECT COUNT(*) c FROM raw_docs")["c"]
    assert count == 4


def test_content_hash_trigger_rejects_duplicate_insert_and_update(db: Database):
    _insert_raw_doc(db, "doc_hash_a", "hash-a")
    _insert_raw_doc(db, "doc_hash_b", "hash-b")

    with pytest.raises(sqlite3.IntegrityError):
        _insert_raw_doc(db, "doc_hash_dup", "hash-a")

    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE raw_docs SET content_hash=? WHERE id=?",
                   ("hash-a", "doc_hash_b"))

    rows = db.query_all(
        "SELECT id, content_hash FROM raw_docs WHERE id LIKE 'doc_hash_%' "
        "ORDER BY id")
    assert [(r["id"], r["content_hash"]) for r in rows] == [
        ("doc_hash_a", "hash-a"),
        ("doc_hash_b", "hash-b"),
    ]
