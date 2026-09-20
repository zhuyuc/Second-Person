"""按 Provider 协议选择文生视频 Adapter。"""
from __future__ import annotations

from pathlib import Path

from .anthropic_adapter import AnthropicVideoAdapter
from .cloud_adapter import KlingVideoAdapter
from .comfyui_adapter import ComfyUIVideoAdapter
from .custom_adapter import CustomVideoAdapter
from .dashscope_adapter import DashScopeVideoAdapter
from .google_adapter import GoogleVideoAdapter
from .openai_adapter import OpenAIVideoAdapter
from .profiles import video_profile_for


def get_video_adapter(snap, config, data_dir: Path):
    ptype = (getattr(snap, "provider_type", "") or "").strip().lower()
    profile = video_profile_for(snap, config)
    if ptype == "comfyui":
        wf = Path(config.get_raw(
            "video_gen_comfyui_workflow",
            "./workflows/wan21_t2v_1.3b.json") or "./workflows/wan21_t2v_1.3b.json")
        if not wf.is_absolute():
            wf = (Path(data_dir).parent / wf).resolve()
        fps = int(config.get("video_gen_fps", 16) or 16)
        return ComfyUIVideoAdapter(
            base_url=(snap.base_url or "http://127.0.0.1:8188").rstrip("/"),
            workflow_path=wf,
            data_dir=Path(data_dir),
            timeout_sec=profile.timeout_sec,
            default_steps=int(config.get("video_gen_steps", 20) or 20),
            default_size=profile.default_size,
            default_duration_sec=profile.default_duration,
            fps=fps,
            prompt_max_chars=int(
                config.get("video_gen_prompt_max_chars", 1500) or 1500),
        )
    common = dict(
        base_url=snap.base_url or "",
        api_key=getattr(snap, "api_key", "") or "",
        data_dir=Path(data_dir),
        profile=profile,
        model_id=snap.model_id or "",
    )
    if ptype == "kling":
        return KlingVideoAdapter(**common)
    if ptype == "dashscope":
        return DashScopeVideoAdapter(**common)
    if ptype == "custom":
        return CustomVideoAdapter(**common)
    if ptype == "openai_compatible":
        return OpenAIVideoAdapter(**common)
    if ptype == "google":
        return GoogleVideoAdapter(**common)
    if ptype == "anthropic":
        return AnthropicVideoAdapter(**common)
    raise RuntimeError(
        f"文生视频不支持协议 {ptype or '（空）'}，"
        "请选择 OpenAI 兼容、Anthropic、自定义、Google 或本地 ComfyUI")
