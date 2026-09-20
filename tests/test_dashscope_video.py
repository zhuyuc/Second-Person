"""百炼视频与可灵分流。"""
from __future__ import annotations

import asyncio
from pathlib import Path

from infrastructure.video_gen.profiles import CLOUD_PROFILE
from infrastructure.video_gen.vendor import dashscope_api_root


def _run(coro):
    return asyncio.run(coro)


def test_dashscope_api_root_strips_compatible_mode():
    assert dashscope_api_root(
        "https://dashscope.aliyuncs.com/compatible-mode/v1"
    ) == "https://dashscope.aliyuncs.com/api/v1"


def test_factory_uses_protocol_not_url(tmp_path: Path):
    from infrastructure.video_gen.dashscope_adapter import DashScopeVideoAdapter
    from infrastructure.video_gen.factory import get_video_adapter
    from infrastructure.video_gen.cloud_adapter import KlingVideoAdapter

    class _Snap:
        def __init__(self, provider_type, base_url):
            self.provider_type = provider_type
            self.base_url = base_url
            self.api_key = "sk-ali"
            self.model_id = "wan2.6-t2v"

    ali_url = "https://dashscope.aliyuncs.com/api/v1"
    dash = get_video_adapter(_Snap("dashscope", ali_url), None, tmp_path)
    assert isinstance(dash, DashScopeVideoAdapter)
    kling = get_video_adapter(_Snap("kling", ali_url), None, tmp_path)
    assert isinstance(kling, KlingVideoAdapter)


def test_dashscope_probe_treats_missing_task_as_reachable(tmp_path: Path, monkeypatch):
    import infrastructure.video_gen.dashscope_adapter as mod
    from infrastructure.video_gen.dashscope_adapter import DashScopeVideoAdapter

    seen = {}

    class _Resp:
        status_code = 400
        text = '{"code":"InvalidParameter","message":"task can not be found"}'

        def json(self):
            return {"code": "InvalidParameter", "message": "task can not be found"}

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
    adapter = DashScopeVideoAdapter(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="sk-ali",
        data_dir=tmp_path,
        profile=CLOUD_PROFILE,
        model_id="wan2.6-t2v",
    )
    out = _run(adapter.probe())
    assert out["ok"] is True
    assert out["protocol"] == "dashscope"
    assert seen["url"] == "https://dashscope.aliyuncs.com/api/v1/tasks/0"
    assert seen["auth"] == "Bearer sk-ali"
    assert "task_ids" not in seen["url"]


def test_dashscope_generate_uses_video_synthesis(tmp_path: Path, monkeypatch):
    import infrastructure.video_gen.dashscope_adapter as mod
    from infrastructure.video_gen.dashscope_adapter import DashScopeVideoAdapter
    from infrastructure.video_gen.types import VideoGenRequest

    posted = {}

    class _Resp:
        def __init__(self, status_code=200, payload=None, content=b""):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = ""
            self.content = content

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
            posted["body"] = json
            posted["async"] = (headers or {}).get("X-DashScope-Async")
            return _Resp(200, {"output": {
                "task_id": "task-ali-1", "task_status": "PENDING"}})

        async def get(self, url, headers=None, params=None):
            posted["poll"] = url
            return _Resp(200, {
                "output": {
                    "task_status": "SUCCEEDED",
                    "video_url": "https://cdn.example/a.mp4",
                },
                "usage": {"duration": 5},
            })

    async def _bytes(*a, **k):
        return b"mp4-bytes"

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(mod, "fetch_url_bytes", _bytes)
    monkeypatch.setattr(mod, "get_store", lambda: None)
    adapter = DashScopeVideoAdapter(
        base_url="https://dashscope.aliyuncs.com/api/v1",
        api_key="sk-ali",
        data_dir=tmp_path,
        profile=CLOUD_PROFILE,
        model_id="wan2.6-t2v",
    )
    result = _run(adapter.generate(
        VideoGenRequest(prompt="雨夜街头", size="16:9", duration_sec=5, resolution="720p"),
        provider_id="p-ali",
        model_id="wan2.6-t2v",
        session_id="sess-ali",
    ))
    assert posted["url"].endswith("/services/aigc/video-generation/video-synthesis")
    assert posted["body"]["model"] == "wan2.6-t2v"
    assert posted["body"]["parameters"]["size"] == "1280*720"
    assert posted["async"] == "enable"
    assert posted["poll"].endswith("/tasks/task-ali-1")
    assert result.filenames
    assert (tmp_path / "chat_videos" / result.filenames[0]).read_bytes() == b"mp4-bytes"


