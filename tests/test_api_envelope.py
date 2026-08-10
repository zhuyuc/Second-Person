"""API envelope 契约测试。

保护契约：FastAPI 业务错误与参数校验错误统一返回
{code, message, trace_id, details}，成功响应返回 {code, data}。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.container import AppContainer
from app.main import create_app, get_container


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    async def _noop_startup(self):
        return None

    async def _noop_shutdown(self):
        return None

    # API envelope 测试只验证路由与异常处理器；容器 _build 已完成迁移与依赖装配，
    # startup/shutdown 中的 watcher/scheduler/外部连接器不参与本契约，显式置空避免真实后台副作用。
    monkeypatch.setattr(AppContainer, "startup", _noop_startup)
    monkeypatch.setattr(AppContainer, "shutdown", _noop_shutdown)

    app = create_app(tmp_path)
    with TestClient(app) as test_client:
        yield test_client
    get_container().db.close()


def _assert_error_envelope(resp, code: int, message: str | None = None) -> dict:
    assert resp.status_code == code
    body = resp.json()
    assert body["code"] == code
    if message is not None:
        assert body["message"] == message
    else:
        assert body["message"]
    assert "trace_id" in body
    assert body["trace_id"]
    assert "details" in body
    return body


def test_http_exception_uses_unified_error_envelope(client: TestClient):
    resp = client.post("/api/chat/session/handoff",
                       json={"from_session_id": ""})
    _assert_error_envelope(resp, 400, "缺少 from_session_id")


def test_validation_error_uses_unified_error_envelope(client: TestClient):
    resp = client.post("/api/chat/session/rename",
                       json={"session_id": "sid_only"})
    body = _assert_error_envelope(resp, 422, "请求参数校验失败")
    assert isinstance(body["details"], list)
    assert body["details"]


def test_success_response_keeps_code_data_envelope(client: TestClient):
    resp = client.post("/api/chat/session/create")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 200
    assert "data" in body
    assert body["data"]["session_id"]
