"""火山方舟 Seedance 视频适配。"""
from __future__ import annotations

import asyncio
from pathlib import Path

from infrastructure.video_gen.profiles import CLOUD_PROFILE
from infrastructure.video_gen.vendor import volcengine_api_root


def _run(coro):
    return asyncio.run(coro)


def test_volcengine_api_root_normalizes_plan_and_bare_host():
    assert volcengine_api_root(
        "https://ark.cn-beijing.volces.com/api/v3"
    ) == "https://ark.cn-beijing.volces.com/api/v3"
    assert volcengine_api_root(
        "https://ark.cn-beijing.volces.com"
    ) == "https://ark.cn-beijing.volces.com/api/v3"
    assert volcengine_api_root(
        "https://ark.cn-beijing.volces.com/api/plan"
    ) == "https://ark.cn-beijing.volces.com/api/v3"
    assert volcengine_api_root(
        "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks"
    ) == "https://ark.cn-beijing.volces.com/api/v3"
    # 误粘贴「plan + 视频 tasks」时不得落成 /api/plan/v3
    assert volcengine_api_root(
        "https://ark.cn-beijing.volces.com/api/plan/v3/contents/generations/tasks"
    ) == "https://ark.cn-beijing.volces.com/api/v3"


def test_factory_routes_volcengine(tmp_path: Path):
    from infrastructure.video_gen.factory import get_video_adapter
    from infrastructure.video_gen.volcengine_adapter import VolcengineVideoAdapter

    class _Snap:
        provider_type = "volcengine"
        base_url = "https://ark.cn-beijing.volces.com/api/v3"
        api_key = "sk-ark"
        model_id = "doubao-seedance-1-0-pro-250528"

    adapter = get_video_adapter(_Snap(), None, tmp_path)
    assert isinstance(adapter, VolcengineVideoAdapter)


def test_volcengine_probe_treats_missing_task_as_reachable(tmp_path: Path, monkeypatch):
    import infrastructure.video_gen.volcengine_adapter as mod
    from infrastructure.video_gen.volcengine_adapter import VolcengineVideoAdapter

    seen = {}

    class _Resp:
        status_code = 404
        text = '{"error":{"code":"NotFound","message":"task not found"}}'

        def json(self):
            return {"error": {"code": "NotFound", "message": "task not found"}}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, params=None):
            seen["url"] = url
            seen["auth"] = (headers or {}).get("Authorization")
            return _Resp()

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    adapter = VolcengineVideoAdapter(
        base_url="https://ark.cn-beijing.volces.com/api/plan",
        api_key="sk-ark",
        data_dir=tmp_path,
        profile=CLOUD_PROFILE,
        model_id="doubao-seedance-1-0-pro-250528",
    )
    out = _run(adapter.probe())
    assert out["ok"] is True
    assert out["protocol"] == "volcengine"
    assert seen["url"].endswith("/api/v3/contents/generations/tasks/0")
    assert seen["auth"] == "Bearer sk-ark"


def test_volcengine_generate_uses_content_array(tmp_path: Path, monkeypatch):
    import infrastructure.video_gen.volcengine_adapter as mod
    from infrastructure.video_gen.types import VideoGenRequest
    from infrastructure.video_gen.volcengine_adapter import VolcengineVideoAdapter

    posted = {}
    polls = {"n": 0}

    class _Resp:
        def __init__(self, status_code=200, payload=None):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = ""

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            posted["url"] = url
            posted["json"] = json
            posted["headers"] = headers
            return _Resp(200, {"id": "cgt-test-1"})

    async def fake_get_json(client, url, headers=None, **kw):
        polls["n"] += 1
        if polls["n"] == 1:
            return {"id": "cgt-test-1", "status": "running"}
        return {
            "id": "cgt-test-1",
            "status": "succeeded",
            "content": {"video_url": "https://cdn.example/v.mp4"},
            "duration": 5,
        }

    async def fake_fetch(url, **kw):
        assert url == "https://cdn.example/v.mp4"
        return b"fake-mp4"

    async def fake_sleep(*a, **k):
        return None

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(mod, "http_get_json_resilient", fake_get_json)
    monkeypatch.setattr(mod, "fetch_url_bytes", fake_fetch)
    monkeypatch.setattr(mod, "sleep_or_cancel", fake_sleep)

    adapter = VolcengineVideoAdapter(
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key="sk-ark",
        data_dir=tmp_path,
        profile=CLOUD_PROFILE,
        model_id="doubao-seedance-1-0-pro-250528",
    )
    req = VideoGenRequest(
        prompt="小猫打哈欠",
        size="832x480",
        duration_sec=5,
        resolution="720p",
    )
    result = _run(adapter.generate(req, session_id="s1"))
    assert posted["url"].endswith("/contents/generations/tasks")
    body = posted["json"]
    assert body["model"] == "doubao-seedance-1-0-pro-250528"
    assert body["content"][0] == {"type": "text", "text": "小猫打哈欠"}
    assert "prompt" not in body
    assert body["duration"] == 5
    assert body["ratio"] == "16:9"
    assert result.filenames
    assert (tmp_path / "chat_videos" / result.filenames[0]).read_bytes() == b"fake-mp4"
    assert result.duration_sec == 5.0
