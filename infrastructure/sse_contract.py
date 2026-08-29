"""Public Server-Sent Events contract for browser and channel clients.

The EventBus is an internal domain-event mechanism.  This module owns the
separate wire protocol emitted by ``POST /api/chat/send`` and prevents either
side of the UI from silently inventing event names.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SSE_CONTRACT_VERSION = "2026-08-24"


class SSEContractError(ValueError):
    """An event cannot be safely sent through the public SSE stream."""


@dataclass(frozen=True)
class SSEEventSpec:
    description: str
    required_fields: frozenset[str] = frozenset()


SSE_EVENT_SPECS: dict[str, SSEEventSpec] = {
    "queued": SSEEventSpec("同会话任务正在排队", frozenset({"session_id"})),
    "error": SSEEventSpec("本轮生成失败或被停止", frozenset({"code", "message"})),
    "reasoning_delta": SSEEventSpec("Provider 返回的原生 reasoning 增量", frozenset({"text", "source"})),
    "decision_notice": SSEEventSpec("宿主推断的可验证决策摘要", frozenset({"stage", "actor", "source", "reason_code", "summary"})),
    "turn_started": SSEEventSpec("持久化任务轮次已创建", frozenset({"turn_id", "reasoning_effort"})),
    "step_started": SSEEventSpec("模型/工具循环的新步骤", frozenset({"turn_id", "step"})),
    "tool_executing": SSEEventSpec("工具调用状态", frozenset({"tool_name", "status"})),
    "tool_result": SSEEventSpec("工具执行结果摘要", frozenset({"turn_id", "tool_name", "ok"})),
    "tool_visual": SSEEventSpec("工具生成的可视化", frozenset({"type", "data"})),
    "content_delta": SSEEventSpec("回复正文增量", frozenset({"text"})),
    "citations": SSEEventSpec("回复引用", frozenset({"refs"})),
    "handoff_ready": SSEEventSpec("会话交接摘要状态", frozenset({"status"})),
    "mood_updated": SSEEventSpec("人格情绪快照", frozenset({"ai_mood"})),
    "turn_completed": SSEEventSpec("本轮持久化完成", frozenset({"message_id"})),
    "step_metrics": SSEEventSpec("多步 turn 的步边界指标刷新", frozenset({"turn_id", "step"})),
}

SSE_TERMINAL_EVENTS = frozenset({"turn_completed", "error"})


def validate_sse_event(event: str, data: Any) -> dict[str, Any]:
    """Validate the wire envelope while preserving domain-specific fields."""
    spec = SSE_EVENT_SPECS.get(event)
    if spec is None:
        raise SSEContractError(f"unknown SSE event: {event}")
    if not isinstance(data, dict):
        raise SSEContractError(f"SSE event {event} data must be an object")
    missing = spec.required_fields.difference(data)
    if missing:
        fields = ", ".join(sorted(missing))
        raise SSEContractError(f"SSE event {event} missing fields: {fields}")
    return data
