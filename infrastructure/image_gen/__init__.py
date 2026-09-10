"""本地文生图：ComfyUI Adapter + 统一结果类型。"""
from __future__ import annotations

from .active_jobs import interrupt_session
from .comfyui_adapter import ComfyUIAdapter, probe_comfyui
from .types import ALLOWED_SIZES, ImageGenRequest, ImageGenResult

__all__ = [
    "ALLOWED_SIZES",
    "ComfyUIAdapter",
    "ImageGenRequest",
    "ImageGenResult",
    "interrupt_session",
    "probe_comfyui",
]
