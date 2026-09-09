"""读密集型 async 路由必须调用 Database 的异步读入口。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.routes import memory as memory_routes


class _GraphDb:
    def __init__(self):
        self.calls: list[str] = []

    async def query_one_async(self, sql, params=()):
        self.calls.append(sql)
        assert "COUNT(*)" in sql
        return {"cnt": 1}

    async def query_all_async(self, sql, params=()):
        self.calls.append(sql)
        if "FROM memory_entities e" in sql:
            return [{"entity_id": "ent_1", "entity_name": "示例实体",
                     "entity_type": "concept", "primary_domain": "work",
                     "memory_count": 2, "x": 12, "y": 24}]
        return [{"src": "ent_1", "tgt": "ent_1", "w": 1}]

    def query_one(self, *args, **kwargs):  # pragma: no cover - contract guard
        raise AssertionError("图谱路由不得在事件循环调用同步 query_one")

    def query_all(self, *args, **kwargs):  # pragma: no cover - contract guard
        raise AssertionError("图谱路由不得在事件循环调用同步 query_all")


def test_graph_route_uses_async_database_reads(monkeypatch):
    db = _GraphDb()
    monkeypatch.setattr(memory_routes, "_c", lambda: SimpleNamespace(db=db))
    from memory import graph_layout
    monkeypatch.setattr(graph_layout, "place_missing", lambda _db: None)

    response = asyncio.run(memory_routes.graph(limit=20))

    assert response["data"]["total_count"] == 1
    assert response["data"]["nodes"][0]["entity_id"] == "ent_1"
    assert len(db.calls) == 3
