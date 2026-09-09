"""会话 keyset 分页：稳定排序、无重复、无遗漏，并兼容旧页码接口。"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.session_context import SessionStore
from infrastructure.db import Database


ROOT = Path(__file__).resolve().parent.parent


def _store(tmp_path: Path) -> tuple[SessionStore, Database]:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db = Database(data_dir / "sp.db")
    db.run_migrations(ROOT / "migrations")
    return SessionStore(db, data_dir), db


def test_session_keyset_pagination_has_no_duplicate_or_gap(tmp_path):
    store, db = _store(tmp_path)
    try:
        expected = []
        for index in range(5):
            sid = store.create_session()
            db.execute(
                "UPDATE sessions SET last_active=?, pinned=? WHERE session_id=?",
                (f"2026-01-01T00:00:0{index}", 1 if index == 4 else 0, sid))
            expected.append(sid)

        first = store.list_sessions(page_size=2, cursor="")
        second = store.list_sessions(page_size=2, cursor=first["next_cursor"])
        third = store.list_sessions(page_size=2, cursor=second["next_cursor"])
        actual = [row["session_id"] for page in (first, second, third)
                  for row in page["list"]]

        assert first["total"] == 5
        assert len(actual) == 5
        assert len(set(actual)) == 5
        assert set(actual) == set(expected)
        assert third["next_cursor"] is None

        # 不传 cursor 时沿用旧响应形状和 page 语义。
        legacy = store.list_sessions(page=1, page_size=2)
        assert "next_cursor" not in legacy
        assert len(legacy["list"]) == 2
    finally:
        db.close()


def test_session_keyset_rejects_malformed_cursor(tmp_path):
    store, db = _store(tmp_path)
    try:
        with pytest.raises(ValueError, match="无效的会话分页游标"):
            store.list_sessions(page_size=2, cursor="not-a-cursor")
    finally:
        db.close()
