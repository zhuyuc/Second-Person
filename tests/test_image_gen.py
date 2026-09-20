"""文生图 generate_image：tool_visual、落盘、槽位、visuals 清理。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agent.turn_runtime import TurnRuntime
from infrastructure.db import Database
from infrastructure.image_gen import ComfyUIAdapter, ImageGenRequest, probe_comfyui
from tools.base import ToolRegistry, ToolSpec

import pytest

ROOT = Path(__file__).resolve().parent.parent

_GEN_RESULT = {
    "type": "generated_image",
    "filenames": ["gen_testhash12.png"],
    "public_urls": ["/chat-images/gen_testhash12.png"],
    "n": 1,
    "size": "1024x1024",
    "provider_id": "prov_img",
    "model_id": "sd_xl_base_1.0.safetensors",
    "backend": "comfyui",
    "summary": "已本地生成 1 张图（SDXL，约 1s）",
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

    def update_message(self, msg_id, **fields):
        target = self.messages[msg_id - 1]
        for key, value in fields.items():
            if value is not None:
                target[key] = value


class _Provider:
    model_id = "test-model"
    context_window = 10000


class _Providers:
    def snapshot_for(self, _slot):
        return _Provider()


class _ImageExecutor:
    async def execute_tool(self, name, params, **_kw):
        if name == "generate_image":
            return {"ok": True, "result": _GEN_RESULT}
        return {"ok": True, "result": {"summary": f"{name} 结果"}}


class _ImageLLM:
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
                        "name": "generate_image",
                        "arguments": '{"prompt":"an orange cat on a windowsill"}',
                    },
                }],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
        else:
            yield "content", "图已生成"
            yield "done", {
                "content": "图已生成",
                "tool_calls": [],
                "usage": {"input_tokens": 5, "output_tokens": 2},
            }


def _run(coro):
    # 勿用 new_event_loop().run_until_complete：在 TestClient/anyio 之后会挂死
    return asyncio.run(coro)


def _build_runtime(db):
    registry = ToolRegistry()
    registry.register_function(ToolSpec(
        "generate_image", "本地生图",
        {"type": "object", "properties": {"prompt": {"type": "string"}},
         "required": ["prompt"]}), lambda **_: None)
    sessions = _Sessions()

    async def context_loader(**_kw):
        return {"snap": _Provider(), "history": [],
                "history_ids": [], "memory_count": 0}

    runtime = TurnRuntime(
        db=db, config=_Config(agent_max_steps=3),
        sessions=sessions, registry=registry,
        executor=_ImageExecutor(), llm=_ImageLLM(),
        providers=_Providers(),
        system_prompt=lambda *_a: "sys",
        context_loader=context_loader)
    return runtime, sessions


def test_generate_image_tool_visual_emitted_and_persisted(tmp_path: Path):
    async def scenario():
        db = Database(tmp_path / "v.db")
        db.run_migrations(ROOT / "migrations")
        try:
            events: list[tuple[str, dict]] = []

            async def emit(name, data):
                events.append((name, data))

            runtime, sessions = _build_runtime(db)
            await runtime.run(session_id="s", message="画一只猫",
                              reasoning_effort="high", emit=emit)

            visual_events = [d for (n, d) in events if n == "tool_visual"]
            assert len(visual_events) == 1, "generate_image 成功后应发射一次 tool_visual"
            ve = visual_events[0]
            assert ve["type"] == "generated_image"
            assert ve["data"]["filenames"][0].startswith("gen_")

            assistant = [m for m in sessions.messages if m["role"] == "assistant"][-1]
            visuals = assistant.get("visuals")
            assert visuals, "assistant 消息应持久化 visuals"
            assert visuals[0]["type"] == "generated_image"
            assert visuals[0]["data"]["public_urls"][0].startswith("/chat-images/")
        finally:
            db.close()

    _run(scenario())


def test_comfyui_adapter_persist_with_mock_http(tmp_path: Path, monkeypatch):
    import infrastructure.image_gen.comfyui_adapter as mod

    wf = ROOT / "workflows" / "sdxl_txt2img.json"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    adapter = ComfyUIAdapter(
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
            return _Resp(200, {"prompt_id": "pid1"})

        async def get(self, url, params=None):
            if "/history/" in url:
                return _Resp(200, {
                    "pid1": {
                        "status": {"completed": True, "status_str": "success"},
                        "outputs": {
                            "9": {"images": [{
                                "filename": "sp_gen_00001_.png",
                                "subfolder": "",
                                "type": "output",
                            }]},
                        },
                    },
                })
            if url.endswith("/view"):
                return _Resp(200, content=b"\x89PNG\r\n\x1a\nfake")
            return _Resp(404)

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    result = _run(adapter.generate(
        ImageGenRequest(prompt="an orange cat"),
        provider_id="p1",
        model_id="sd_xl_base_1.0.safetensors",
    ))
    assert result.n == 1
    assert result.type == "generated_image"
    assert result.filenames[0].startswith("gen_")
    assert (data_dir / "chat_images" / result.filenames[0]).exists()
    assert result.public_urls[0] == f"/chat-images/{result.filenames[0]}"


def test_probe_comfyui_connect_error(monkeypatch):
    import httpx
    import infrastructure.image_gen.comfyui_adapter as mod

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            raise httpx.ConnectError("refuse")

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    out = _run(probe_comfyui("http://127.0.0.1:8188"))
    assert out["ok"] is False
    assert "未启动" in out["error"]


def test_image_gen_slot_registered():
    from infrastructure.provider_registry import TASK_SLOTS
    assert "image_gen" in TASK_SLOTS
    assert TASK_SLOTS["image_gen"].fallback == ()


def test_turn_blocks_second_generate_image(tmp_path: Path):
    """同轮第二次 generate_image 应被硬拦。"""
    from agent.turn_runtime_tools import TurnToolRunner
    from agent.turn_events import TurnEventStore

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
        "generate_image", "g",
        {"type": "object", "properties": {"prompt": {"type": "string"}},
         "required": ["prompt"]}, parallel_safe=False), lambda **_: None)
    runner = TurnToolRunner(registry=registry, executor=_Exec(), events=events_store)
    emitted = []

    async def emit(n, d):
        emitted.append((n, d))

    from agent.repeat_tool_guard import RepeatToolGuard
    guard = RepeatToolGuard()

    async def scenario():
        tc = [{
            "id": "c1", "type": "function",
            "function": {"name": "generate_image",
                         "arguments": '{"prompt":"a"}'},
        }]
        r1 = await runner.run_tool_calls(turn_id, 1, tc, emit, guard)
        assert r1[0]["ok"] is True
        r2 = await runner.run_tool_calls(turn_id, 2, [{
            "id": "c2", "type": "function",
            "function": {"name": "generate_image",
                         "arguments": '{"prompt":"b"}'},
        }], emit, guard)
        assert r2[0]["ok"] is False
        assert "本轮已成功出图" in (r2[0].get("error") or "")
        assert calls["n"] == 1

    _run(scenario())
    db.close()


def test_allowed_sizes():
    from infrastructure.image_gen import ALLOWED_SIZES
    assert ALLOWED_SIZES == ("1024x1024",)


def test_image_factory_picks_cloud_adapter(tmp_path: Path):
    from infrastructure.image_gen import OpenAIImageAdapter, get_image_adapter

    class _Snap:
        base_url = "https://api.openai.example"
        api_key = "sk-test"
        model_id = "dall-e-3"

        def __init__(self, provider_type):
            self.provider_type = provider_type

    for ptype in ("openai_compatible", "custom"):
        adapter = get_image_adapter(_Snap(ptype), _Config(), tmp_path)
        assert isinstance(adapter, OpenAIImageAdapter)


def test_openai_image_adapter_download(tmp_path: Path, monkeypatch):
    import infrastructure.image_gen.cloud_adapter as mod
    from infrastructure.image_gen import OpenAIImageAdapter, ImageGenRequest

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    adapter = OpenAIImageAdapter(
        base_url="https://api.openai.example",
        api_key="sk-test",
        data_dir=data_dir,
        model_id="dall-e-3",
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
            assert url.endswith("/images/generations")
            assert json["n"] == 1
            return _Resp(200, {"data": [{"url": "https://cdn.example/cat.png"}]})

        async def get(self, url, headers=None, timeout=None, params=None):
            assert "cdn.example" in url
            return _Resp(200, content=b"\x89PNG\r\n\x1a\nfake")

        def stream(self, method, url, timeout=None):
            assert method == "GET"
            assert "cdn.example" in url
            return _Resp(200, content=b"\x89PNG\r\n\x1a\nfake")

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    result = _run(adapter.generate(
        ImageGenRequest(prompt="an orange cat"),
        provider_id="p-img",
        model_id="dall-e-3",
    ))
    path = data_dir / "chat_images" / result.filenames[0]
    assert path.exists()
    assert result.backend == "cloud"
    assert result.n == 1


def test_custom_image_posts_to_the_url_as_written(tmp_path: Path, monkeypatch):
    import infrastructure.image_gen.cloud_adapter as mod
    from infrastructure.image_gen import OpenAIImageAdapter, ImageGenRequest

    seen = {}
    raw = "https://example.com/api/v1/services/aigc/text2image/image-synthesis"

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"data": [{"b64_json": "aGk="}]}

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

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(mod, "get_store", lambda: None)
    adapter = OpenAIImageAdapter(
        base_url=raw, api_key="sk", data_dir=tmp_path,
        model_id="wanx", provider_type="custom")
    _run(adapter.generate(ImageGenRequest(prompt="猫"), session_id="s"))
    assert seen["url"] == raw


def test_generate_image_skips_empty_retry():
    """图/视频禁止空结果二次调用，避免云端双倍扣费。"""
    from agent.tool_executor import ToolExecutor

    calls = {"n": 0}

    async def empty_fn(**_kw):
        calls["n"] += 1
        return {}

    registry = ToolRegistry()
    registry.register_function(ToolSpec(
        "generate_image", "g",
        {"type": "object", "properties": {"prompt": {"type": "string"}},
         "required": ["prompt"]}, parallel_safe=False), empty_fn)

    class _Cfg:
        def get(self, k, default=None):
            return default

    out = _run(ToolExecutor(registry, _Cfg()).execute_tool(
        "generate_image", {"prompt": "a"}))
    assert calls["n"] == 1
    assert out["ok"] is True


def test_mcp_tool_skips_empty_retry_and_timeout_hints():
    """MCP 副作用：空结果不重试；超时文案禁止自动重试。"""
    from agent.tool_executor import ToolExecutor

    calls = {"n": 0}

    async def empty_fn(**_kw):
        calls["n"] += 1
        return {}

    registry = ToolRegistry()
    registry.register_function(ToolSpec(
        "conn_x__do", "mcp tool",
        {"type": "object", "properties": {}},
        source="mcp", connector_id="conn_x", parallel_safe=False), empty_fn)

    class _Cfg:
        def get(self, k, default=None):
            if k == "tool_timeout_seconds":
                return 1
            return default

    out = _run(ToolExecutor(registry, _Cfg()).execute_tool("conn_x__do", {}))
    assert calls["n"] == 1
    assert out["ok"] is True

    async def hang(**_kw):
        import asyncio
        await asyncio.sleep(5)

    registry.register_function(ToolSpec(
        "conn_x__hang", "mcp hang",
        {"type": "object", "properties": {}},
        source="mcp", connector_id="conn_x", parallel_safe=False), hang)
    out2 = _run(ToolExecutor(registry, _Cfg()).execute_tool("conn_x__hang", {}))
    assert out2["ok"] is False
    assert "请勿自动重试" in (out2.get("error") or "")


def test_openai_image_post_timeout_forbids_blind_retry(tmp_path: Path, monkeypatch):
    import infrastructure.image_gen.cloud_adapter as mod
    from infrastructure.image_gen import OpenAIImageAdapter, ImageGenRequest
    import httpx

    adapter = OpenAIImageAdapter(
        base_url="https://api.openai.example",
        api_key="sk-test",
        data_dir=tmp_path,
        model_id="dall-e-3",
        timeout_sec=1,
    )

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    with pytest.raises(RuntimeError) as ei:
        _run(adapter.generate(ImageGenRequest(prompt="cat")))
    assert "请勿立即" in str(ei.value) or "重复扣费" in str(ei.value)
