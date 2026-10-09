"""本地 ComfyUI 无损性能优化：质量门禁、启动参数、工作流安全补丁。"""
from .quality import (
    LOSSLESS_FORBIDDEN_CLASS_TYPES,
    LOSSLESS_FORBIDDEN_MODEL_SUFFIXES,
    apply_lossless_workflow_patches,
    assert_lossless_generation,
    assert_lossless_workflow,
    build_comfyui_launch_extras,
    merge_launch_args,
    normalize_quality_mode,
)

__all__ = [
    "LOSSLESS_FORBIDDEN_CLASS_TYPES",
    "LOSSLESS_FORBIDDEN_MODEL_SUFFIXES",
    "apply_lossless_workflow_patches",
    "assert_lossless_generation",
    "assert_lossless_workflow",
    "build_comfyui_launch_extras",
    "merge_launch_args",
    "normalize_quality_mode",
]
