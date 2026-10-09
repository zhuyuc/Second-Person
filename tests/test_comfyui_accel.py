"""本地 ComfyUI 无损加速：门禁、启动参数、Adapter 补丁。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_lossless_workflow_file_has_cpu_clip():
    wf = json.loads(
        (ROOT / "workflows" / "wan21_t2v_1_3b_lossless.json").read_text(
            encoding="utf-8"))
    assert wf["6"]["class_type"] == "CLIPLoader"
    assert wf["6"]["inputs"]["device"] == "cpu"
    assert wf["3"]["inputs"]["unet_name"].endswith(".safetensors")


def test_assert_lossless_rejects_magcache_and_gguf():
    from infrastructure.comfyui_accel import assert_lossless_generation

    wf = json.loads(
        (ROOT / "workflows" / "wan21_t2v_1_3b_lossless.json").read_text(
            encoding="utf-8"))
    assert_lossless_generation(
        wf, model_id="wan2.1_t2v_1.3B_fp16.safetensors", quality_mode="lossless")

    bad = dict(wf)
    bad["99"] = {"class_type": "TeaCache", "inputs": {}}
    with pytest.raises(RuntimeError, match="TeaCache|有损"):
        assert_lossless_generation(bad, quality_mode="lossless")

    with pytest.raises(RuntimeError, match="量化|gguf"):
        assert_lossless_generation(
            wf, model_id="x.gguf", quality_mode="lossless")

    # lossy 不拦截
    assert_lossless_generation(
        bad, model_id="x.gguf", quality_mode="lossy")


def test_merge_launch_args_idempotent():
    from infrastructure.comfyui_accel import (
        build_comfyui_launch_extras,
        merge_launch_args,
    )

    extras = build_comfyui_launch_extras(
        lowvram=True, use_sage_attention=True, reserve_vram_gb=2)
    base = ["python", "-s", "ComfyUI/main.py", "--port", "8188"]
    once = merge_launch_args(base, extras)
    twice = merge_launch_args(once, extras)
    assert once.count("--lowvram") == 1
    assert twice.count("--lowvram") == 1
    assert twice.count("--use-sage-attention") == 1
    assert "--reserve-vram" in twice
    assert "2" in twice or "2.0" in twice

    shell = merge_launch_args(
        "python -s ComfyUI\\main.py --port 8188",
        ["--lowvram", "--use-sage-attention"],
    )
    assert isinstance(shell, str)
    assert shell.count("--lowvram") == 1


def test_video_adapter_patches_clip_device(tmp_path: Path):
    from infrastructure.video_gen.comfyui_adapter import ComfyUIVideoAdapter
    from infrastructure.video_gen.types import VideoGenRequest

    wf_path = ROOT / "workflows" / "wan21_t2v_1_3b.json"
    adapter = ComfyUIVideoAdapter(
        base_url="http://127.0.0.1:9",
        workflow_path=wf_path,
        data_dir=tmp_path,
        quality_mode="lossless",
        t5_on_cpu=True,
        block_swap=0,
    )
    template = adapter._load_workflow()
    # 基线工作流 default → 补丁后应为 cpu
    assert template["6"]["inputs"].get("device") == "default"
    out = adapter._apply_workflow(
        template,
        VideoGenRequest(prompt="cat", duration_sec=3, fps=16, seed=1),
        "wan2.1_t2v_1.3B_fp16.safetensors",
    )
    assert out["6"]["inputs"]["device"] == "cpu"


def test_video_adapter_rejects_gguf_in_lossless(tmp_path: Path, monkeypatch):
    import asyncio

    import infrastructure.video_gen.comfyui_adapter as mod
    from infrastructure.video_gen.comfyui_adapter import ComfyUIVideoAdapter
    from infrastructure.video_gen.types import VideoGenRequest

    adapter = ComfyUIVideoAdapter(
        base_url="http://127.0.0.1:9",
        workflow_path=ROOT / "workflows" / "wan21_t2v_1_3b_lossless.json",
        data_dir=tmp_path,
        quality_mode="lossless",
    )

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    with pytest.raises(RuntimeError, match="量化|gguf"):
        asyncio.run(adapter.generate(
            VideoGenRequest(prompt="x"),
            model_id="wan-q4.gguf",
        ))


def test_factory_default_workflow_is_lossless(tmp_path: Path):
    from infrastructure.video_gen.factory import get_video_adapter

    class _Snap:
        provider_type = "comfyui"
        base_url = "http://127.0.0.1:8188"
        model_id = "wan2.1_t2v_1.3B_fp16.safetensors"
        api_key = ""

    class _Cfg:
        def get_raw(self, key, default=None):
            return default

        def get(self, key, default=None):
            defaults = {
                "video_gen_fps": 16,
                "video_gen_steps": 20,
                "video_gen_prompt_max_chars": 1500,
                "comfyui_quality_mode": "lossless",
                "video_gen_t5_on_cpu": True,
                "video_gen_block_swap": 0,
            }
            return defaults.get(key, default)

    adapter = get_video_adapter(_Snap(), _Cfg(), tmp_path)
    assert adapter.workflow_path.name == "wan21_t2v_1_3b_lossless.json"
    assert adapter.quality_mode == "lossless"
    assert adapter.t5_on_cpu is True


def test_config_schema_has_lossless_keys():
    from infrastructure.config_manager import PARAM_SCHEMA

    keys = {p["key"] for p in PARAM_SCHEMA}
    for k in (
        "comfyui_quality_mode",
        "comfyui_use_sage_attention",
        "comfyui_lowvram",
        "comfyui_reserve_vram_gb",
        "video_gen_t5_on_cpu",
        "video_gen_block_swap",
    ):
        assert k in keys
