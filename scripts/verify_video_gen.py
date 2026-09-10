"""验证文生视频自动落盘链路（Adapter → chat_videos/genv_*.mp4）。

默认用 mock ComfyUI（无需 GPU），证明产品路径可自动生成并保存视频文件。
若本机 ComfyUI 已启动且工作流可用，设置环境变量 VIDEO_GEN_LIVE=1 走真实推理。

用法：
  python scripts/verify_video_gen.py
  $env:VIDEO_GEN_LIVE=1; python scripts/verify_video_gen.py
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

from infrastructure.video_gen import ComfyUIVideoAdapter, VideoGenRequest  # noqa: E402

_MINI_MP4 = (
    b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
    b"\x00\x00\x00\x08free"
    b"\x00\x00\x00\x08mdat"
)


async def _run_mock(data_dir: Path) -> Path:
    import infrastructure.video_gen.comfyui_adapter as mod

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

        async def post(self, url, json=None):  # noqa: A002
            return _Resp(200, {"prompt_id": "verify1"})

        async def get(self, url, params=None):
            if "/history/" in url:
                return _Resp(200, {
                    "verify1": {
                        "status": {"completed": True},
                        "outputs": {
                            "20": {"videos": [{
                                "filename": "verify.mp4",
                                "subfolder": "",
                                "type": "output",
                            }]},
                        },
                    },
                })
            return _Resp(200, content=_MINI_MP4)

    mod.httpx.AsyncClient = _Client  # type: ignore[misc]
    adapter = ComfyUIVideoAdapter(
        base_url="http://127.0.0.1:8188",
        workflow_path=ROOT / "workflows" / "wan21_t2v_1_3b.json",
        data_dir=data_dir,
        timeout_sec=30,
    )
    result = await adapter.generate(
        VideoGenRequest(prompt="an orange cat lounging on a sunny windowsill"),
        provider_id="verify",
        model_id="wan2.1_t2v_1.3B_fp16.safetensors",
        session_id="verify-session",
    )
    path = data_dir / "chat_videos" / result.filenames[0]
    assert path.exists(), "未落盘"
    assert path.stat().st_size > 0, "空文件"
    assert result.type == "generated_video"
    assert result.public_urls[0].startswith("/chat-videos/")
    print(f"[mock] OK type={result.type} file={path} bytes={path.stat().st_size}")
    print(f"[mock] public_url={result.public_urls[0]} summary={result.summary}")
    return path


async def _run_live(data_dir: Path) -> Path:
    from infrastructure.video_gen import probe_comfyui

    base = os.environ.get("COMFYUI_BASE_URL", "http://127.0.0.1:8188")
    probe = await probe_comfyui(base)
    if not probe.get("ok"):
        raise SystemExit(f"ComfyUI 不可用：{probe.get('error')}")

    model = os.environ.get(
        "VIDEO_GEN_MODEL_ID", "wan2.1_t2v_1.3B_fp16.safetensors")
    adapter = ComfyUIVideoAdapter(
        base_url=base,
        workflow_path=ROOT / "workflows" / "wan21_t2v_1_3b.json",
        data_dir=data_dir,
        timeout_sec=float(os.environ.get("VIDEO_GEN_TIMEOUT", "600")),
    )
    print(f"[live] 提交 ComfyUI {base} model={model} …（可能需要数分钟）")
    result = await adapter.generate(
        VideoGenRequest(prompt="an orange cat lounging on a sunny windowsill, gentle breeze"),
        provider_id="live",
        model_id=model,
        session_id="verify-live",
    )
    path = data_dir / "chat_videos" / result.filenames[0]
    assert path.exists() and path.stat().st_size > 1000
    print(f"[live] OK file={path} bytes={path.stat().st_size} ms={result.latency_ms}")
    return path


def main() -> None:
    live = os.environ.get("VIDEO_GEN_LIVE", "").strip() in ("1", "true", "yes")
    out = Path(os.environ.get("VIDEO_GEN_OUT", "") or "")
    if out:
        out.mkdir(parents=True, exist_ok=True)
        data_dir = out
        cleanup = False
    else:
        tmp = tempfile.TemporaryDirectory(prefix="sp_video_verify_")
        data_dir = Path(tmp.name) / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        cleanup = True

    try:
        if live:
            asyncio.run(_run_live(data_dir))
        else:
            asyncio.run(_run_mock(data_dir))
        print("verify_video_gen: PASS")
    finally:
        if cleanup:
            tmp.cleanup()  # type: ignore[name-defined]


if __name__ == "__main__":
    main()
