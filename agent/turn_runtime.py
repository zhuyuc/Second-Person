"""A short, event-sourced model/tool loop for normal agent turns."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Awaitable, Callable

from infrastructure.developer_trace import build_agent_trace
from langfuse.integration import get_tracer
from .decision_summary import build_tool_decision_notice
from .repeat_tool_guard import RepeatToolGuard
from .turn_events import TurnEventStore
from .prompt_assembler import PROMPT_VERSION, ToolPromptBuilder

logger = logging.getLogger("second_person.turn_runtime")


class TurnRuntime:
    def __init__(self, *, db, config, sessions, registry, executor, llm,
                 providers, tool_policy, system_prompt: Callable[..., str],
                 context_loader: Callable[..., Awaitable[dict[str, Any]]],
                 persist_images: Callable[[list[str] | None], list[str] | None] | None = None,
                 tool_prompt_builder: ToolPromptBuilder | None = None) -> None:
        self.db = db
        self.config = config
        self.sessions = sessions
        self.registry = registry
        self.executor = executor
        self.llm = llm
        self.providers = providers
        self.policy = tool_policy
        self.events = TurnEventStore(db)
        self.system_prompt = system_prompt
        self.context_loader = context_loader
        self.persist_images = persist_images
        self.tool_prompts = tool_prompt_builder or ToolPromptBuilder(registry, config)

    async def run(self, *, session_id: str, message: str, reasoning_effort: str,
                  emit: Callable[[str, dict], Awaitable[None]],
                  client_request_id: str | None = None, images: list[str] | None = None,
                  location: str | None = None, onboarding: bool = False,
                  persist_user: bool = True, user_parent_id: int | None = None,
                  user_version_group_id: int | None = None,
                  assistant_parent_id: int | None = None,
                  assistant_version_group_id: int | None = None,
                  handoff_path: str | None = None) -> dict[str, Any]:
        max_steps = self.config.get("agent_max_steps", 8)
        tracer = get_tracer()
        trace = tracer.trace_start(
            "agent.turn", session_id=session_id,
            input={"message_chars": len(message), "images": len(images or [])},
            metadata={"request_id": client_request_id, "reasoning_effort": reasoning_effort,
                      "contract_version": "v2"})
        turn = self.events.start_turn(session_id, reasoning_effort=reasoning_effort,
                                      max_steps=max_steps, request_id=client_request_id,
                                      langfuse_trace_id=getattr(trace, "id", None))
        turn_id = turn["id"]
        started = time.monotonic()
        calls = 0
        thinking_parts: list[str] = []
        reasoning_parts: list[str] = []
        system_parts: list[str] = []
        decision_notices: list[dict[str, Any]] = []
        tool_events: list[dict[str, Any]] = []
        repeat_guard = RepeatToolGuard(self.config.get("repeat_tool_thresholds", (3, 5, 8)))
        reasoning_source = "none"

        async def runtime_emit(event: str, data: dict) -> None:
            """Broadcast events and retain a coherent thinking summary with the reply.

            Two lanes end up in the persisted thinking panel:
            - Model reasoning (reasoning_delta text from stream_chat reasoning
              chunks) — user-facing "what the model is chewing on".
            - Tool lifecycle labels — user-facing "what actions are firing".
            Turn/step scaffolding is not surfaced: it is engineering telemetry
            with zero signal for a normal reader.
            """
            labels = {
                "tool_executing": f"【工具】正在执行 {data.get('tool_name', '')}\n",
                "tool_result": f"【工具】{data.get('tool_name', '')}"
                               f"{'已完成' if data.get('ok') else '未完成'}\n",
                "tool_pending_approval": f"【工具】等待确认：{data.get('tool_name', '')}\n",
                "tool_blocked": f"【工具】已拦截：{data.get('tool_name', '')}\n",
            }
            text = labels.get(event)
            if text:
                thinking_parts.append(text)
                tool_events.append({"event": event, **data})
            elif event == "decision_notice":
                decision_notices.append(dict(data))
                decision_span = tracer.span_start("agent.decision", input={
                    "stage": data.get("stage"), "reason_code": data.get("reason_code"),
                    "tool_name": data.get("tool_name")},
                    metadata={"actor": data.get("actor"), "source": data.get("source")})
                decision_span.end(output={"summary_chars": len(data.get("summary", ""))})
            await emit(event, data)

        try:
            persisted_images = None
            if persist_user and images and self.persist_images:
                persisted_images = await asyncio.to_thread(self.persist_images, images)
            user_message_id = (self.sessions.append_message(
                session_id, "user", message, images=persisted_images,
                parent_id=user_parent_id, version_group_id=user_version_group_id)
                if persist_user else None)
            self.events.append(turn_id, "user.message", actor="user", model_visible=True,
                               payload={"content": message, "message_id": user_message_id})
            await runtime_emit("turn_started", {"turn_id": turn_id,
                                                  "reasoning_effort": reasoning_effort,
                                                  "max_steps": max_steps})
            for step in range(1, max_steps + 1):
                self.events.set_status(turn_id, "running", step=step)
                self.events.append(turn_id, "step.started", actor="host", step=step,
                                   payload={"reasoning_effort": reasoning_effort})
                await runtime_emit("step_started", {"turn_id": turn_id, "step": step})
                context_span = tracer.span_start("context.assemble", input={
                    "turn_id": turn_id, "step": step})
                context = await self.context_loader(session_id=session_id, turn_id=turn_id,
                                                    message=message, onboarding=onboarding,
                                                    step=step, handoff_path=handoff_path)
                context_span.end(output={"history_messages": len(context["history"]),
                                         "memories": context.get("memory_count", 0)})
                snap = context["snap"]
                effective_effort = reasoning_effort
                supported_efforts = tuple(getattr(snap, "reasoning_efforts", ()) or ())
                if supported_efforts and effective_effort not in supported_efforts:
                    effective_effort = "off" if "off" in supported_efforts else supported_efforts[0]
                    notice = {
                        "stage": "model_selection", "actor": "host", "source": "capability_catalog",
                        "reason_code": "unsupported_reasoning_effort",
                        "summary": f"当前模型不支持 {reasoning_effort}，已降级为 {effective_effort}",
                        "requested_effort": reasoning_effort, "effective_effort": effective_effort,
                    }
                    await runtime_emit("decision_notice", notice)
                prompt = [{"role": "system", "content": self.system_prompt(
                    onboarding, location, session_id, context.get("dynamic_blocks"))}]
                prompt += context["history"] + self.events.model_messages(turn_id)
                tools = self._project_tools(message, step)
                self.events.append(turn_id, "request.header", actor="host", step=step,
                                   payload={"model_id": snap.model_id,
                                            "reasoning_effort": effective_effort,
                                            "tool_names": [t["function"]["name"] for t in tools],
                                            "history_count": len(prompt),
                                            "prompt_version": PROMPT_VERSION}, model_visible=False)
                step_span = tracer.span_start("agent.step", input={"turn_id": turn_id, "step": step},
                                              metadata={"reasoning_effort": reasoning_effort})
                content_parts: list[str] = []
                tool_calls: list[dict] = []
                try:
                    # 流式：内容增量边收边发 content_delta，首字延迟由整段生成
                    # 时间降到首 chunk 到达时间；tool_calls 在流内累积，末尾 done
                    # 事件同时返回内容与工具调用，与非流式契约等价。
                    async for kind, data in self.llm.stream_chat(
                            snap, prompt, source="agent_step",
                            session_id=session_id, tools=tools,
                            images=images if step == 1 else None,
                            extra_body={"reasoning_effort": effective_effort}):
                        if kind == "content":
                            content_parts.append(data)
                            await emit("content_delta", {"text": data})
                        elif kind == "reasoning":
                            reasoning_source = "provider"
                            reasoning_parts.append(data)
                            await emit("reasoning_delta", {"text": data, "source": "provider"})
                        elif kind == "done":
                            tool_calls = data.get("tool_calls") or []
                            break
                    calls += 1
                finally:
                    step_span.end()
                if not tool_calls:
                    content = "".join(content_parts)
                    analysis_metadata = self._analysis_metadata(
                        turn_id=turn_id, reasoning_effort=reasoning_effort,
                        reasoning_parts=reasoning_parts, system_parts=system_parts,
                        tool_events=tool_events, decision_notices=decision_notices,
                        reasoning_source=reasoning_source, end_reason="final_answer")
                    msg_id = self.sessions.append_message(
                        session_id, "assistant", content,
                        thinking="".join(thinking_parts) or None,
                        analysis_metadata=analysis_metadata,
                        parent_id=assistant_parent_id,
                        version_group_id=assistant_version_group_id)
                    self.events.append(turn_id, "assistant.message", actor="model", step=step,
                                       model_visible=True, payload={"content": content,
                                                                    "message_id": msg_id})
                    self.events.append(turn_id, "step.finished", actor="host", step=step,
                                       payload={"outcome": "final"})
                    self.events.finish(turn_id, status="completed", end_reason="final_answer", step=step)
                    trace.update(output={"message_id": msg_id}, metadata={
                        "provider_capabilities": {
                            "model_id": snap.model_id,
                            "reasoning_efforts": list(getattr(snap, "reasoning_efforts", ()) or ()),
                            "native_reasoning": bool(getattr(snap, "native_reasoning", False)),
                            "reasoning_received": bool(reasoning_parts),
                        }, "developer_trace": build_agent_trace(
                        turn_id=turn_id, reasoning_effort=reasoning_effort, steps=step,
                        llm_call_count=calls, latency_ms=int((time.monotonic() - started) * 1000),
                        end_reason="final_answer")})
                    await runtime_emit("turn_completed", {"message_id": msg_id, "turn_id": turn_id,
                                                           "reasoning_effort": reasoning_effort,
                                                           "analysis_metadata": analysis_metadata})
                    return {"turn_id": turn_id, "message_id": msg_id, "content": content}
                self.events.append(turn_id, "assistant.tool_calls", actor="model", step=step,
                                   model_visible=True, payload={"content": "".join(content_parts),
                                                                "tool_calls": tool_calls})
                results = await self._run_tool_calls(turn_id, step, tool_calls, runtime_emit,
                                                     repeat_guard)
                self.events.append(turn_id, "step.finished", actor="host", step=step,
                                   payload={"outcome": "tool_calls", "count": len(results)})
            content = "任务已达到最大执行步骤，以下是已完成的结果。"
            msg_id = self.sessions.append_message(
                session_id, "assistant", content,
                thinking="".join(thinking_parts) or None,
                analysis_metadata=self._analysis_metadata(
                    turn_id=turn_id, reasoning_effort=reasoning_effort,
                    reasoning_parts=reasoning_parts, system_parts=system_parts,
                    tool_events=tool_events, decision_notices=decision_notices,
                    reasoning_source=reasoning_source, end_reason="max_steps"),
                parent_id=assistant_parent_id,
                version_group_id=assistant_version_group_id)
            self.events.append(turn_id, "assistant.message", actor="host", step=max_steps,
                               model_visible=True, payload={"content": content, "message_id": msg_id})
            self.events.finish(turn_id, status="completed", end_reason="max_steps", step=max_steps)
            await runtime_emit("content_delta", {"text": content})
            analysis_metadata = self._analysis_metadata(
                turn_id=turn_id, reasoning_effort=reasoning_effort,
                reasoning_parts=reasoning_parts, system_parts=system_parts,
                tool_events=tool_events, decision_notices=decision_notices,
                reasoning_source=reasoning_source, end_reason="max_steps")
            await runtime_emit("turn_completed", {"message_id": msg_id, "turn_id": turn_id,
                                                   "reasoning_effort": reasoning_effort,
                                                   "analysis_metadata": analysis_metadata})
            return {"turn_id": turn_id, "message_id": msg_id, "content": content}
        except asyncio.CancelledError:
            self.events.finish(turn_id, status="cancelled", end_reason="cancelled", step=0)
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("Agent turn failed: %s", turn_id)
            self.events.finish(turn_id, status="failed", end_reason="error", step=0,
                               payload={"error": str(exc)[:500]})
            trace.update(level="ERROR", status_message=str(exc)[:500])
            raise
        finally:
            trace.end()

    @staticmethod
    def _analysis_metadata(*, turn_id: str, reasoning_effort: str,
                           reasoning_parts: list[str], system_parts: list[str],
                           tool_events: list[dict[str, Any]],
                           decision_notices: list[dict[str, Any]],
                           reasoning_source: str, end_reason: str) -> dict[str, Any]:
        """Persist structured, safe display lanes instead of one mixed string."""
        return {
            "schema_version": "agent-analysis-v1",
            "turn_id": turn_id,
            "reasoning_effort": reasoning_effort,
            "reasoning_source": reasoning_source,
            "reasoning_available": bool(reasoning_parts),
            "reasoning_text": "".join(reasoning_parts)[:12000],
            "system_progress": "".join(system_parts)[:12000],
            "tool_events": tool_events[-80:],
            "decision_notices": decision_notices[-40:],
            "end_reason": end_reason,
        }

    def _project_tools(self, message: str, step: int) -> list[dict]:
        """Project only tools relevant to the current request."""
        return self.tool_prompts.schemas(message, step)

    async def _run_tool_calls(self, turn_id: str, step: int, tool_calls: list[dict],
                              emit, repeat_guard: RepeatToolGuard) -> list[dict]:
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
                return await self._record_result(turn_id, step, call_id, name, False,
                                                 "工具不存在", emit)
            decision = self.policy.inspect(tool, params, turn_id=turn_id, call_id=call_id)
            if decision.action == "block":
                self.events.append(turn_id, "tool.blocked", actor="host", step=step, call_id=call_id,
                                   model_visible=True, payload={"tool_name": name,
                                                                "reason": decision.reason})
                await emit("tool_blocked", {"turn_id": turn_id, "call_id": call_id,
                                              "tool_name": name, "reason": decision.reason})
                return {"tool": name, "ok": False, "error": decision.reason}
            if decision.action == "approval":
                self.events.set_status(turn_id, "awaiting_approval", step=step)
                self.events.append(turn_id, "tool.approval_requested", actor="host", step=step,
                                   call_id=call_id, payload={"approval_id": decision.approval_id,
                                                              "tool_name": name, "params": params,
                                                              "risk_level": decision.risk_level})
                await emit("tool_pending_approval", {"turn_id": turn_id, "call_id": call_id,
                                                       "approval_id": decision.approval_id,
                                                       "tool_name": name, "params": params,
                                                       "risk_level": decision.risk_level})
                if not await self.policy.wait(decision.approval_id or ""):
                    return await self._record_result(turn_id, step, call_id, name, False,
                                                     "用户拒绝或确认已过期", emit)
                self.events.set_status(turn_id, "running", step=step)
            await emit("tool_executing", {"tool_name": name, "status": "running",
                                            "turn_id": turn_id, "call_id": call_id})
            turn = self.events.get_turn(turn_id) or {}
            result = await self.executor.execute_tool(name, params, emit=emit,
                                                      session_id=turn.get("session_id", ""))
            if result.get("ok"):
                return await self._record_result(turn_id, step, call_id, name, True,
                                                 result.get("result"), emit)
            return await self._record_result(turn_id, step, call_id, name, False,
                                             result.get("error") or "工具执行失败", emit)

        # Only explicitly read-only tools can share a model step. A write,
        # destructive, or external operation remains ordered and inspectable.
        resolved = [self.registry.get((raw.get("function") or {}).get("name") or raw.get("name", ""))
                    for raw in tool_calls]
        if all(tool is not None and tool.spec.parallel_safe for tool in resolved):
            return await asyncio.gather(*(run_one(raw, i) for i, raw in enumerate(tool_calls, 1)))
        return [await run_one(raw, i) for i, raw in enumerate(tool_calls, 1)]

    async def _record_result(self, turn_id: str, step: int, call_id: str, name: str,
                             ok: bool, result: Any, emit) -> dict:
        content = json.dumps(result, ensure_ascii=False, default=str) if not isinstance(result, str) else result
        content = content[:12000]
        self.events.append(turn_id, "tool.result", actor="tool", step=step, call_id=call_id,
                           model_visible=True, payload={"tool_name": name, "ok": ok,
                                                        "model_content": content})
        await emit("tool_result", {"turn_id": turn_id, "call_id": call_id,
                                    "tool_name": name, "ok": ok,
                                    "summary": content[:500]})
        return {"tool": name, "ok": ok, "result": result if ok else None,
                "error": None if ok else content}
