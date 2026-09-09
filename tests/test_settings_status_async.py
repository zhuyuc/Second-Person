"""设置状态页的数据库探测回归。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

# settings 路由含文件上传端点，FastAPI 在导入时需要该可选运行依赖。
pytest.importorskip("multipart")

from app.routes import settings as settings_routes


class _StatusDb:
    async def query_one_async(self, sql, params=()):
        if "FROM sessions" in sql:
            return {"c": 3}
        if "memories_fts" in sql:
            return {"c": 8}
        if "FROM memories" in sql:
            return {"c": 8}
        if "schema_migrations" in sql:
            return {"version": "051_prompt_cache_observability"}
        raise AssertionError(sql)

    def integrity_check(self):
        return True

    def foreign_key_check(self):
        return []


def test_status_route_reports_foreign_key_check_without_sync_reads(tmp_path, monkeypatch):
    class _Queue:
        def qsize(self):
            return 0

    c = SimpleNamespace(
        db=_StatusDb(),
        data_dir=tmp_path,
        palace=SimpleNamespace(stats=lambda: {"total": 8}),
        vs=SimpleNamespace(loaded=True, memory_mb=lambda: 1.0),
        bus=SimpleNamespace(subscriber_count=lambda: 1),
        fw=SimpleNamespace(_running=True, _queue=_Queue()),
        scheduler=SimpleNamespace(_running=True, list_tasks=lambda: []),
        config=SimpleNamespace(get=lambda *_args: "", get_raw=lambda *_args: ""),
    )
    monkeypatch.setattr(settings_routes, "_c", lambda: c)

    response = asyncio.run(settings_routes.status())

    fk = next(item for item in response["data"]["subsystems"]
              if item["name"] == "外键一致性")
    assert fk["status"] == "healthy"
    assert response["data"]["system_info"]["session_count"] == 3
