"""Tool-call execution loop extracted from TurnRuntime."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Awaitable, Callable

from .decision_summary import build_tool_decision_notice
from .repeat_tool_guard import RepeatToolGuard
from .turn_events import TurnEventStore
from .turn_runtime_helpers import extract_web_citations


class TurnToolRunner:
    def __init__(self, *, registry, executor, events: TurnEventStore) -> None:
        self.registry = registry
        self.executor = executor
        self.events = events

    async def run_tool_calls(self, turn_id: str, step: int, tool_calls: list[dict],
                             emit: Callable[[str, dict], Awaitable[None]],
                             repeat_guard: RepeatToolGuard) -> list[dict]:
        async def run_one(raw: dict, index: int) -> dict:
            function = raw.get("function") or {}
            name = function.get("name") or raw.get("name") or ""
            call_id = raw.get("id") or f"call_{step}_{index}_{uuid.uuid4().hex[:8]}"
            raw_args = function.get("arguments", raw.get("arguments", {}))
            try:
                params = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args or {})
            except (TypeError, ValueError):
                params = {}
            self.events.append(turn_id, "tool.call", actor="model", step=step, call_id=call_id,
                               payload={"tool_name": name, "params": params})
            tool = self.registry.get(name)
            spec = tool.spec if tool is not None else None
            notice = build_tool_decision_notice(
                tool_name=name, description=spec.description if spec else "",
                arguments=params, step=step, call_id=call_id)
            if notice is not None:
                self.events.append(turn_id, "decision.notice", actor="host", step=step,
                                   call_id=call_id, payload=notice)
                await emit("decision_notice", notice)
            reminder = repeat_guard.observe(name, params)
            if reminder is not None:
                repeat_notice = build_tool_decision_notice(
                    tool_name=name, description=spec.description if spec else "",
                    arguments=params, step=step, call_id=call_id,
                    repeated_count=reminder.count)
                if repeat_notice is not None:
                    self.events.append(turn_id, "decision.notice", actor="host", step=step,
                                       call_id=call_id, payload=repeat_notice)
                    await emit("decision_notice", repeat_notice)
                self.events.append(turn_id, "context.notice", actor="host", step=step,
                                   call_id=call_id, model_visible=True,
                                   payload={"content": reminder.message,
                                            "reason": "repeat_tool_call",
                                            "tool_name": name, "count": reminder.count})
            if tool is None:
                return await self.record_result(turn_id, step, call_id, name, False,
                                                "工具不存在", emit)
            try:
                args_preview = json.dumps(params, ensure_ascii=False, default=str)[:200]
            except Exception:  # noqa: BLE001
                args_preview = ""
            await emit("tool_executing", {"tool_name": name, "status": "running",
                                            "turn_id": turn_id, "call_id": call_id,
                                            "arguments": args_preview})
            turn = self.events.get_turn(turn_id) or {}
            tool_started_at = time.perf_counter()
            result = await self.executor.execute_tool(name, params, emit=emit,
                                                      session_id=turn.get("session_id", ""))
            duration_ms = max(0, round((time.perf_counter() - tool_started_at) * 1000))
            if result.get("ok"):
                recorded = await self.record_result(
                    turn_id, step, call_id, name, True,
                    result.get("result"), emit, arguments=params)
            else:
                recorded = await self.record_result(
                    turn_id, step, call_id, name, False,
                    result.get("error") or "工具执行失败", emit,
                    arguments=params)
            recorded["_duration_ms"] = duration_ms
            return recorded

        resolved = [self.registry.get((raw.get("function") or {}).get("name") or raw.get("name", ""))
                    for raw in tool_calls]
        if all(tool is not None and tool.spec.parallel_safe for tool in resolved):
            return await asyncio.gather(*(run_one(raw, i) for i, raw in enumerate(tool_calls, 1)))
        return [await run_one(raw, i) for i, raw in enumerate(tool_calls, 1)]

    async def record_result(self, turn_id: str, step: int, call_id: str, name: str,
                            ok: bool, result: Any,
                            emit: Callable[[str, dict], Awaitable[None]],
                            arguments: Any = None) -> dict:
        content = json.dumps(result, ensure_ascii=False, default=str) if not isinstance(result, str) else result
        content = content[:12000]
        self.events.append(turn_id, "tool.result", actor="tool", step=step, call_id=call_id,
                           model_visible=True, payload={"tool_name": name, "ok": ok,
                                                        "model_content": content})
        payload: dict[str, Any] = {
            "turn_id": turn_id, "call_id": call_id,
            "tool_name": name, "ok": ok,
            "summary": content[:500],
        }
        if ok:
            cites = extract_web_citations(name, result, arguments)
            if cites:
                payload["citations"] = cites
        await emit("tool_result", payload)
        # 图形工具执行成功 → 发射 tool_visual 事件供前端 DiagramRenderer 渲染。
        # （render_flowchart/render_mermaid 产出 {type, ...}；前端据 type 选 SVG/Mermaid）
        if ok and name in ("render_flowchart", "render_mermaid") \
                and isinstance(result, dict) and result.get("type"):
            await emit("tool_visual", {"type": result["type"], "data": result})
        return {"tool": name, "ok": ok, "result": result if ok else None,
                "error": None if ok else content}
