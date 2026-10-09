"""图片厂商协议：可灵 / 百炼 / 火山。"""
from __future__ import annotations

import asyncio
from pathlib import Path


def _run(coro):
    return asyncio.run(coro)


def test_image_factory_routes_vendors(tmp_path: Path):
    from infrastructure.image_gen.dashscope_adapter import DashScopeImageAdapter
    from infrastructure.image_gen.factory import get_image_adapter
    from infrastructure.image_gen.kling_adapter import KlingImageAdapter
    from infrastructure.image_gen.volcengine_adapter import VolcengineImageAdapter

    class _Cfg:
        def get(self, key, default=None):
            return default

        def get_raw(self, key, default=None):
            return default

    class _Snap:
        def __init__(self, ptype, base, model):
            self.provider_type = ptype
            self.base_url = base
            self.api_key = "sk"
            self.model_id = model

    assert isinstance(
        get_image_adapter(
            _Snap("volcengine", "https://ark.cn-beijing.volces.com/api/v3",
                  "doubao-seedream-4-0-250828"),
            _Cfg(), tmp_path),
        VolcengineImageAdapter)
    assert isinstance(
        get_image_adapter(
            _Snap("dashscope", "https://dashscope.aliyuncs.com/api/v1",
                  "wan2.2-t2i-flash"),
            _Cfg(), tmp_path),
        DashScopeImageAdapter)
    assert isinstance(
        get_image_adapter(
            _Snap("kling", "https://api-beijing.klingai.com", "kling-v2"),
            _Cfg(), tmp_path),
        KlingImageAdapter)


def test_volcengine_image_posts_images_generations(tmp_path: Path, monkeypatch):
    import infrastructure.image_gen.volcengine_adapter as mod
    from infrastructure.image_gen.types import ImageGenRequest
    from infrastructure.image_gen.volcengine_adapter import VolcengineImageAdapter

    posted = {}

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"data": [{"url": "https://cdn.example/a.png"}]}

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
            return _Resp()

    async def fake_fetch(url, **kw):
        return b"png-bytes"

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(mod, "fetch_url_bytes", fake_fetch)

    adapter = VolcengineImageAdapter(
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key="sk-ark",
        data_dir=tmp_path,
        model_id="doubao-seedream-5-0-pro",
    )
    result = _run(adapter.generate(
        ImageGenRequest(prompt="一只猫"), session_id="s1"))
    assert posted["url"].endswith("/images/generations")
    assert posted["json"]["model"] == "doubao-seedream-5-0-pro"
    assert posted["json"]["prompt"] == "一只猫"
    assert "sequential_image_generation" not in posted["json"]  # 5.x 不带
    assert result.filenames
    assert (tmp_path / "chat_images" / result.filenames[0]).read_bytes() == b"png-bytes"


def test_volcengine_image_rejects_seedance_model(tmp_path: Path):
    from infrastructure.image_gen.types import ImageGenRequest
    from infrastructure.image_gen.volcengine_adapter import VolcengineImageAdapter

    adapter = VolcengineImageAdapter(
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key="sk",
        data_dir=tmp_path,
        model_id="doubao-seedance-1-0-pro-250528",
    )
    try:
        _run(adapter.generate(ImageGenRequest(prompt="x"), session_id="s"))
        assert False, "should raise"
    except RuntimeError as exc:
        assert "Seedance" in str(exc) or "视频" in str(exc)
