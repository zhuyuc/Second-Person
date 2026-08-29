"""API envelope 契约测试。

保护契约：FastAPI 业务错误与参数校验错误统一返回
{code, message, trace_id, details}，成功响应返回 {code, data}。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

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


def test_non_object_json_body_uses_unified_error_envelope(client: TestClient):
    resp = client.post("/api/onboarding/test-connection", json=[])
    _assert_error_envelope(resp, 400, "request body must be an object")


def test_success_response_keeps_code_data_envelope(client: TestClient):
    resp = client.post("/api/chat/session/create")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 200
    assert "data" in body
    assert body["data"]["session_id"]


def test_new_session_uses_new_chat_title_placeholder(client: TestClient):
    created = client.post("/api/chat/session/create").json()["data"]
    sessions = client.get("/api/chat/sessions?page_size=500").json()["data"]["list"]

    session = next(s for s in sessions if s["session_id"] == created["session_id"])

    assert session["title"] == "新对话"
    assert session["title_source"] == "auto"


def test_generated_title_replaces_new_chat_placeholder():
    class _Providers:
        def snapshot_for(self, _role):
            return {"model": "test-model"}

    class _Llm:
        async def chat(self, *_args, **_kwargs):
            return {"content": '{"title":"模型生成标题"}'}

    class _Sessions:
        def __init__(self):
            self.titles = []

        def set_auto_title(self, sid, title):
            self.titles.append((sid, title))

    sessions = _Sessions()
    container = SimpleNamespace(
        providers=_Providers(), llm=_Llm(), sessions=sessions)

    asyncio.run(chat._gen_title(container, "sess_title_test", "用户的原始提问"))

    assert sessions.titles == [("sess_title_test", "模型生成标题")]


@pytest.mark.parametrize("payload", [
    {"message": 123},
    {"message": "测试", "edit_message_id": "bad-id"},
    {"message": "测试", "images": {"name": "x.png"}},
])
def test_chat_send_rejects_invalid_request_fields(client: TestClient, payload: dict):
    resp = client.post("/api/chat/send", json=payload)
    _assert_error_envelope(resp, 400)


@pytest.mark.parametrize("payload, expected", [
    ({}, "high"),
    ({"reasoning_effort": "off"}, "off"),
    ({"reasoning_effort": "max"}, "max"),
])
def test_chat_sse_forwards_reasoning_effort(
        client: TestClient, monkeypatch: pytest.MonkeyPatch,
        payload, expected: str):
    captured = []
    crid = f"cr-{expected}-{id(payload)}"

    async def fake_run(_sid, _message, _crid, **kwargs):
        captured.append(kwargs["reasoning_effort"])
        yield {"event": "turn_started", "data": {
            "turn_id": "turn_test", "reasoning_effort": kwargs["reasoning_effort"],
        }}
        yield {"event": "turn_completed", "data": {"message_id": 1, "turn_id": "turn_test"}}

    monkeypatch.setattr(get_container().core, "run", fake_run)
    body = {"session_id": "sse-mode-contract", "message": "测试", "client_request_id": crid}
    body.update(payload)
    response = client.post("/api/chat/send", json=body)
    assert response.status_code == 200
    assert captured == [expected]
    assert "event: turn_started" in response.text
    assert chat._BUFFERS[crid]["reasoning_effort"] == expected


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
    assert "event: error" in response.text
