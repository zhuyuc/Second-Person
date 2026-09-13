"""本地文生图：ComfyUI Adapter + 统一结果类型。"""
from __future__ import annotations

from .active_jobs import interrupt_session
from .cloud_adapter import OpenAIImageAdapter
from .comfyui_adapter import ComfyUIAdapter, probe_comfyui
from .factory import get_image_adapter
from .types import ALLOWED_SIZES, ImageGenRequest, ImageGenResult, image_capability_hint

__all__ = [
    "ALLOWED_SIZES",
    "ComfyUIAdapter",
    "ImageGenRequest",
    "ImageGenResult",
    "OpenAIImageAdapter",
    "get_image_adapter",
    "image_capability_hint",
    "interrupt_session",
    "probe_comfyui",
]
