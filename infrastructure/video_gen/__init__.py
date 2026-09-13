"""文生视频：本地 ComfyUI / 云端 Adapter + 统一结果类型。"""
from __future__ import annotations

from infrastructure.image_gen import interrupt_session, probe_comfyui

from .cloud_adapter import KlingVideoAdapter
from .comfyui_adapter import (
    ComfyUIVideoAdapter,
    clamp_duration_sec,
    normalize_size,
)
from .execute import execute_video_gen
from .factory import get_video_adapter
from .profiles import (
    aspect_ratio_of,
    capability_hint,
    clamp_duration,
    normalize_video_size,
    video_profile_for,
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
    "KlingVideoAdapter",
    "VideoGenRequest",
    "VideoGenResult",
    "aspect_ratio_of",
    "capability_hint",
    "clamp_duration",
    "clamp_duration_sec",
    "execute_video_gen",
    "get_video_adapter",
    "interrupt_session",
    "normalize_size",
    "normalize_video_size",
    "probe_comfyui",
    "video_profile_for",
]
