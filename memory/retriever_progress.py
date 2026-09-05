"""Memory retrieval progress payloads for SSE memory_progress events.

Summaries are built from measured runtime fields only — no fixed placeholder copy.
"""
from __future__ import annotations

from typing import Any

GATE_LABELS: dict[str, str] = {
    "short_query_shortcircuit": "极短寒暄且携带会话上下文",
    "ack_shortcut": "确认/致谢类消息",
    "empty_query": "空消息",
    "presearch_empty": "预筛无候选",
    "refine_empty": "精筛后无相关记忆",
    "none": "正常检索",
}

REFINE_PATH_LABELS: dict[str, str] = {
    "full": "LLM 精筛",
    "fast_path": "高置信单条，跳过 LLM 精筛",
    "degrade_pick": "候选不足或精筛不可用，按得分选取",
    "refine_cache": "精筛 cache 命中",
}


def build_progress_payload(
    *,
    stage: str,
    status: str,
    summary: str,
    candidates: int | None = None,
    hit_count: int | None = None,
    gate: str | None = None,
    refine_path: str | None = None,
    elapsed_ms: int | None = None,
    vector_hits: int | None = None,
    fts_hits: int | None = None,
    hits: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "stage": stage,
        "status": status,
        "summary": summary,
    }
    if candidates is not None:
        payload["candidates"] = candidates
    if hit_count is not None:
        payload["hit_count"] = hit_count
    if gate is not None:
        payload["gate"] = gate
    if refine_path is not None:
        payload["refine_path"] = refine_path
    if elapsed_ms is not None:
        payload["elapsed_ms"] = elapsed_ms
    if vector_hits is not None:
        payload["vector_hits"] = vector_hits
    if fts_hits is not None:
        payload["fts_hits"] = fts_hits
    if hits:
        # 面板展开时用；只保留 UI 需要的最小字段，避免 SSE 载荷膨胀 / 泄漏正文
        payload["hits"] = [_compact_hit(h) for h in hits]
    return payload


def _compact_hit(item: dict[str, Any]) -> dict[str, Any]:
    """Compact hit for UI: id/title/summary + relation/seed 供前端点击定位。"""
    out: dict[str, Any] = {
        "id": item.get("id") or item.get("memory_id") or "",
        "title": item.get("title") or "",
        "summary": item.get("summary") or "",
    }
    for key in ("relation", "from_seed",
                "verification_state", "freshness_state", "confidence",
                "selected", "score"):
        val = item.get(key)
        if val is not None and val != "":
            out[key] = val
    return out


def compact_candidates(
    candidates: list[Any],
    *,
    selected_ids: set[str] | frozenset[str] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """把 Candidate / dict 列表压成时间线与 Langfuse 可用的短结构。

    selected_ids 非空时标记精筛选中（selected=True/False）；预筛阶段不传。
    """
    out: list[dict[str, Any]] = []
    for raw in (candidates or [])[: max(0, limit)]:
        if isinstance(raw, dict):
            item = dict(raw)
            mid = item.get("id") or item.get("memory_id") or ""
        else:
            mid = getattr(raw, "memory_id", "") or ""
            score = getattr(raw, "final_score", None)
            item = {
                "id": mid,
                "title": getattr(raw, "title", "") or "",
                "summary": getattr(raw, "summary", "") or "",
                "relation": getattr(raw, "relation", None),
                "from_seed": getattr(raw, "from_seed", None),
                "confidence": getattr(raw, "confidence", None),
                "verification_state": getattr(raw, "verification_state", None),
                "freshness_state": getattr(raw, "freshness_state", None),
                "score": round(float(score), 4) if score else None,
            }
        if selected_ids is not None and mid:
            item["selected"] = mid in selected_ids
        out.append(_compact_hit(item))
    return out


def skip_summary(gate: str, query: str) -> str:
    label = GATE_LABELS.get(gate, gate)
    q = (query or "").strip()
    snippet = q[:40] + ("…" if len(q) > 40 else "")
    return f"跳过记忆检索：{label}（query={snippet!r}）"


def presearch_summary(n_candidates: int, vector_hits: int, fts_hits: int) -> str:
    if n_candidates <= 0:
        return f"Hybrid 预筛 0 条候选（向量 {vector_hits} + FTS {fts_hits}）"
    return (
        f"Hybrid 预筛召回 {n_candidates} 条候选"
        f"（向量 {vector_hits} + FTS {fts_hits}）"
    )


def refine_start_summary(n_picked: int, refine_path: str | None = None) -> str:
    if refine_path == "refine_cache":
        return "精筛结果 cache 命中，沿用已有选取"
    if refine_path == "fast_path":
        return "高置信单条命中，跳过 LLM 精筛"
    if refine_path == "degrade_pick":
        return f"候选 {n_picked} 条，按检索得分直接选取（未调用 LLM 精筛）"
    return f"对 {n_picked} 条候选做 LLM 精筛"


def done_summary(hit_count: int, related_count: int = 0) -> str:
    total = hit_count + related_count
    if total <= 0:
        return "未找到相关记忆，不注入"
    if related_count > 0:
        return f"注入 {hit_count} 条主记忆、{related_count} 条关联记忆"
    return f"注入 {hit_count} 条相关记忆"


def timeline_item(payload: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "memory_stage", **payload}


def upsert_memory_timeline(timeline: list[dict], payload: dict[str, Any]) -> None:
    stage = payload.get("stage")
    for item in reversed(timeline):
        if item.get("kind") == "memory_stage" and item.get("stage") == stage:
            item.update(timeline_item(payload))
            return
    timeline.append(timeline_item(payload))
