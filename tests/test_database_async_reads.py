"""异步 SQLite 读、慢读预算观测与外键巡检的回归测试。"""
from __future__ import annotations

import asyncio

from infrastructure.db import Database


def test_async_read_returns_rows_without_blocking_call_contract(tmp_path):
    db = Database(tmp_path / "reads.db")
    try:
        db.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        db.execute("INSERT INTO items(name) VALUES(?)", ("alpha",))

        async def read():
            one = await db.query_one_async(
                "SELECT name FROM items WHERE id=?", (1,), budget_ms=1)
            all_rows = await db.query_all_async(
                "SELECT id,name FROM items ORDER BY id", budget_ms=1)
            return one, all_rows

        one, all_rows = asyncio.run(read())
        assert one == {"name": "alpha"}
        assert all_rows == [{"id": 1, "name": "alpha"}]
    finally:
        db.close()


def test_foreign_key_check_detects_declared_constraint_orphans(tmp_path):
    db = Database(tmp_path / "foreign-keys.db")
    try:
        # 本项目当前保持 foreign_keys=OFF 以兼容存量应用层清理逻辑；
        # 巡检仍应能暴露后续已声明关系中的孤儿数据。
        db.execute("CREATE TABLE parents(id INTEGER PRIMARY KEY)")
        db.execute(
            "CREATE TABLE children(id INTEGER PRIMARY KEY, parent_id INTEGER "
            "REFERENCES parents(id))")
        db.execute("INSERT INTO children(id,parent_id) VALUES(1,999)")

        violations = db.foreign_key_check()
        assert len(violations) == 1
        assert violations[0]["table"] == "children"
        assert violations[0]["rowid"] == 1
    finally:
        db.close()
