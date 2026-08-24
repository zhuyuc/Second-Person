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
from .turn_events import TurnEventStore

logger = logging.getLogger("second_person.turn_runtime")


class TurnRuntime:
    def __init__(self, *, db, config, sessions, registry, executor, llm,
                 providers, tool_policy, system_prompt: Callable[..., str],
                 context_loader: Callable[..., Awaitable[dict[str, Any]]],
                 persist_images: Callable[[list[str] | None], list[str] | None] | None = None) -> None:
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

        async def runtime_emit(event: str, data: dict) -> None:
            """Broadcast events and retain a coherent thinking summary with the reply.

            Two lanes end up in the persisted thinking panel:
            - Model reasoning (thinking_delta text from stream_chat reasoning
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
            elif event == "thinking_delta":
                # 模型的思考增量：入面板同时随消息落库，刷新历史后思考仍在
                thinking_parts.append(data.get("text", ""))
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
                prompt = [{"role": "system", "content": self.system_prompt(onboarding, location, session_id)
                           + context.get("extra_system", "")}] + context["history"] + self.events.model_messages(turn_id)
                tools = self.registry.openai_schemas()
                self.events.append(turn_id, "request.header", actor="host", step=step,
                                   payload={"model_id": context["snap"].model_id,
                                            "reasoning_effort": reasoning_effort,
                                            "tool_names": [t["function"]["name"] for t in tools],
                                            "history_count": len(prompt)}, model_visible=False)
                step_span = tracer.span_start("agent.step", input={"turn_id": turn_id, "step": step},
                                              metadata={"reasoning_effort": reasoning_effort})
                content_parts: list[str] = []
                tool_calls: list[dict] = []
                try:
                    # 流式：内容增量边收边发 content_delta，首字延迟由整段生成
                    # 时间降到首 chunk 到达时间；tool_calls 在流内累积，末尾 done
                    # 事件同时返回内容与工具调用，与非流式契约等价。
                    async for kind, data in self.llm.stream_chat(
                            context["snap"], prompt, source="agent_step",
                            session_id=session_id, tools=tools,
                            images=images if step == 1 else None,
                            extra_body={"reasoning_effort": reasoning_effort}):
                        if kind == "content":
                            content_parts.append(data)
                            await emit("content_delta", {"text": data})
                        elif kind == "reasoning":
                            # 推理模型思考增量（DeepSeek reasoning_content 等）
                            # → 通过 runtime_emit 走 thinking_delta，同时进面板与
                            # 落库缓冲。走 runtime_emit 而非 emit 是为了把这段
                            # 追加进 thinking_parts，随消息一并持久化。
                            await runtime_emit("thinking_delta", {"text": data})
                        elif kind == "done":
                            tool_calls = data.get("tool_calls") or []
                            break
                    calls += 1
                finally:
                    step_span.end()
                if not tool_calls:
                    content = "".join(content_parts)
                    msg_id = self.sessions.append_message(
                        session_id, "assistant", content,
                        thinking="".join(thinking_parts) or None,
                        analysis_metadata={"turn_id": turn_id,
                                           "reasoning_effort": reasoning_effort},
                        parent_id=assistant_parent_id,
                        version_group_id=assistant_version_group_id)
                    self.events.append(turn_id, "assistant.message", actor="model", step=step,
                                       model_visible=True, payload={"content": content,
                                                                    "message_id": msg_id})
                    self.events.append(turn_id, "step.finished", actor="host", step=step,
                                       payload={"outcome": "final"})
                    self.events.finish(turn_id, status="completed", end_reason="final_answer", step=step)
                    trace.update(output={"message_id": msg_id}, metadata={"developer_trace": build_agent_trace(
                        turn_id=turn_id, reasoning_effort=reasoning_effort, steps=step,
                        llm_call_count=calls, latency_ms=int((time.monotonic() - started) * 1000),
                        end_reason="final_answer")})
                    await runtime_emit("turn_completed", {"message_id": msg_id, "turn_id": turn_id,
                                                           "reasoning_effort": reasoning_effort})
                    return {"turn_id": turn_id, "message_id": msg_id, "content": content}
                self.events.append(turn_id, "assistant.tool_calls", actor="model", step=step,
                                   model_visible=True, payload={"content": "".join(content_parts),
                                                                "tool_calls": tool_calls})
                results = await self._run_tool_calls(turn_id, step, tool_calls, runtime_emit)
                self.events.append(turn_id, "step.finished", actor="host", step=step,
                                   payload={"outcome": "tool_calls", "count": len(results)})
            content = "任务已达到最大执行步骤，以下是已完成的结果。"
            msg_id = self.sessions.append_message(
                session_id, "assistant", content,
                thinking="".join(thinking_parts) or None,
                analysis_metadata={"turn_id": turn_id, "end_reason": "max_steps"},
                parent_id=assistant_parent_id,
                version_group_id=assistant_version_group_id)
            self.events.append(turn_id, "assistant.message", actor="host", step=max_steps,
                               model_visible=True, payload={"content": content, "message_id": msg_id})
            self.events.finish(turn_id, status="completed", end_reason="max_steps", step=max_steps)
            await runtime_emit("content_delta", {"text": content})
            await runtime_emit("turn_completed", {"message_id": msg_id, "turn_id": turn_id,
                                                   "reasoning_effort": reasoning_effort})
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

    async def _run_tool_calls(self, turn_id: str, step: int, tool_calls: list[dict], emit) -> list[dict]:
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
