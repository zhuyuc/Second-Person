"""Advisory repeat-tool guard inspired by DeepSeek Harness."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


@dataclass(frozen=True)
class RepeatReminder:
    tool_name: str
    count: int
    threshold: int
    key: str

    @property
    def message(self) -> str:
        return (f"宿主提醒：工具 {self.tool_name} 已使用相同参数连续调用 {self.count} 次。"
                "请检查已有结果，必要时调整参数或直接给出阶段性结论。")


class RepeatToolGuard:
    """Count exact (tool, canonical arguments) chains and emit advisory reminders."""

    def __init__(self, thresholds: list[int] | tuple[int, ...] = (3, 5, 8)) -> None:
        if isinstance(thresholds, str):
            try:
                thresholds = json.loads(thresholds)
            except (TypeError, ValueError):
                thresholds = (3, 5, 8)
        self.thresholds = tuple(sorted({int(v) for v in thresholds if int(v) > 0})) or (3,)
        self._last_key: str | None = None
        self._count = 0
        self._fired: set[int] = set()

    def reset(self) -> None:
        self._last_key = None
        self._count = 0
        self._fired.clear()

    def observe(self, tool_name: str, arguments: dict[str, Any] | None) -> RepeatReminder | None:
        key = f"{tool_name}:{canonical_json(arguments or {})}"
        if key == self._last_key:
            self._count += 1
        else:
            self._last_key = key
            self._count = 1
            self._fired.clear()
        threshold = next((item for item in self.thresholds
                          if self._count >= item and item not in self._fired), None)
        if threshold is None:
            return None
        self._fired.add(threshold)
        return RepeatReminder(tool_name, self._count, threshold, key)
