"""按 Provider 协议选择文生图 Adapter。"""
from __future__ import annotations

from pathlib import Path

from .cloud_adapter import OpenAIImageAdapter
from .comfyui_adapter import ComfyUIAdapter
from .named_adapters import AnthropicImageAdapter, GoogleImageAdapter


def get_image_adapter(snap, config, data_dir: Path):
    ptype = (getattr(snap, "provider_type", "") or "").strip().lower()
    if ptype == "comfyui":
        wf = Path(config.get_raw(
            "image_gen_comfyui_workflow",
            "./workflows/sdxl_txt2img.json") or "./workflows/sdxl_txt2img.json")
        if not wf.is_absolute():
            wf = (Path(data_dir).parent / wf).resolve()
        return ComfyUIAdapter(
            base_url=(snap.base_url or "http://127.0.0.1:8188").rstrip("/"),
            workflow_path=wf,
            data_dir=Path(data_dir),
            timeout_sec=float(config.get("image_gen_timeout_sec", 180) or 180),
            default_steps=int(config.get("image_gen_steps", 24) or 24),
            prompt_max_chars=int(
                config.get("image_gen_prompt_max_chars", 1500) or 1500),
        )
    if ptype in ("openai_compatible", "custom"):
        return OpenAIImageAdapter(
            base_url=snap.base_url or "",
            api_key=getattr(snap, "api_key", "") or "",
            data_dir=Path(data_dir),
            timeout_sec=float(config.get("image_gen_timeout_sec", 180) or 180),
            model_id=snap.model_id or "",
            provider_type=ptype,
        )
    common = dict(
        base_url=snap.base_url or "",
        api_key=getattr(snap, "api_key", "") or "",
        data_dir=Path(data_dir),
        timeout_sec=float(config.get("image_gen_timeout_sec", 180) or 180),
        model_id=snap.model_id or "",
    )
    if ptype == "google":
        return GoogleImageAdapter(**common)
    if ptype == "anthropic":
        return AnthropicImageAdapter(**common)
    raise RuntimeError(
        f"文生图不支持协议 {ptype or '（空）'}，请选择 OpenAI 兼容、Anthropic、自定义、Google 或本地 ComfyUI")
