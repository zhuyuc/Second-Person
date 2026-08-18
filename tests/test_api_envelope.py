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
from app.routes import chat


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


@pytest.mark.parametrize("sent, expected", [
    (None, "auto"),
    ("invalid", "auto"),
    ("quick", "quick"),
    ("deep", "deep"),
])
def test_chat_sse_normalizes_and_forwards_think_mode(
        client: TestClient, monkeypatch: pytest.MonkeyPatch,
        sent: str | None, expected: str):
    """auto 是默认路由控制，quick/deep 仅作为显式执行覆盖。"""
    captured = []
    crid = f"cr-{expected}-{sent}"

    async def fake_run(_sid, _message, _crid, **kwargs):
        captured.append(kwargs["think_mode"])
        yield {"event": "mode_decision", "data": {
            "requested_mode": kwargs["think_mode"],
            "effective_mode": "deep" if expected == "deep" else "quick",
            "reason": "测试路由",
        }}
        yield {"event": "turn_completed", "data": {"message_id": 1}}

    monkeypatch.setattr(get_container().core, "run", fake_run)
    body = {"session_id": "sse-mode-contract", "message": "测试", "client_request_id": crid}
    if sent is not None:
        body["think_mode"] = sent
    response = client.post("/api/chat/send", json=body)
    assert response.status_code == 200
    assert captured == [expected]
    assert "event: mode_decision" in response.text
    if expected == "deep":
        assert chat._BUFFERS[crid]["deep_requested"] is True
        assert chat._BUFFERS[crid]["deep_delivery"] is True


def test_active_buffer_is_not_cancelled_by_fixed_wall_clock_limit(monkeypatch: pytest.MonkeyPatch):
    cancelled = []

    class _Task:
        def done(self):
            return False

        def cancel(self):
            cancelled.append(True)

    monkeypatch.setattr(chat.time, "time", lambda: 9_999_999_999)
    chat._BUFFERS["active-long-task"] = {
        "done": False, "started": 0, "task": _Task(), "events": [], "size": 0,
    }
    chat._gc_buffers()
    assert not cancelled
    assert "active-long-task" in chat._BUFFERS
    chat._BUFFERS.pop("active-long-task", None)


def test_unconfigured_onboarding_chat_ends_with_friendly_sse_message(client: TestClient):
    """引导期没有模型时不得把 None 快照传给流式调用。"""
    container = get_container()
    container.config.set_raw("onboarding_completed", False)
    crid = "unconfigured-onboarding-chat"
    response = client.post("/api/chat/send", json={
        "session_id": "unconfigured-onboarding", "message": "你好",
        "client_request_id": crid,
    })
    assert response.status_code == 200
    assert "当前对话模型不可用，请在设置页检查模型配置。" in response.text
    assert "event: turn_completed" in response.text
