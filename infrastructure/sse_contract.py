"""Public Server-Sent Events contract for browser and channel clients.

The EventBus is an internal domain-event mechanism.  This module owns the
separate wire protocol emitted by ``POST /api/chat/send`` and prevents either
side of the UI from silently inventing event names.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SSE_CONTRACT_VERSION = "2026-08-21"


class SSEContractError(ValueError):
    """An event cannot be safely sent through the public SSE stream."""


@dataclass(frozen=True)
class SSEEventSpec:
    description: str
    required_fields: frozenset[str] = frozenset()


SSE_EVENT_SPECS: dict[str, SSEEventSpec] = {
    "queued": SSEEventSpec("同会话任务正在排队", frozenset({"session_id"})),
    "error": SSEEventSpec("本轮生成失败或被停止", frozenset({"code", "message"})),
    "memory_retrieved": SSEEventSpec("已选中的记忆摘要", frozenset({"count", "titles"})),
    "thinking_delta": SSEEventSpec("面向用户的处理进度摘要", frozenset({"text"})),
    "mode_decision": SSEEventSpec("思考模式路由结果", frozenset({"requested_mode", "effective_mode", "reason"})),
    "analysis_progress": SSEEventSpec("问题建模或交付阶段进度", frozenset({"stage", "status"})),
    "delivery_progress": SSEEventSpec("长文交付进度", frozenset({"status", "current", "total"})),
    "quality_status": SSEEventSpec("需求覆盖质量检查结果"),
    "tool_executing": SSEEventSpec("工具调用状态", frozenset({"tool_name", "status"})),
    "tool_visual": SSEEventSpec("工具生成的可视化", frozenset({"type", "data"})),
    "content_delta": SSEEventSpec("回复正文增量", frozenset({"text"})),
    "citations": SSEEventSpec("回复引用", frozenset({"refs"})),
    "elicitation": SSEEventSpec("需要用户补充的信息", frozenset({"tool_use_id", "questions"})),
    "elicitation_status": SSEEventSpec("补充信息流程状态", frozenset({"status"})),
    "handoff_ready": SSEEventSpec("会话交接摘要状态", frozenset({"status"})),
    "mood_updated": SSEEventSpec("人格情绪快照", frozenset({"ai_mood"})),
    "turn_completed": SSEEventSpec("本轮持久化完成", frozenset({"message_id"})),
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
