"""本地生图/生视频无损优化验收。

默认（无 GPU）：
  - 校验无损工作流门禁（拒绝 MagCache/GGUF）
  - mock 走 Adapter，确认 T5→CPU 补丁与默认工作流路径

可选 LIVE（需本机 ComfyUI + 权重）：
  $env:LOCAL_GEN_LIVE=1
  python scripts/verify_local_gen_quality.py

用法：
  python scripts/verify_local_gen_quality.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from infrastructure.comfyui_accel import (  # noqa: E402
    apply_lossless_workflow_patches,
    assert_lossless_generation,
    build_comfyui_launch_extras,
    merge_launch_args,
)
from infrastructure.video_gen import ComfyUIVideoAdapter, VideoGenRequest  # noqa: E402

_MINI_MP4 = (
    b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
    b"\x00\x00\x00\x08free"
    b"\x00\x00\x00\x08mdat"
)


def _check_gate() -> None:
    wf = json.loads(
        (ROOT / "workflows" / "wan21_t2v_1_3b_lossless.json").read_text(
            encoding="utf-8"))
    assert_lossless_generation(
        wf, model_id="wan2.1_t2v_1.3B_fp16.safetensors", quality_mode="lossless")
    apply_lossless_workflow_patches(wf, t5_on_cpu=True, block_swap=0)
    assert wf["6"]["inputs"]["device"] == "cpu"

    bad = dict(wf)
    bad["99"] = {"class_type": "MagCache", "inputs": {}}
    try:
        assert_lossless_generation(bad, quality_mode="lossless")
        raise AssertionError("MagCache 应被拒绝")
    except RuntimeError as exc:
        assert "MagCache" in str(exc) or "有损" in str(exc)

    try:
        assert_lossless_generation(
            wf, model_id="wan-q4.gguf", quality_mode="lossless")
        raise AssertionError("GGUF 应被拒绝")
    except RuntimeError as exc:
        assert "gguf" in str(exc).lower() or "量化" in str(exc)

    # lossy 模式放行
    assert_lossless_generation(
        bad, model_id="wan-q4.gguf", quality_mode="lossy")
    print("[ok] quality gate")


def _check_launch_merge() -> None:
    extras = build_comfyui_launch_extras(
        lowvram=True, use_sage_attention=True, reserve_vram_gb=1.0)
    cmd = [
        "python.exe", "-s", "ComfyUI\\main.py",
        "--windows-standalone-build", "--listen", "127.0.0.1", "--port", "8188",
    ]
    merged = merge_launch_args(cmd, extras)
    assert isinstance(merged, list)
    assert "--lowvram" in merged
    assert "--use-sage-attention" in merged
    assert "--reserve-vram" in merged
    # 幂等
    merged2 = merge_launch_args(merged, extras)
    assert merged2.count("--lowvram") == 1
    print("[ok] launch args merge")


async def _check_adapter_mock(data_dir: Path) -> None:
    import infrastructure.video_gen.comfyui_adapter as mod

    class _Resp:
        def __init__(self, status_code=200, payload=None, content=b""):
            self.status_code = status_code
            self._payload = payload
            self.content = content
            self.text = json.dumps(payload or {})

        def json(self):
            return self._payload

    posted: dict = {}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):  # noqa: A002
            posted["prompt"] = (json or {}).get("prompt") or {}
            return _Resp(200, {"prompt_id": "q1"})

        async def get(self, url, params=None):
            if "/history/" in url:
                return _Resp(200, {
                    "q1": {
                        "status": {"completed": True},
                        "outputs": {
                            "20": {"videos": [{
                                "filename": "v.mp4",
                                "subfolder": "",
                                "type": "output",
                            }]},
                        },
                    },
                })
            return _Resp(200, content=_MINI_MP4)

    mod.httpx.AsyncClient = _Client  # type: ignore[misc]
    adapter = ComfyUIVideoAdapter(
        base_url="http://127.0.0.1:9",
        workflow_path=ROOT / "workflows" / "wan21_t2v_1_3b_lossless.json",
        data_dir=data_dir,
        timeout_sec=10,
        quality_mode="lossless",
        t5_on_cpu=True,
    )
    result = await adapter.generate(
        VideoGenRequest(prompt="cat", duration_sec=3, fps=16, seed=42),
        model_id="wan2.1_t2v_1.3B_fp16.safetensors",
    )
    assert result.n == 1
    clip = (posted.get("prompt") or {}).get("6") or {}
    assert clip.get("inputs", {}).get("device") == "cpu"
    print("[ok] adapter mock + T5 CPU patch")


async def _check_live() -> None:
    base = os.environ.get("COMFYUI_BASE", "http://127.0.0.1:8188").rstrip("/")
    import httpx
    async with httpx.AsyncClient(timeout=8.0) as client:
        r = await client.get(f"{base}/system_stats")
        if r.status_code != 200:
            raise RuntimeError(f"ComfyUI 不可达 HTTP {r.status_code}")
    data_dir = ROOT / "data"
    adapter = ComfyUIVideoAdapter(
        base_url=base,
        workflow_path=ROOT / "workflows" / "wan21_t2v_1_3b_lossless.json",
        data_dir=data_dir,
        timeout_sec=600,
        quality_mode="lossless",
        t5_on_cpu=True,
    )
    result = await adapter.generate(
        VideoGenRequest(
            prompt="an orange cat on a sunny windowsill, gentle motion",
            duration_sec=3,
            fps=16,
            seed=42,
        ),
        model_id="wan2.1_t2v_1.3B_fp16.safetensors",
    )
    out = data_dir / "chat_videos" / result.filenames[0]
    assert out.exists() and out.stat().st_size > 1000
    print(f"[ok] live video -> {out} ({result.latency_ms} ms)")


def main() -> int:
    _check_gate()
    _check_launch_merge()
    with tempfile.TemporaryDirectory() as td:
        asyncio.run(_check_adapter_mock(Path(td)))
    if os.environ.get("LOCAL_GEN_LIVE", "").strip() in ("1", "true", "yes"):
        asyncio.run(_check_live())
    else:
        print("[skip] live GPU (set LOCAL_GEN_LIVE=1 to enable)")
    print("verify_local_gen_quality: all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
