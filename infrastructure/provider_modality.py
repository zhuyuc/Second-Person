"""Provider 用途模态（text/image/video）与协议组合校验。"""
from __future__ import annotations

import re

MODALITY_TEXT = "text"
MODALITY_IMAGE = "image"
MODALITY_VIDEO = "video"
MODALITIES = (MODALITY_TEXT, MODALITY_IMAGE, MODALITY_VIDEO)

# 文本可含 Anthropic；图/视频云端不含 Anthropic（无对等生图/生视频接口）。
# OpenAI 兼容自动补标准路径；自定义对常见 /vN 与叶子路径智能派生。
# 图和视频另有本地 ComfyUI。kling / dashscope 只留给已经保存的旧记录。
TEXT_PROTOCOLS = frozenset({"openai_compatible", "anthropic", "custom"})
MEDIA_CLOUD_PROTOCOLS = frozenset({"openai_compatible", "custom"})
CLOUD_PROTOCOLS = MEDIA_CLOUD_PROTOCOLS  # 图/视频云端（历史别名）
OPENAI_WIRE = frozenset({"openai_compatible"})
LEGACY_VIDEO = frozenset({"kling", "dashscope"})

PROTOCOLS_BY_MODALITY: dict[str, frozenset[str]] = {
    MODALITY_TEXT: TEXT_PROTOCOLS,
    MODALITY_IMAGE: MEDIA_CLOUD_PROTOCOLS | frozenset({"comfyui"}),
    MODALITY_VIDEO: MEDIA_CLOUD_PROTOCOLS | frozenset({"comfyui"}) | LEGACY_VIDEO,
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

# Anthropic 无 embedding / 生图 / 生视频对等接口，禁止绑这些槽位
ANTHROPIC_DENIED_SLOTS = frozenset({"embedding", "image_gen", "video_gen"})

_CUSTOM_KNOWN_LEAVES = (
    "/chat/completions",
    "/embeddings",
    "/images/generations",
    "/models",
)
_V_ROOT_RE = re.compile(r"/v\d+$", re.IGNORECASE)


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


def validate_slot_provider(task_type: str, provider_type: str) -> None:
    """槽位 × 协议门禁（比模态组合更细：同为 text，embedding 仍拒 Anthropic）。"""
    ptype = (provider_type or "").strip().lower()
    slot = (task_type or "").strip()
    if ptype == "anthropic" and slot in ANTHROPIC_DENIED_SLOTS:
        raise ValueError(
            "Anthropic 仅支持对话类槽位，不能用于 embedding / 文生图 / 文生视频")


def slot_modality(slot_key: str) -> str:
    return SLOT_MODALITY.get(slot_key, MODALITY_TEXT)


def is_local_gpu(snap) -> bool:
    ptype = (getattr(snap, "provider_type", "") or "").strip().lower()
    return ptype == "comfyui"


def is_openai_wire(provider_type: str) -> bool:
    return (provider_type or "").strip().lower() in OPENAI_WIRE


def is_custom(provider_type: str) -> bool:
    return (provider_type or "").strip().lower() == "custom"


def _custom_api_root(base: str) -> str | None:
    """自定义地址可派生 OpenAI 子路径时返回 API 根；否则 None（保持原样）。

    可派生：
    - 以 ``/vN`` 结尾（如 ``…/v1``）
    - 以常见叶子结尾（``/chat/completions`` 等）→ 剥叶子后的根
    """
    b = (base or "").rstrip("/")
    if not b:
        return None
    lower = b.lower()
    for leaf in _CUSTOM_KNOWN_LEAVES:
        if lower.endswith(leaf):
            return b[: -len(leaf)].rstrip("/") or None
    if _V_ROOT_RE.search(b):
        return b
    return None


def endpoint_url(base_url: str, provider_type: str, suffix: str) -> str:
    """拼出最终请求 URL。

    - OpenAI 兼容：``base + suffix``
    - 自定义：若像 ``/vN`` 或常见叶子，则智能派生；否则原样使用填写地址
    """
    base = (base_url or "").rstrip("/")
    path = suffix if str(suffix).startswith("/") else f"/{suffix}"
    if is_custom(provider_type):
        if base.lower().endswith(path.lower()):
            return base
        root = _custom_api_root(base)
        if root:
            return root + path
        return base
    return base + path


def anthropic_messages_url(base_url: str) -> str:
    """Anthropic Messages 完整地址。

    官方 Anthropic、火山 Agent Plan（/api/plan）、Coding Plan（/api/coding）
    均走 ``{base}/v1/messages``。若用户已把 ``/v1`` 写进 Base URL，不再重复拼接。
    """
    base = (base_url or "").rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/messages"
    return f"{base}/v1/messages"


def anthropic_headers(api_key: str) -> dict[str, str]:
    """Anthropic 兼容鉴权头。

    - 火山方舟 Agent/Coding Plan 等网关按 Claude Code 约定吃 ``Authorization: Bearer``
    - 原生 Anthropic 吃 ``x-api-key``
    两边同时带上，避免兼容端点 401。
    """
    key = api_key or ""
    return {
        "Authorization": f"Bearer {key}",
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
