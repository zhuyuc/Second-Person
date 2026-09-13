"""Provider 用途模态（text/image/video）与协议组合校验。"""
from __future__ import annotations

MODALITY_TEXT = "text"
MODALITY_IMAGE = "image"
MODALITY_VIDEO = "video"
MODALITIES = (MODALITY_TEXT, MODALITY_IMAGE, MODALITY_VIDEO)

# 三种模态共用的云端协议；文本另加 google，图/视频另加本地 ComfyUI。
CLOUD_PROTOCOLS = frozenset({"openai_compatible", "anthropic", "custom"})

PROTOCOLS_BY_MODALITY: dict[str, frozenset[str]] = {
    MODALITY_TEXT: CLOUD_PROTOCOLS | frozenset({"google"}),
    MODALITY_IMAGE: CLOUD_PROTOCOLS | frozenset({"comfyui"}),
    MODALITY_VIDEO: CLOUD_PROTOCOLS | frozenset({"comfyui"}),
}

SLOT_MODALITY: dict[str, str] = {
    "chat": MODALITY_TEXT,
    "agent": MODALITY_TEXT,
    "embedding": MODALITY_TEXT,
    "vision": MODALITY_TEXT,
    "retriever_refine": MODALITY_TEXT,
    "image_gen": MODALITY_IMAGE,
    "video_gen": MODALITY_VIDEO,
}


def normalize_modality(value: str | None, default: str = MODALITY_TEXT) -> str:
    raw = (value or default or MODALITY_TEXT).strip().lower()
    return raw if raw in MODALITIES else default


def infer_modality(provider_type: str, model_id: str = "") -> str:
    ptype = (provider_type or "").strip().lower()
    mid = (model_id or "").lower()
    if ptype == "comfyui":
        if "wan" in mid:
            return MODALITY_VIDEO
        return MODALITY_IMAGE
    if "kling" in mid or "wan" in mid:
        return MODALITY_VIDEO
    if any(k in mid for k in ("dall-e", "dalle", "sdxl", "gpt-image")):
        return MODALITY_IMAGE
    return MODALITY_TEXT


def protocols_for(modality: str) -> frozenset[str]:
    return PROTOCOLS_BY_MODALITY.get(normalize_modality(modality), frozenset())


def validate_combo(modality: str, provider_type: str) -> None:
    ptype = (provider_type or "").strip().lower()
    allowed = protocols_for(modality)
    if ptype not in allowed:
        raise ValueError(
            f"模态 {normalize_modality(modality)} 不支持协议 {ptype or '（空）'}")


def slot_modality(slot_key: str) -> str:
    return SLOT_MODALITY.get(slot_key, MODALITY_TEXT)


def is_local_gpu(snap) -> bool:
    ptype = (getattr(snap, "provider_type", "") or "").strip().lower()
    return ptype == "comfyui"


def is_cloud_http(provider_type: str) -> bool:
    return (provider_type or "").strip().lower() in CLOUD_PROTOCOLS
