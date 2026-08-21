"""Structured developer-facing reasoning records.

The payload records verifiable decisions and execution evidence.  It never
stores a model's raw hidden-reasoning token stream, system prompt, or complete
memory contents.  Langfuse spans retain the detailed operational trace.
"""
from __future__ import annotations

from typing import Any

DEVELOPER_TRACE_SCHEMA_VERSION = "developer-trace-v1"


def initial_developer_trace(*, requested_mode: str,
                            client_request_id: str | None) -> dict[str, Any]:
    return {
        "schema_version": DEVELOPER_TRACE_SCHEMA_VERSION,
        "status": "running",
        "request": {
            "requested_mode": requested_mode,
            "client_request_id": client_request_id,
        },
    }


def build_developer_trace(*, requested_mode: str, effective_mode: str,
                          route_reason: str, history_messages: int,
                          raw_rounds: int, compressed: bool,
                          memories: list[dict], retrieval_diagnostics: dict,
                          intents: list[Any], tool_results: list[dict],
                          problem_model: Any, quality_report: Any,
                          delivery_job_id: str | None, latency_ms: int,
                          llm_call_count: int, model_id: str | None) -> dict[str, Any]:
    """Build a compact, inspectable trace snapshot for Langfuse metadata."""
    selected_memories = [
        {
            "id": str(memory.get("id", "")),
            "title": str(memory.get("title", ""))[:120],
            "source_type": str(memory.get("source_type", "")),
            "confidence": str(memory.get("confidence", "")),
        }
        for memory in (memories or [])[:30]
    ]
    execution = [
        {
            "tool": str(result.get("tool", "")),
            "ok": bool(result.get("ok")),
            "deferred": bool(result.get("deferred")),
            "error": str(result.get("error", ""))[:240] or None,
        }
        for result in (tool_results or [])[:40]
    ]
    trace: dict[str, Any] = {
        "schema_version": DEVELOPER_TRACE_SCHEMA_VERSION,
        "status": "completed",
        "route": {
            "requested_mode": requested_mode,
            "effective_mode": effective_mode,
            "reason": (route_reason or "")[:240],
        },
        "context": {
            "history_messages": history_messages,
            "raw_rounds": raw_rounds,
            "compressed": bool(compressed),
            "selected_memories": selected_memories,
            "retrieval": {
                key: retrieval_diagnostics[key]
                for key in ("gate", "degraded", "vector_hits", "fts_hits",
                            "refined_count", "retrieval_time_ms", "context_chars")
                if key in (retrieval_diagnostics or {})
            },
        },
        "execution": {
            "intent_types": [
                str(getattr(intent, "intent_type", ""))
                for intent in (intents or [])[:20]
            ],
            "tools": execution,
        },
        "quality": (quality_report.safe_summary()
                    if quality_report is not None else None),
        "delivery": {"job_id": delivery_job_id} if delivery_job_id else None,
        "outcome": {
            "latency_ms": latency_ms,
            "llm_call_count": llm_call_count,
            "model_id": model_id,
        },
    }
    if problem_model is not None:
        trace["problem_model"] = problem_model.safe_summary()
    return trace
