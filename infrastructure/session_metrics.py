"""Durable projections for conversation performance metrics."""
from __future__ import annotations

from typing import Any

from infrastructure.timeutil import now_cst


def _num(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _metric_row(row: dict | None) -> dict:
    row = row or {}
    llm_ms = _num(row.get("llm_ms"))
    ttft_ms = _num(row.get("ttft_ms")) if row.get("ttft_ms") is not None else None
    decode_ms = _num(row.get("decode_ms")) if row.get("decode_ms") is not None else None
    output_tokens = _num(row.get("output_tokens"))
    return {
        "llm_ms": llm_ms,
        "ttft_ms": ttft_ms,
        "decode_ms": decode_ms,
        "context_ms": _num(row.get("context_ms")),
        "input_tokens": _num(row.get("input_tokens")),
        "output_tokens": output_tokens,
        "cache_read_tokens": _num(row.get("cache_read_tokens")),
        "cache_write_tokens": _num(row.get("cache_write_tokens")),
        "tool_ms": _num(row.get("tool_ms")),
    }


def record_step(db, *, turn_id: str, step: int, llm_ms: int,
                ttft_ms: int | None, decode_ms: int | None,
                input_tokens: int = 0, output_tokens: int = 0,
                cache_read_tokens: int = 0, cache_write_tokens: int = 0,
                tool_ms: int = 0, context_ms: int = 0) -> None:
    """Insert one idempotent step reading.

    A retry/reconnect must not double count a model call. The unique turn/step
    key makes the projection naturally idempotent if the runtime is resumed.

    context_ms 独立于 ttft_ms/llm_ms：ttft/llm 与 deepseek-harness 同口径
    （只计量 LLM 调用），context_ms 单独承接检索 + 精筛 + prompt 组装耗时。
    """
    db.execute(
        "INSERT INTO agent_step_metrics(turn_id,step,llm_ms,ttft_ms,decode_ms,"
        "input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,tool_ms,"
        "context_ms,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(turn_id,step) DO UPDATE SET llm_ms=excluded.llm_ms,"
        "ttft_ms=excluded.ttft_ms,decode_ms=excluded.decode_ms,"
        "input_tokens=excluded.input_tokens,output_tokens=excluded.output_tokens,"
        "cache_read_tokens=excluded.cache_read_tokens,"
        "cache_write_tokens=excluded.cache_write_tokens,tool_ms=excluded.tool_ms,"
        "context_ms=excluded.context_ms",
        (turn_id, step, _num(llm_ms), ttft_ms, decode_ms, _num(input_tokens),
         _num(output_tokens), _num(cache_read_tokens), _num(cache_write_tokens),
         _num(tool_ms), _num(context_ms),
         now_cst().isoformat(timespec="seconds")),
    )


def add_tool_time(db, *, turn_id: str, step: int, tool_ms: int) -> None:
    db.execute(
        "UPDATE agent_step_metrics SET tool_ms=tool_ms+? WHERE turn_id=? AND step=?",
        (_num(tool_ms), turn_id, step),
    )


def turn_metrics(db, turn_id: str) -> dict:
    rows = db.query_all(
        "SELECT step,llm_ms,ttft_ms,decode_ms,input_tokens,output_tokens,"
        "cache_read_tokens,cache_write_tokens,tool_ms,context_ms "
        "FROM agent_step_metrics WHERE turn_id=? ORDER BY step", (turn_id,))
    readings = [_metric_row(row) for row in rows]
    first_ttft = next((r["ttft_ms"] for r in readings if r["ttft_ms"] is not None), None)
    decode_rows = [r for r in readings if r["decode_ms"] is not None and r["output_tokens"] >= 0]
    decode_ms = sum(_num(r["decode_ms"]) for r in decode_rows)
    output_tokens = sum(r["output_tokens"] for r in decode_rows)
    input_tokens = sum(r["input_tokens"] for r in readings)
    cache_read_tokens = sum(r["cache_read_tokens"] for r in readings)
    cache_write_tokens = sum(r["cache_write_tokens"] for r in readings)
    context_ms = sum(r["context_ms"] for r in readings)
    return {
        "steps": len(readings),
        "llm_ms": sum(r["llm_ms"] for r in readings),
        "tool_ms": sum(r["tool_ms"] for r in readings),
        "ttft_ms": first_ttft,
        "decode_ms": decode_ms,
        "context_ms": context_ms,
        "output_tokens": output_tokens,
        "input_tokens": input_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "tokens_per_second": (output_tokens / (decode_ms / 1000.0)
                               if decode_ms > 0 else None),
    }


def session_metrics(db, session_id: str, *, current_turn_id: str | None = None) -> dict:
    row = db.query_one(
        "SELECT COUNT(DISTINCT t.id) turns, COUNT(m.id) steps,"
        "COALESCE(SUM(m.llm_ms),0) llm_ms, COALESCE(SUM(m.tool_ms),0) tool_ms,"
        "COALESCE(SUM(CASE WHEN m.ttft_ms IS NOT NULL THEN m.ttft_ms ELSE 0 END),0) ttft_ms,"
        "SUM(CASE WHEN m.ttft_ms IS NOT NULL THEN 1 ELSE 0 END) ttft_steps,"
        "COALESCE(SUM(CASE WHEN m.decode_ms IS NOT NULL THEN m.decode_ms ELSE 0 END),0) decode_ms,"
        "COALESCE(SUM(CASE WHEN m.decode_ms IS NOT NULL THEN m.output_tokens ELSE 0 END),0) decode_tokens,"
        "COALESCE(SUM(m.context_ms),0) context_ms "
        "FROM agent_turns t LEFT JOIN agent_step_metrics m ON m.turn_id=t.id "
        "WHERE t.session_id=?", (session_id,)) or {}
    usage = db.query_one(
        "SELECT COALESCE(SUM(m.input_tokens),0) input_tokens,"
        "COALESCE(SUM(m.output_tokens),0) output_tokens,"
        "COALESCE(SUM(m.cache_read_tokens),0) cache_read_tokens,"
        "COALESCE(SUM(m.cache_write_tokens),0) cache_write_tokens "
        "FROM agent_turns t JOIN agent_step_metrics m ON m.turn_id=t.id "
        "WHERE t.session_id=?", (session_id,)) or {}
    # Old sessions created before the runtime metrics migration still have
    # token_usage rows, so keep a compatibility fallback for their counters.
    if not any(_num(usage.get(key)) for key in (
            "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")):
        usage = db.query_one(
            "SELECT COALESCE(SUM(input_tokens),0) input_tokens,"
            "COALESCE(SUM(output_tokens),0) output_tokens,"
            "COALESCE(SUM(cache_read_tokens),0) cache_read_tokens,"
            "COALESCE(SUM(cache_write_tokens),0) cache_write_tokens "
            "FROM token_usage WHERE session_id=? AND source='agent_step'",
            (session_id,)) or usage
    input_tokens = _num(usage.get("input_tokens"))
    cache_read = _num(usage.get("cache_read_tokens"))
    result = {
        "turns": _num(row.get("turns")),
        "steps": _num(row.get("steps")),
        "llm_ms": _num(row.get("llm_ms")),
        "tool_ms": _num(row.get("tool_ms")),
        "ttft_ms": _num(row.get("ttft_ms")),
        "ttft_steps": _num(row.get("ttft_steps")),
        "ttft_average_ms": (_num(row.get("ttft_ms")) / _num(row.get("ttft_steps"))
                            if _num(row.get("ttft_steps")) else None),
        "decode_ms": _num(row.get("decode_ms")),
        "decode_tokens": _num(row.get("decode_tokens")),
        "context_ms": _num(row.get("context_ms")),
        "context_average_ms": (_num(row.get("context_ms")) / _num(row.get("steps"))
                                if _num(row.get("steps")) else None),
        "input_tokens": input_tokens,
        "output_tokens": _num(usage.get("output_tokens")),
        "cache_read_tokens": cache_read,
        "cache_write_tokens": _num(usage.get("cache_write_tokens")),
        "cache_hit_percent": (cache_read / input_tokens * 100 if input_tokens else None),
        "updated_at": now_cst().isoformat(timespec="seconds"),
    }
    if current_turn_id:
        result["current_turn"] = turn_metrics(db, current_turn_id)
    return result
