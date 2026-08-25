"""Host-owned, provider-neutral summaries for model/tool decisions.

The summary is an explanation of an observable host action.  It deliberately
does not claim to reveal a model's private chain-of-thought.
"""
from __future__ import annotations

from typing import Any


_PURPOSES: tuple[tuple[str, str, str], ...] = (
    ("search", "external_information", "需要外部资料或当前信息"),
    ("fetch", "external_information", "需要读取已定位的外部资料"),
    ("web", "external_information", "需要外部资料或当前信息"),
    ("memory", "memory_context", "需要补充相关历史记忆"),
    ("knowledge", "knowledge_context", "需要补充知识库上下文"),
    ("file_read", "workspace_context", "需要读取工作区文件"),
    ("read_file", "workspace_context", "需要读取工作区文件"),
    ("document", "produce_artifact", "需要生成或处理文档产物"),
    ("file_write", "produce_artifact", "需要写入用户要求的文件产物"),
    ("shell", "execute_system_action", "需要执行工作区系统操作"),
    ("calculator", "deterministic_computation", "需要确定性计算"),
    ("datetime", "deterministic_computation", "需要获取确定性时间信息"),
)


def tool_purpose(tool_name: str, description: str = "") -> tuple[str, str] | None:
    """Return a stable reason code and user-facing summary for known tools."""
    haystack = f"{tool_name} {description}".lower()
    for marker, reason_code, summary in _PURPOSES:
        if marker in haystack:
            return reason_code, summary
    return None


def argument_preview(arguments: dict[str, Any] | None, limit: int = 240) -> dict[str, Any]:
    """Keep argument evidence small and structured for logs/UI."""
    args = arguments or {}
    preview: dict[str, Any] = {}
    for key, value in list(args.items())[:12]:
        text = str(value)
        preview[str(key)] = text if len(text) <= limit else text[:limit] + "..."
    return preview


def build_tool_decision_notice(*, tool_name: str, description: str = "",
                               arguments: dict[str, Any] | None = None,
                               step: int = 0, call_id: str | None = None,
                               repeated_count: int = 0) -> dict[str, Any] | None:
    """Build a factual decision notice, or ``None`` for unknown tools.

    ``actor`` and ``source`` make ownership explicit in both SSE and durable
    event records so the UI can distinguish host inference from model output.
    """
    purpose = tool_purpose(tool_name, description)
    if purpose is None:
        return None
    reason_code, summary = purpose
    if repeated_count:
        summary = f"{summary}；该工具已连续调用 {repeated_count} 次，宿主提醒模型检查进展"
    return {
        "stage": "tool_selection",
        "actor": "host",
        "source": "host_inferred",
        "reason_code": reason_code,
        "summary": summary,
        "tool_name": tool_name,
        "step": step,
        "call_id": call_id,
        "arguments": argument_preview(arguments),
        "repeated_count": repeated_count,
    }