def test_custom_video_probe_treats_get_400_as_reachable(tmp_path: Path, monkeypatch):
    import infrastructure.video_gen.custom_adapter as mod
    from infrastructure.video_gen.custom_adapter import CustomVideoAdapter

    seen = {}

    class _Resp:
        status_code = 400
        text = '{"code":"InvalidParameter","message":"method not supported"}'

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            seen["url"] = url
            return _Resp()

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    adapter = CustomVideoAdapter(
        base_url="https://example.com/v1/videos/submit", api_key="sk",
        data_dir=tmp_path, profile=CLOUD_PROFILE, model_id="x")
    out = _run(adapter.probe())
    assert out["ok"] is True
    assert seen["url"] == "https://example.com/v1/videos/submit"


def test_custom_video_retries_when_sync_is_rejected(tmp_path: Path, monkeypatch):
    import infrastructure.video_gen.custom_adapter as mod
    from infrastructure.video_gen.custom_adapter import CustomVideoAdapter
    from infrastructure.video_gen.types import VideoGenRequest

    raw = "https://dashscope.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis"
    posts = []
    polls = []

    class _Resp:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload
            self.text = str(payload.get("message") or "")

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
            posts.append({"url": url, "json": json, "headers": headers or {}})
            if len(posts) == 1:
                return _Resp(403, {
                    "message": "current user api does not support synchronous calls",
                })
            return _Resp(200, {"output": {"task_id": "task-1"}})

    async def _poll(client, url, **k):
        polls.append({"url": url, "headers": k.get("headers") or {}})
        return {"output": {
            "task_status": "SUCCEEDED",
            "video_url": "https://cdn.example/v.mp4",
        }}

    async def _bytes(*a, **k):
        return b"mp4"

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(mod, "http_get_json_resilient", _poll)
    monkeypatch.setattr(mod, "fetch_url_bytes", _bytes)
    monkeypatch.setattr(mod, "get_store", lambda: None)
    adapter = CustomVideoAdapter(
        base_url=raw, api_key="sk", data_dir=tmp_path,
        profile=CLOUD_PROFILE, model_id="wan2.6-t2v")
    result = _run(adapter.generate(
        VideoGenRequest(prompt="雨夜", duration_sec=5, size="832x480"),
        session_id="s"))
    assert posts[0]["url"] == raw
    assert "X-DashScope-Async" not in posts[0]["headers"]
    assert posts[1]["url"] == raw
    assert posts[1]["headers"]["X-DashScope-Async"] == "enable"
    assert posts[1]["json"]["input"]["prompt"] == "雨夜"
    assert polls[0]["url"] == "https://dashscope.aliyuncs.com/api/v1/tasks/task-1"
    assert polls[0]["headers"] == {"Authorization": "Bearer sk"}
    assert "X-DashScope-Async" not in polls[0]["headers"]
    assert result.filenames


def test_custom_video_posts_to_the_url_as_written(tmp_path: Path, monkeypatch):
    import infrastructure.video_gen.custom_adapter as mod
    from infrastructure.video_gen.custom_adapter import CustomVideoAdapter
    from infrastructure.video_gen.types import VideoGenRequest

    seen = {}
    raw = "https://example.com/v1/videos/submit"

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"video_url": "https://cdn.example/v.mp4"}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            seen["url"] = url
            seen["body"] = json
            return _Resp()

    async def _bytes(*a, **k):
        return b"mp4"

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(mod, "fetch_url_bytes", _bytes)
    monkeypatch.setattr(mod, "get_store", lambda: None)
    adapter = CustomVideoAdapter(
        base_url=raw, api_key="sk", data_dir=tmp_path,
        profile=CLOUD_PROFILE, model_id="my-video")
    _run(adapter.generate(
        VideoGenRequest(prompt="猫", duration_sec=5), session_id="s"))
    assert seen["url"] == raw
    assert "text-to-video" not in seen["url"]
    assert "video-synthesis" not in seen["url"]


def test_openai_video_appends_videos_not_vendor_paths(tmp_path: Path, monkeypatch):
    import infrastructure.video_gen.openai_adapter as mod
    from infrastructure.video_gen.factory import get_video_adapter
    from infrastructure.video_gen.openai_adapter import OpenAIVideoAdapter
    from infrastructure.video_gen.types import VideoGenRequest

    ali = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    class _Snap:
        def __init__(self, provider_type, base_url):
            self.provider_type = provider_type
            self.base_url = base_url
            self.api_key = "sk"
            self.model_id = "sora-2"

    picked = get_video_adapter(_Snap("openai_compatible", ali), None, tmp_path)
    assert isinstance(picked, OpenAIVideoAdapter)
    seen = {}

    class _Resp:
        status_code = 200
        text = ""
        content = b"mp4"

        def json(self):
            return {"id": "vid_1", "status": "completed"}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            seen["url"] = url
            return _Resp()

        async def get(self, url, headers=None):
            seen["get"] = url
            return _Resp()

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(mod, "get_store", lambda: None)
    _run(picked.generate(
        VideoGenRequest(prompt="猫", duration_sec=4), session_id="s"))
    assert seen["url"] == ali + "/videos"
    assert "text-to-video" not in seen["url"]
    assert "video-synthesis" not in seen["url"]
    assert seen["get"] == ali + "/videos/vid_1/content"
