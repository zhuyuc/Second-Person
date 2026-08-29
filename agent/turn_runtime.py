"""A short, event-sourced model/tool loop for normal agent turns."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Awaitable, Callable

from infrastructure.developer_trace import build_agent_trace
from infrastructure.session_metrics import add_tool_time, record_step, session_metrics, turn_metrics
from infrastructure.timeutil import now_cst
from langfuse.integration import get_tracer
from .decision_summary import build_tool_decision_notice
from .repeat_tool_guard import RepeatToolGuard
from .turn_events import TurnEventStore
from .prompt_assembler import PROMPT_VERSION, ToolPromptBuilder


def _format_turn_time() -> str | None:
    """本轮时间元信息文本，追加到 messages 末尾而不进 system prompt。

    进 system prompt 的分钟级时间戳每分钟第一条消息就会击穿整个前缀 cache；
    改到 messages 尾部，只影响尾部一小段 tokens，system + tools + history
    保持字节稳定，DeepSeek 官方 prefix cache 命中率显著提升。

    文本极简化 + 精度降为"天"（T1+T2）：去掉冗余说明，只保留 [北京时间] +
    YYYY-MM-DD。同一天内所有 turn 的 context.time 字节完全相同，直接可命中
    prefix cache；模型仍能通过日期计算星期几，聊天场景下"精确到天"足够，
    真需要精确时刻可由 datetime_now 工具按需返回。
    """
    try:
        now = now_cst()
        return f"[北京时间] {now:%Y-%m-%d}"
    except Exception:  # noqa: BLE001
        return None

logger = logging.getLogger("second_person.turn_runtime")


class TurnRuntime:
    def __init__(self, *, db, config, sessions, registry, executor, llm,
                 providers, system_prompt: Callable[..., str],
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
        from memory import _constants as _mem_const
        max_steps = _mem_const.AGENT_MAX_STEPS
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
        from memory import _constants as _mem_const
        repeat_guard = RepeatToolGuard(_mem_const.REPEAT_TOOL_THRESHOLDS)
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
            # 每 turn 采样一次；写在 user.message 之后 → model_messages 里
            # 时间就跟在用户消息尾巴上；多 step 复用同一份文本，前缀稳定。
            time_text = _format_turn_time()
            if time_text:
                self.events.append(turn_id, "context.time", actor="host",
                                   model_visible=True, payload={"content": time_text})
            await runtime_emit("turn_started", {"turn_id": turn_id,
                                                  "reasoning_effort": reasoning_effort,
                                                  "max_steps": max_steps})
            # Δ6：context 提升到 turn 级缓存 —— 检索/history/snap 只在 step 1
            # 组装一次。step≥2 时 model_messages(turn_id) 已经包含最新的 user/
            # tool 事件，无需再重跑 retriever（原实现每步重跑但结果被 if step==1
            # 挡掉，属于纯浪费；多步工具轮最坏 +7×2.6s）。
            turn_context: dict | None = None
            for step in range(1, max_steps + 1):
                # 与 deepseek-harness 契约对齐：ttft/llm/decode 只计量 LLM 调用本身，
                # 检索/精筛/prompt 组装的耗时单独进 context_ms。这样 ttft 跨轮稳定
                # （不会因冷缓存首轮飘高），context_ms 出现回归也一眼定位。
                context_prep_started_at = time.perf_counter()
                self.events.set_status(turn_id, "running", step=step)
                self.events.append(turn_id, "step.started", actor="host", step=step,
                                   payload={"reasoning_effort": reasoning_effort})
                await runtime_emit("step_started", {"turn_id": turn_id, "step": step})
                if turn_context is None:
                    context_span = tracer.span_start("context.assemble", input={
                        "turn_id": turn_id, "step": step})
                    turn_context = await self.context_loader(
                        session_id=session_id, turn_id=turn_id, message=message,
                        onboarding=onboarding, step=step, handoff_path=handoff_path)
                    context_span.end(output={
                        "history_messages": len(turn_context["history"]),
                        "memories": turn_context.get("memory_count", 0)})
                    memory_ctx = turn_context.get("memory_context")
                    if memory_ctx:
                        self.events.append(turn_id, "context.memories", actor="host",
                                           model_visible=True,
                                           payload={"content": memory_ctx})
                    handoff_ctx = turn_context.get("handoff_context")
                    if handoff_ctx:
                        self.events.append(turn_id, "context.handoff", actor="host",
                                           model_visible=True,
                                           payload={"content": handoff_ctx})
                context = turn_context
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
                    onboarding, location, session_id, context.get("dynamic_blocks"),
                    message)}]
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
                first_token_at: float | None = None
                step_usage: dict[str, Any] = {}
                # LLM 调用起点：ttft/llm/decode 都以此为基准（对齐 harness step/start）。
                step_started_at = time.perf_counter()
                context_ms = max(0, round((step_started_at - context_prep_started_at) * 1000))
                try:
                    # 流式：内容增量边收边发 content_delta，首字延迟由整段生成
                    # 时间降到首 chunk 到达时间；tool_calls 在流内累积，末尾 done
                    # 事件同时返回内容与工具调用，与非流式契约等价。
                    async for kind, data in self.llm.stream_chat(
                            snap, prompt, source="agent_step",
                            session_id=session_id, tools=tools,
                            images=images if step == 1 else None,
                            extra_body={"reasoning_effort": effective_effort}):
                        # 对齐 deepseek-harness/isTokenDelta：首 token 打点覆盖
                        # 所有 meaningful 流式 chunk（含 reasoning）。这样 ttft_ms
                        # 反映"用户看到第一个字符"的真实时刻；decode_ms 覆盖 reasoning
                        # + content 全段，与 provider usage.output_tokens 分母对齐，
                        # 避免"分子含 reasoning、分母只算 content"造成的 tok/s 虚高。
                        if kind in ("content", "reasoning") and data \
                                and first_token_at is None:
                            first_token_at = time.perf_counter()
                        if kind == "content":
                            content_parts.append(data)
                            await emit("content_delta", {"text": data})
                        elif kind == "reasoning":
                            reasoning_source = "provider"
                            reasoning_parts.append(data)
                            await emit("reasoning_delta", {"text": data, "source": "provider"})
                        elif kind == "done":
                            tool_calls = data.get("tool_calls") or []
                            step_usage = data.get("usage") or {}
                            break
                    calls += 1
                finally:
                    step_span.end()
                step_completed_at = time.perf_counter()
                llm_ms = max(0, round((step_completed_at - step_started_at) * 1000))
                ttft_ms = (max(0, round((first_token_at - step_started_at) * 1000))
                           if first_token_at is not None else None)
                decode_ms = (max(0, round((step_completed_at - first_token_at) * 1000))
                             if first_token_at is not None else None)
                record_step(
                    self.db, turn_id=turn_id, step=step, llm_ms=llm_ms,
                    ttft_ms=ttft_ms, decode_ms=decode_ms,
                    input_tokens=step_usage.get("input_tokens", 0),
                    output_tokens=step_usage.get("output_tokens", 0),
                    cache_read_tokens=step_usage.get("cache_read_tokens", 0),
                    cache_write_tokens=step_usage.get("cache_write_tokens", 0),
                    context_ms=context_ms,
                )
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
                    await runtime_emit("turn_completed", {
                        "message_id": msg_id, "turn_id": turn_id,
                        "reasoning_effort": reasoning_effort,
                        "analysis_metadata": analysis_metadata,
                        "metrics": turn_metrics(self.db, turn_id),
                        "session_metrics": session_metrics(self.db, session_id,
                                                           current_turn_id=turn_id),
                    })
                    return {"turn_id": turn_id, "message_id": msg_id, "content": content}
                self.events.append(turn_id, "assistant.tool_calls", actor="model", step=step,
                                   model_visible=True, payload={"content": "".join(content_parts),
                                                                "tool_calls": tool_calls})
                results = await self._run_tool_calls(turn_id, step, tool_calls, runtime_emit,
                                                     repeat_guard)
                tool_ms = sum(int(result.get("_duration_ms", 0) or 0)
                              for result in results)
                if tool_ms:
                    add_tool_time(self.db, turn_id=turn_id, step=step, tool_ms=tool_ms)
                self.events.append(turn_id, "step.finished", actor="host", step=step,
                                   payload={"outcome": "tool_calls", "count": len(results)})
                # 步边界推送刷新后的会话/turn 指标——对齐 deepseek-harness 的
                # sessionStats projection 在 assistant/message 时的更新时机，
                # 让多步 turn 在中间也能刷新 tok/s / TTFT 平均值等聚合数字。
                await emit("step_metrics", {
                    "turn_id": turn_id, "step": step,
                    "metrics": turn_metrics(self.db, turn_id),
                    "session_metrics": session_metrics(self.db, session_id,
                                                       current_turn_id=turn_id),
                })
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
            await runtime_emit("turn_completed", {
                "message_id": msg_id, "turn_id": turn_id,
                "reasoning_effort": reasoning_effort,
                "analysis_metadata": analysis_metadata,
                "metrics": turn_metrics(self.db, turn_id),
                "session_metrics": session_metrics(self.db, session_id,
                                                   current_turn_id=turn_id),
            })
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
            await emit("tool_executing", {"tool_name": name, "status": "running",
                                            "turn_id": turn_id, "call_id": call_id})
            turn = self.events.get_turn(turn_id) or {}
            tool_started_at = time.perf_counter()
            result = await self.executor.execute_tool(name, params, emit=emit,
                                                      session_id=turn.get("session_id", ""))
            duration_ms = max(0, round((time.perf_counter() - tool_started_at) * 1000))
            if result.get("ok"):
                recorded = await self._record_result(turn_id, step, call_id, name, True,
                                                     result.get("result"), emit)
            else:
                recorded = await self._record_result(
                    turn_id, step, call_id, name, False,
                    result.get("error") or "工具执行失败", emit)
            recorded["_duration_ms"] = duration_ms
            return recorded

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
