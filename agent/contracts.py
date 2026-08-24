"""跨前后端共享的对话请求语义。"""
from __future__ import annotations

from typing import Any

REASONING_EFFORTS = frozenset(("off", "low", "high", "max"))
DEFAULT_REASONING_EFFORT = "high"

# 仅用于兼容仍在传递旧字段的客户端；正常运行时不再根据意图把请求
# 分流为“快速/深度”两条链路。
THINK_MODES = frozenset(("auto", "quick", "deep"))


def normalize_reasoning_effort(value: Any) -> str:
    """Normalize the public four-level reasoning contract."""
    return value if isinstance(value, str) and value in REASONING_EFFORTS else DEFAULT_REASONING_EFFORT


def normalize_think_mode(value: Any) -> str:
    """把外部请求值收敛为唯一的三态语义。"""
    return value if isinstance(value, str) and value in THINK_MODES else "auto"


def legacy_think_mode_effort(value: Any) -> str:
    """Map retired request values without retaining the old routing behavior."""
    return {"quick": "low", "deep": "max", "auto": DEFAULT_REASONING_EFFORT}.get(
        normalize_think_mode(value), DEFAULT_REASONING_EFFORT)
