"""文生视频 generate_video：tool_visual、落盘、槽位、图/视频互斥。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent.turn_runtime import TurnRuntime
from infrastructure.db import Database
from infrastructure.video_gen import (
    ALLOWED_SIZES,
    ComfyUIVideoAdapter,
    VideoGenRequest,
    clamp_duration_sec,
    normalize_size,
)
from tools.base import ToolRegistry, ToolSpec

ROOT = Path(__file__).resolve().parent.parent

# 极小可识别的伪 MP4 字节（含 ftyp 头），用于 mock 落盘验收
_MINI_MP4 = (
    b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
    b"\x00\x00\x00\x08free"
    b"\x00\x00\x00\x08mdat"
)

_GEN_RESULT = {
    "type": "generated_video",
    "filenames": ["genv_testhash12.mp4"],
    "public_urls": ["/chat-videos/genv_testhash12.mp4"],
    "n": 1,
    "size": "480x832",
    "duration_sec": 3,
    "fps": 16,
    "provider_id": "prov_vid",
    "model_id": "wan2.1_t2v_1.3B_fp16.safetensors",
    "backend": "comfyui",
    "summary": "已本地生成 1 条短视频（Wan，约 3s，耗时 1s）",
}

_IMG_RESULT = {
    "type": "generated_image",
    "filenames": ["gen_testhash12.png"],
    "public_urls": ["/chat-images/gen_testhash12.png"],
    "n": 1,
    "size": "1024x1024",
    "provider_id": "prov_img",
    "model_id": "sd_xl_base_1.0.safetensors",
    "backend": "comfyui",
    "summary": "已本地生成 1 张图",
}


class _Config:
    def __init__(self, **v):
        self.values = v

    def get(self, k, default=None):
        return self.values.get(k, default)

    def get_raw(self, k, default=None):
        return self.values.get(k, default)


class _Sessions:
    def __init__(self):
        self.messages: list[dict] = []

    def append_message(self, _sid, role, content, **kw):
        self.messages.append({"role": role, "content": content, **kw})
        return len(self.messages)


class _Provider:
    model_id = "test-model"
    context_window = 10000


class _Providers:
    def snapshot_for(self, _slot):
        return _Provider()


class _VideoExecutor:
    async def execute_tool(self, name, params, **_kw):
        if name == "generate_video":
            return {"ok": True, "result": _GEN_RESULT}
        if name == "generate_image":
            return {"ok": True, "result": _IMG_RESULT}
        return {"ok": True, "result": {"summary": f"{name} 结果"}}


class _VideoLLM:
    def __init__(self):
        self.n = 0

    async def stream_chat(self, _snap, _messages, **_kw):
        self.n += 1
        if self.n == 1:
            yield "done", {
                "content": "",
                "tool_calls": [{
                    "id": "c1",
                    "type": "function",
                    "function": {
                        "name": "generate_video",
                        "arguments": '{"prompt":"an orange cat on a windowsill, gentle breeze"}',
                    },
                }],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
        else:
            yield "content", "视频已生成"
            yield "done", {
                "content": "视频已生成",
                "tool_calls": [],
                "usage": {"input_tokens": 5, "output_tokens": 2},
            }


def _run(coro):
    # 勿用 new_event_loop().run_until_complete：在 TestClient/anyio 之后会挂死
    return asyncio.run(coro)


def _build_runtime(db, executor=None, llm=None):
    registry = ToolRegistry()
    registry.register_function(ToolSpec(
        "generate_video", "本地生视频",
        {"type": "object", "properties": {"prompt": {"type": "string"}},
         "required": ["prompt"]}, parallel_safe=False), lambda **_: None)
    registry.register_function(ToolSpec(
        "generate_image", "本地生图",
        {"type": "object", "properties": {"prompt": {"type": "string"}},
         "required": ["prompt"]}, parallel_safe=False), lambda **_: None)
    sessions = _Sessions()

    async def context_loader(**_kw):
        return {"snap": _Provider(), "history": [],
                "history_ids": [], "memory_count": 0}

    runtime = TurnRuntime(
        db=db, config=_Config(agent_max_steps=3),
        sessions=sessions, registry=registry,
        executor=executor or _VideoExecutor(),
        llm=llm or _VideoLLM(),
        providers=_Providers(),
        system_prompt=lambda *_a: "sys",
        context_loader=context_loader)
    return runtime, sessions


def test_generate_video_tool_visual_emitted_and_persisted(tmp_path: Path):
    async def scenario():
        db = Database(tmp_path / "v.db")
        db.run_migrations(ROOT / "migrations")
        try:
            events: list[tuple[str, dict]] = []

            async def emit(name, data):
                events.append((name, data))

            runtime, sessions = _build_runtime(db)
            await runtime.run(session_id="s", message="生成一段橘猫短视频",
                              reasoning_effort="high", emit=emit)

            visual_events = [d for (n, d) in events if n == "tool_visual"]
            assert len(visual_events) == 1, "generate_video 成功后应发射一次 tool_visual"
            ve = visual_events[0]
            assert ve["type"] == "generated_video"
            assert ve["data"]["filenames"][0].startswith("genv_")

            assistant = [m for m in sessions.messages if m["role"] == "assistant"][-1]
            visuals = assistant.get("visuals")
            assert visuals, "assistant 消息应持久化 visuals"
            assert visuals[0]["type"] == "generated_video"
            assert visuals[0]["data"]["public_urls"][0].startswith("/chat-videos/")
        finally:
            db.close()

    _run(scenario())


def test_comfyui_video_adapter_persist_with_mock_http(tmp_path: Path, monkeypatch):
    import infrastructure.video_gen.comfyui_adapter as mod

    wf = ROOT / "workflows" / "wan21_t2v_1_3b.json"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    adapter = ComfyUIVideoAdapter(
        base_url="http://127.0.0.1:9",
        workflow_path=wf,
        data_dir=data_dir,
        timeout_sec=5,
    )

    class _Resp:
        def __init__(self, status_code=200, payload=None, content=b""):
            self.status_code = status_code
            self._payload = payload
            self.content = content
            self.text = json.dumps(payload or {})

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            assert url.endswith("/prompt")
            # 校验工作流已被注入尺寸/帧数
            prompt = (json or {}).get("prompt") or {}
            latent = prompt.get("13") or {}
            assert latent.get("inputs", {}).get("width") == 480
            assert latent.get("inputs", {}).get("height") == 832
            assert latent.get("inputs", {}).get("length") == 49  # 3s * 16fps + 奇
            return _Resp(200, {"prompt_id": "pidv1"})

        async def get(self, url, params=None):
            if "/history/" in url:
                return _Resp(200, {
                    "pidv1": {
                        "status": {"completed": True, "status_str": "success"},
                        "outputs": {
                            "20": {"videos": [{
                                "filename": "sp_genv_00001.mp4",
                                "subfolder": "video",
                                "type": "output",
                            }]},
                        },
                    },
                })
            if url.endswith("/view"):
                return _Resp(200, content=_MINI_MP4)
            return _Resp(404)

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    result = _run(adapter.generate(
        VideoGenRequest(prompt="an orange cat", duration_sec=3, fps=16),
        provider_id="p1",
        model_id="wan2.1_t2v_1.3B_fp16.safetensors",
    ))
    assert result.n == 1
    assert result.type == "generated_video"
    assert result.filenames[0].startswith("genv_")
    assert result.filenames[0].endswith(".mp4")
    out = data_dir / "chat_videos" / result.filenames[0]
    assert out.exists()
    assert out.read_bytes().startswith(b"\x00\x00\x00\x18ftyp")
    assert result.public_urls[0] == f"/chat-videos/{result.filenames[0]}"
    assert result.duration_sec == 3


def test_video_gen_slot_registered():
    from infrastructure.provider_registry import TASK_SLOTS
    assert "video_gen" in TASK_SLOTS
    assert TASK_SLOTS["video_gen"].fallback == ()


def test_ensure_video_gen_assignment_creates_provider(tmp_path: Path):
    """启动补齐：无 video_gen 绑定时自动创建 Wan ComfyUI Provider 并绑定。"""
    from connectors.credential_store import CredentialStore
    from infrastructure.provider_registry import (
        ProviderRegistry, ensure_slot_assignments)

    db = Database(tmp_path / "slots.db")
    db.run_migrations(ROOT / "migrations")
    try:
        creds = CredentialStore(db, tmp_path)
        reg = ProviderRegistry(db, creds)
        # 先有文生图 ComfyUI，供复用 base_url
        reg.add_provider(
            pid="prov_001",
            display_name="ComfyUI SDXL",
            provider_type="comfyui",
            base_url="http://127.0.0.1:8188",
            model_id="sd_xl_base_1.0.safetensors",
            api_key="local",
            input_price=None,
            output_price=None,
            context_window=0,
        )
        reg.set_assignment("image_gen", "prov_001")
        assert reg.assignment("video_gen") is None
        filled = ensure_slot_assignments(reg)
        assert "video_gen" in filled
        vid_pid = reg.assignment("video_gen")
        assert vid_pid
        snap = reg.snapshot(vid_pid)
        assert snap is not None
        assert snap.provider_type == "comfyui"
        assert "wan" in snap.model_id.lower()
        assert snap.base_url.rstrip("/") == "http://127.0.0.1:8188"
        # 再次 ensure 不覆盖
        filled2 = ensure_slot_assignments(reg)
        assert "video_gen" not in filled2
        assert reg.assignment("video_gen") == vid_pid
    finally:
        db.close()


def test_turn_blocks_second_generate_video(tmp_path: Path):
    from agent.turn_runtime_tools import TurnToolRunner
    from agent.turn_events import TurnEventStore
    from agent.repeat_tool_guard import RepeatToolGuard

    db = Database(tmp_path / "t.db")
    db.run_migrations(ROOT / "migrations")
    events_store = TurnEventStore(db)
    turn = events_store.start_turn("s1", reasoning_effort="high", max_steps=3)
    turn_id = turn["id"]
    calls = {"n": 0}

    class _Exec:
        async def execute_tool(self, name, params, **kw):
            calls["n"] += 1
            return {"ok": True, "result": _GEN_RESULT}

    registry = ToolRegistry()
    registry.register_function(ToolSpec(
        "generate_video", "g",
        {"type": "object", "properties": {"prompt": {"type": "string"}},
         "required": ["prompt"]}, parallel_safe=False), lambda **_: None)
    runner = TurnToolRunner(registry=registry, executor=_Exec(), events=events_store)
    emitted = []

    async def emit(n, d):
        emitted.append((n, d))

    guard = RepeatToolGuard()

    async def scenario():
        r1 = await runner.run_tool_calls(turn_id, 1, [{
            "id": "c1", "type": "function",
            "function": {"name": "generate_video",
                         "arguments": '{"prompt":"a"}'},
        }], emit, guard)
        assert r1[0]["ok"] is True
        r2 = await runner.run_tool_calls(turn_id, 2, [{
            "id": "c2", "type": "function",
            "function": {"name": "generate_video",
                         "arguments": '{"prompt":"b"}'},
        }], emit, guard)
        assert r2[0]["ok"] is False
        assert "本轮已成功生成一条视频" in (r2[0].get("error") or "")
        assert calls["n"] == 1

    _run(scenario())
    db.close()


def test_turn_mutex_image_then_video(tmp_path: Path):
    """同轮先成功出图后再 generate_video 应被互斥拦截。"""
    from agent.turn_runtime_tools import TurnToolRunner
    from agent.turn_events import TurnEventStore
    from agent.repeat_tool_guard import RepeatToolGuard

    db = Database(tmp_path / "t2.db")
    db.run_migrations(ROOT / "migrations")
    events_store = TurnEventStore(db)
    turn = events_store.start_turn("s2", reasoning_effort="high", max_steps=3)
    turn_id = turn["id"]
    calls = {"n": 0}

    class _Exec:
        async def execute_tool(self, name, params, **kw):
            calls["n"] += 1
            if name == "generate_image":
                return {"ok": True, "result": _IMG_RESULT}
            return {"ok": True, "result": _GEN_RESULT}

    registry = ToolRegistry()
    for name in ("generate_image", "generate_video"):
        registry.register_function(ToolSpec(
            name, name,
            {"type": "object", "properties": {"prompt": {"type": "string"}},
             "required": ["prompt"]}, parallel_safe=False), lambda **_: None)
    runner = TurnToolRunner(registry=registry, executor=_Exec(), events=events_store)

    async def emit(_n, _d):
        return None

    guard = RepeatToolGuard()

    async def scenario():
        r1 = await runner.run_tool_calls(turn_id, 1, [{
            "id": "c1", "type": "function",
            "function": {"name": "generate_image",
                         "arguments": '{"prompt":"a"}'},
        }], emit, guard)
        assert r1[0]["ok"] is True
        r2 = await runner.run_tool_calls(turn_id, 2, [{
            "id": "c2", "type": "function",
            "function": {"name": "generate_video",
                         "arguments": '{"prompt":"b"}'},
        }], emit, guard)
        assert r2[0]["ok"] is False
        assert "同轮图/视频互斥" in (r2[0].get("error") or "")
        assert calls["n"] == 1

    _run(scenario())
    db.close()


def test_allowed_sizes_and_duration_clamp():
    assert ALLOWED_SIZES == ("480x832", "832x480")
    assert normalize_size("1920x1080") == "480x832"
    assert normalize_size("832x480") == "832x480"
    d, clamped = clamp_duration_sec(10)
    assert d == 4 and clamped is True
    d2, c2 = clamp_duration_sec(3)
    assert d2 == 3 and c2 is False


def test_auto_generate_video_file_via_adapter(tmp_path: Path, monkeypatch):
    """端到端：Adapter 自动提交→轮询→落盘，产出可识别的视频文件。"""
    import infrastructure.video_gen.comfyui_adapter as mod

    wf = ROOT / "workflows" / "wan21_t2v_1_3b.json"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    adapter = ComfyUIVideoAdapter(
        base_url="http://127.0.0.1:9",
        workflow_path=wf,
        data_dir=data_dir,
        timeout_sec=5,
        fps=16,
    )

    class _Resp:
        def __init__(self, status_code=200, payload=None, content=b""):
            self.status_code = status_code
            self._payload = payload
            self.content = content
            self.text = json.dumps(payload or {})

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *a, **k):
            self._polls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            return _Resp(200, {"prompt_id": "auto1"})

        async def get(self, url, params=None):
            if "/history/" in url:
                self._polls += 1
                if self._polls < 2:
                    return _Resp(200, {"auto1": {"status": {}, "outputs": {}}})
                return _Resp(200, {
                    "auto1": {
                        "status": {"completed": True},
                        "outputs": {
                            "20": {"videos": [{
                                "filename": "auto.mp4",
                                "subfolder": "",
                                "type": "output",
                            }]},
                        },
                    },
                })
            return _Resp(200, content=_MINI_MP4)

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    result = _run(adapter.generate(
        VideoGenRequest(prompt="cat stretches on windowsill", size="832x480",
                        duration_sec=2, fps=16),
        provider_id="auto",
        model_id="wan2.1_t2v_1.3B_fp16.safetensors",
        session_id="sess-auto",
    ))
    path = data_dir / "chat_videos" / result.filenames[0]
    assert path.exists()
    assert path.stat().st_size >= len(_MINI_MP4)
    assert result.type == "generated_video"
    assert result.size == "832x480"
    assert result.duration_sec == 2


def test_kling_jwt_auth_header():
    from infrastructure.video_gen.kling_auth import kling_auth_header
    jwt_h = kling_auth_header("ak_test:sk_secret")
    assert jwt_h["Authorization"].startswith("Bearer eyJ")
    bearer = kling_auth_header("sk-openai-key")
    assert bearer["Authorization"] == "Bearer sk-openai-key"
    new_h = kling_auth_header("ak_test:sk_secret", model_id="kling-3.0-turbo")
    assert new_h["Authorization"] == "Bearer ak_test:sk_secret"
    old_h = kling_auth_header("ak_test:sk_secret", model_id="kling-v2-6")
    assert old_h["Authorization"].startswith("Bearer eyJ")
    api_key = kling_auth_header("kling-console-key", model_id="kling-3.0-turbo")
    assert api_key["Authorization"] == "Bearer kling-console-key"


def test_video_factory_picks_cloud_adapter(tmp_path: Path):
    from infrastructure.video_gen import KlingVideoAdapter, get_video_adapter

    class _Snap:
        base_url = "https://api.kling.example"
        api_key = "ak:sk"
        model_id = "kling-v3-turbo"

        def __init__(self, provider_type):
            self.provider_type = provider_type

    for ptype in ("openai_compatible", "anthropic", "custom"):
        adapter = get_video_adapter(_Snap(ptype), _Config(), tmp_path)
        assert isinstance(adapter, KlingVideoAdapter)


def test_kling_probe_explains_beijing_https_failure(tmp_path: Path, monkeypatch):
    import httpx
    import infrastructure.video_gen.cloud_adapter as mod
    from infrastructure.video_gen import KlingVideoAdapter
    from infrastructure.video_gen.profiles import CLOUD_PROFILE

    adapter = KlingVideoAdapter(
        base_url="https://api-beijing.klingai.com",
        api_key="kling-console-key",
        data_dir=tmp_path,
        profile=CLOUD_PROFILE,
        model_id="kling-3.0-turbo",
    )

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, params=None):
            raise httpx.ConnectError("[SSL: WRONG_VERSION_NUMBER] wrong version number")

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    out = _run(adapter.probe())
    assert out["ok"] is False
    assert "无法连接云端视频服务" in out["error"]
    assert "HTTPS" in out["error"]
    assert "api-beijing.klingai.com" in out["error"]


def test_kling_probe_rejects_ak_sk_on_new_api(tmp_path: Path):
    from infrastructure.video_gen import KlingVideoAdapter
    from infrastructure.video_gen.profiles import CLOUD_PROFILE

    adapter = KlingVideoAdapter(
        base_url="https://api-beijing.klingai.com",
        api_key="ak:sk",
        data_dir=tmp_path,
        profile=CLOUD_PROFILE,
        model_id="kling-3.0-turbo",
    )
    out = _run(adapter.probe())
    assert out["ok"] is False
    assert "API Key" in out["error"]


def test_kling_adapter_create_poll_download(tmp_path: Path, monkeypatch):
    import infrastructure.video_gen.cloud_adapter as mod
    from infrastructure.video_gen import KlingVideoAdapter, VideoGenRequest
    from infrastructure.video_gen.profiles import CLOUD_PROFILE

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    adapter = KlingVideoAdapter(
        base_url="https://api.kling.example",
        api_key="ak:sk",
        data_dir=data_dir,
        profile=CLOUD_PROFILE,
        model_id="kling-v3-turbo",
    )

    class _Resp:
        def __init__(self, status_code=200, payload=None, content=b""):
            self.status_code = status_code
            self._payload = payload
            self.content = content
            self.text = json.dumps(payload or {})
            self.headers = {"content-length": str(len(content or b""))}

        def json(self):
            return self._payload

        async def aiter_bytes(self, chunk_size=65536):
            data = self.content or b""
            for i in range(0, len(data), chunk_size):
                yield data[i:i + chunk_size]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Client:
        def __init__(self, *a, **k):
            self.posts = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            self.posts.append((url, json, headers))
            assert url.endswith("/v1/videos/text2video")
            assert json["model_name"] == "kling-v3-turbo"
            assert json["aspect_ratio"] == "9:16"
            assert json["duration"] == "5"
            assert headers["Authorization"].startswith("Bearer ")
            return _Resp(200, {"code": 0, "data": {"task_id": "task-1"}})

        async def get(self, url, headers=None, timeout=None, params=None):
            if url.endswith("/v1/videos/text2video/task-1"):
                return _Resp(200, {
                    "data": {
                        "task_status": "succeed",
                        "task_result": {"videos": [{
                            "url": "https://cdn.example/v.mp4",
                            "duration": 5,
                        }]},
                    },
                })
            if "cdn.example" in url:
                return _Resp(200, content=_MINI_MP4)
            return _Resp(404)

        def stream(self, method, url, timeout=None):
            assert method == "GET"
            if "cdn.example" in url:
                return _Resp(200, content=_MINI_MP4)
            return _Resp(404)

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    result = _run(adapter.generate(
        VideoGenRequest(prompt="an orange cat", size="9:16", duration_sec=5),
        provider_id="p-cloud",
        model_id="kling-v3-turbo",
        session_id="sess-k",
    ))
    path = data_dir / "chat_videos" / result.filenames[0]
    assert path.exists()
    assert path.read_bytes().startswith(b"\x00\x00\x00\x18ftyp")
    assert result.backend == "cloud"
    assert result.duration_sec == 5
    assert result.public_urls[0].startswith("/chat-videos/")


def test_kling_new_api_create_poll_download(tmp_path: Path, monkeypatch):
    import infrastructure.video_gen.cloud_adapter as mod
    from infrastructure.video_gen import KlingVideoAdapter, VideoGenRequest
    from infrastructure.video_gen.profiles import CLOUD_PROFILE

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    adapter = KlingVideoAdapter(
        base_url="https://api-beijing.klingai.com",
        api_key="kling-console-key",
        data_dir=data_dir,
        profile=CLOUD_PROFILE,
        model_id="kling-3.0-turbo",
    )

    class _Resp:
        def __init__(self, status_code=200, payload=None, content=b""):
            self.status_code = status_code
            self._payload = payload
            self.content = content
            self.text = json.dumps(payload or {})
            self.headers = {"content-length": str(len(content or b""))}

        def json(self):
            return self._payload

        async def aiter_bytes(self, chunk_size=65536):
            data = self.content or b""
            for i in range(0, len(data), chunk_size):
                yield data[i:i + chunk_size]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            assert url.endswith("/text-to-video/kling-3.0-turbo")
            assert "model_name" not in json
            assert json["settings"]["duration"] == 5
            assert json["settings"]["aspect_ratio"] == "9:16"
            assert headers["Authorization"] == "Bearer kling-console-key"
            return _Resp(200, {"code": 0, "data": {"id": "893605946402811985"}})

        async def get(self, url, headers=None, timeout=None, params=None):
            if url.endswith("/tasks") and (params or {}).get("task_ids") == "893605946402811985":
                return _Resp(200, {
                    "code": 0,
                    "data": [{
                        "id": "893605946402811985",
                        "status": "succeeded",
                        "outputs": [{
                            "type": "video",
                            "url": "https://cdn.example/v.mp4",
                            "duration": "5",
                        }],
                    }],
                })
            if "cdn.example" in url:
                return _Resp(200, content=_MINI_MP4)
            return _Resp(404)

        def stream(self, method, url, timeout=None):
            assert method == "GET"
            if "cdn.example" in url:
                return _Resp(200, content=_MINI_MP4)
            return _Resp(404)

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    result = _run(adapter.generate(
        VideoGenRequest(prompt="an orange cat", size="9:16", duration_sec=5),
        provider_id="p-cloud",
        model_id="kling-3.0-turbo",
        session_id="sess-new",
    ))
    path = data_dir / "chat_videos" / result.filenames[0]
    assert path.exists()
    assert result.model_id == "kling-3.0-turbo"
    assert result.duration_sec == 5


def test_resolve_chat_image_and_pick_first(tmp_path: Path):
    from infrastructure.video_gen.images import (
        pick_first_image_name, resolve_chat_image,
    )

    root = tmp_path / "chat_images"
    root.mkdir()
    f = root / "img_abc.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert resolve_chat_image(tmp_path, "img_abc.png") == f.resolve()
    assert resolve_chat_image(tmp_path, "/chat-images/img_abc.png") == f.resolve()
    assert resolve_chat_image(tmp_path, "../etc/passwd") is None
    assert pick_first_image_name(["/chat-images/img_abc.png", "b.png"]) == "img_abc.png"


def test_kling_legacy_i2v_create_poll(tmp_path: Path, monkeypatch):
    import infrastructure.video_gen.cloud_adapter as mod
    from infrastructure.video_gen import KlingVideoAdapter, VideoGenRequest
    from infrastructure.video_gen.profiles import CLOUD_PROFILE

    data_dir = tmp_path / "data"
    (data_dir / "chat_images").mkdir(parents=True)
    img = data_dir / "chat_images" / "ref.png"
    img.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
        b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    adapter = KlingVideoAdapter(
        base_url="https://api.kling.example",
        api_key="ak:sk",
        data_dir=data_dir,
        profile=CLOUD_PROFILE,
        model_id="kling-v3-turbo",
    )

    class _Resp:
        def __init__(self, status_code=200, payload=None, content=b""):
            self.status_code = status_code
            self._payload = payload
            self.content = content
            self.text = json.dumps(payload or {})
            self.headers = {"content-length": str(len(content or b""))}

        def json(self):
            return self._payload

        async def aiter_bytes(self, chunk_size=65536):
            data = self.content or b""
            for i in range(0, len(data), chunk_size):
                yield data[i:i + chunk_size]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Client:
        def __init__(self, *a, **k):
            self.posts = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            self.posts.append((url, json))
            assert url.endswith("/v1/videos/image2video")
            assert json["image"].startswith("data:image/")
            assert json["prompt"] == "cat waves"
            return _Resp(200, {"code": 0, "data": {"task_id": "i2v-1"}})

        async def get(self, url, headers=None, timeout=None, params=None):
            if url.endswith("/v1/videos/image2video/i2v-1"):
                return _Resp(200, {
                    "data": {
                        "task_status": "succeed",
                        "task_result": {"videos": [{
                            "url": "https://cdn.example/v.mp4",
                            "duration": 5,
                        }]},
                    },
                })
            return _Resp(404)

        def stream(self, method, url, timeout=None):
            if "cdn.example" in url:
                return _Resp(200, content=_MINI_MP4)
            return _Resp(404)

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    result = _run(adapter.generate(
        VideoGenRequest(
            prompt="cat waves", size="9:16", duration_sec=5,
            image_filename="ref.png"),
        provider_id="p-cloud",
        model_id="kling-v3-turbo",
        session_id="sess-i2v",
    ))
    assert result.filenames
    assert (data_dir / "chat_videos" / result.filenames[0]).exists()


def test_comfy_rejects_i2v(tmp_path: Path):
    from infrastructure.video_gen.comfyui_adapter import ComfyUIVideoAdapter
    from infrastructure.video_gen.types import VideoGenRequest

    adapter = ComfyUIVideoAdapter(
        base_url="http://127.0.0.1:8188",
        data_dir=tmp_path,
        workflow_path=tmp_path / "missing.json",
    )
    with pytest.raises(RuntimeError, match="不支持图生视频"):
        _run(adapter.generate(
            VideoGenRequest(prompt="x", image_filename="a.png"),
            session_id="s",
        ))


def test_cloud_mutex_allows_image_then_video(tmp_path: Path):
    """云端不启用 GPU 互斥：同轮可先出图再出视频。"""
    from agent.turn_runtime_tools import TurnToolRunner
    from agent.turn_events import TurnEventStore
    from agent.repeat_tool_guard import RepeatToolGuard

    db = Database(tmp_path / "mutex-off.db")
    db.run_migrations(ROOT / "migrations")
    events_store = TurnEventStore(db)
    turn = events_store.start_turn("s3", reasoning_effort="high", max_steps=3)
    turn_id = turn["id"]
    calls = {"n": 0}

    class _Exec:
        async def execute_tool(self, name, params, **kw):
            calls["n"] += 1
            if name == "generate_image":
                return {"ok": True, "result": _IMG_RESULT}
            return {"ok": True, "result": _GEN_RESULT}

    registry = ToolRegistry()
    for name in ("generate_image", "generate_video"):
        registry.register_function(ToolSpec(
            name, name,
            {"type": "object", "properties": {"prompt": {"type": "string"}},
             "required": ["prompt"]}, parallel_safe=False), lambda **_: None)
    runner = TurnToolRunner(
        registry=registry, executor=_Exec(), events=events_store,
        gpu_mutex_enabled=lambda: False)

    async def emit(_n, _d):
        return None

    guard = RepeatToolGuard()

    async def scenario():
        r1 = await runner.run_tool_calls(turn_id, 1, [{
            "id": "c1", "type": "function",
            "function": {"name": "generate_image",
                         "arguments": '{"prompt":"a"}'},
        }], emit, guard)
        assert r1[0]["ok"] is True
        r2 = await runner.run_tool_calls(turn_id, 2, [{
            "id": "c2", "type": "function",
            "function": {"name": "generate_video",
                         "arguments": '{"prompt":"b"}'},
        }], emit, guard)
        assert r2[0]["ok"] is True
        assert calls["n"] == 2

    _run(scenario())
    db.close()
