"""Provider 用途模态（text/image/video）与协议组合校验。"""
from __future__ import annotations

MODALITY_TEXT = "text"
MODALITY_IMAGE = "image"
MODALITY_VIDEO = "video"
MODALITIES = (MODALITY_TEXT, MODALITY_IMAGE, MODALITY_VIDEO)

# 三种模态共用同一套协议名。OpenAI 兼容才补本模态的标准路径；
# 自定义按填写地址原样请求；Anthropic / Google 走各自的固定接口。
# 图和视频另有本地 ComfyUI。kling / dashscope 只留给已经保存的旧记录。
TEXT_PROTOCOLS = frozenset({"openai_compatible", "anthropic", "custom", "google"})
CLOUD_PROTOCOLS = frozenset({"openai_compatible", "anthropic", "custom"})
OPENAI_WIRE = frozenset({"openai_compatible"})
LEGACY_VIDEO = frozenset({"kling", "dashscope"})

PROTOCOLS_BY_MODALITY: dict[str, frozenset[str]] = {
    MODALITY_TEXT: TEXT_PROTOCOLS,
    MODALITY_IMAGE: TEXT_PROTOCOLS | frozenset({"comfyui"}),
    MODALITY_VIDEO: TEXT_PROTOCOLS | frozenset({"comfyui"}) | LEGACY_VIDEO,
}

SLOT_MODALITY: dict[str, str] = {
    "chat": MODALITY_TEXT,
    "agent": MODALITY_TEXT,
    "embedding": MODALITY_TEXT,
    "vision": MODALITY_TEXT,
    "retriever_refine": MODALITY_TEXT,
    "mood_fast": MODALITY_TEXT,
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
    if ptype in ("kling", "dashscope"):
        return MODALITY_VIDEO
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


def is_openai_wire(provider_type: str) -> bool:
    return (provider_type or "").strip().lower() in OPENAI_WIRE


def is_custom(provider_type: str) -> bool:
    return (provider_type or "").strip().lower() == "custom"


def endpoint_url(base_url: str, provider_type: str, suffix: str) -> str:
    """OpenAI 兼容才补路径。自定义用用户填写的地址，不再改写成 OpenAI 路径。"""
    base = (base_url or "").rstrip("/")
    if is_custom(provider_type):
        return base
    path = suffix if str(suffix).startswith("/") else f"/{suffix}"
    return base + path
