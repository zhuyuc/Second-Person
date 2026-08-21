"""跨前后端共享的对话请求语义。"""
from __future__ import annotations

from typing import Any

THINK_MODES = frozenset(("auto", "quick", "deep"))


def normalize_think_mode(value: Any) -> str:
    """把外部请求值收敛为唯一的三态语义。"""
    return value if isinstance(value, str) and value in THINK_MODES else "auto"
