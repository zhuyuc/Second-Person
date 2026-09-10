"""本地文生视频：ComfyUI Adapter + 统一结果类型。"""
from __future__ import annotations

from infrastructure.image_gen import interrupt_session, probe_comfyui

from .comfyui_adapter import (
    ComfyUIVideoAdapter,
    clamp_duration_sec,
    normalize_size,
)
from .types import (
    ALLOWED_SIZES,
    DEFAULT_DURATION_SEC,
    DEFAULT_FPS,
    DEFAULT_SIZE,
    MAX_DURATION_SEC,
    MIN_DURATION_SEC,
    VideoGenRequest,
    VideoGenResult,
)

__all__ = [
    "ALLOWED_SIZES",
    "DEFAULT_DURATION_SEC",
    "DEFAULT_FPS",
    "DEFAULT_SIZE",
    "MAX_DURATION_SEC",
    "MIN_DURATION_SEC",
    "ComfyUIVideoAdapter",
    "VideoGenRequest",
    "VideoGenResult",
    "clamp_duration_sec",
    "interrupt_session",
    "normalize_size",
    "probe_comfyui",
]
