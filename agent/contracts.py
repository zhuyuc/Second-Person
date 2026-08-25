"""跨前后端共享的对话请求语义。"""
from __future__ import annotations

from typing import Any

REASONING_EFFORTS = frozenset(("off", "low", "high", "max"))
DEFAULT_REASONING_EFFORT = "high"

def normalize_reasoning_effort(value: Any) -> str:
    """Normalize the public four-level reasoning contract."""
    return value if isinstance(value, str) and value in REASONING_EFFORTS else DEFAULT_REASONING_EFFORT
