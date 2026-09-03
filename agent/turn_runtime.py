"""A short, event-sourced model/tool loop for normal agent turns."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from infrastructure.developer_trace import build_agent_trace
from infrastructure.session_metrics import add_tool_time, record_step, session_metrics, turn_metrics
from langfuse.integration import get_tracer
from .repeat_tool_guard import RepeatToolGuard
from .turn_events import TurnEventStore
from .prompt_assembler import PROMPT_VERSION, SessionCtx, ToolPromptBuilder
from . import turn_runtime_helpers as _tr_helpers
from .turn_runtime_helpers import (
    format_turn_time as _format_turn_time,
    summarize_tool_result as _summarize_tool_result,
)
from .turn_runtime_tools import TurnToolRunner

_extract_web_citations = _tr_helpers.extract_web_citations

logger = logging.getLogger("second_person.turn_runtime")


class TurnRuntime:
    def __init__(self, *, db, config, sessions, registry, executor, llm,
                 providers, system_prompt: Callable[..., str],
                 context_loader: Callable[..., Awaitable[dict[str, Any]]],
                 persist_images: Callable[[list[str] | None], list[str] | None] | None = None,
                 tool_prompt_builder: ToolPromptBuilder | None = None,
                 token_meter=None, compaction_engine=None) -> None:
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
        # v7：token 度量 + 自动压缩（可 None，降级为原有行为）
        self.token_meter = token_meter
        self.compaction_engine = compaction_engine
        self._tool_runner = TurnToolRunner(
            registry=registry, executor=executor, events=self.events)

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
        # v7：允许 config 覆盖，方便运维按会话类型或 A/B 分组调整
        max_steps = int(self.config.get(
            "agent_max_steps", _mem_const.AGENT_MAX_STEPS))
        tracer = get_tracer()
        # 侧边会话（channel='aside'）在 Langfuse 打 tag，便于按会话类型独立分析。
        _ch_row = self.db.query_one(
            "SELECT channel FROM sessions WHERE session_id=?", (session_id,))
        _is_aside = bool(_ch_row and _ch_row["channel"] == "aside")
        trace = tracer.trace_start(
            "agent.turn", session_id=session_id,
            input={"message_chars": len(message), "images": len(images or [])},
            metadata={"request_id": client_request_id, "reasoning_effort": reasoning_effort,
                      "contract_version": "v2", "channel": (_ch_row["channel"] if _ch_row else None)},
            tags=(["aside"] if _is_aside else None))
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
        # 图形工具产出的可视化（render_flowchart/render_mermaid），随最终消息持久化，
        # 供刷新后 DiagramRenderer 恢复渲染；实时渲染走 tool_visual SSE 事件。
        turn_visuals: list[dict[str, Any]] = []
        # v7：交错时间线——按事件到达顺序 append reasoning 段与 tool_call 卡片，
        # 前端按此重放"想 → 调 → 想 → 调"的因果链。相邻同类事件自动合并/更新。
        # 结构：{"kind": "reasoning", "text": "..."} 或
        #      {"kind": "tool_call", "call_id": "...", "name": "...",
        #       "arguments": "...", "status": "running|ok|fail",
        #       "result_preview": "..."}
        timeline: list[dict[str, Any]] = []
        # call_id → timeline 下标：让 tool_result 更新在 O(1) 完成，替代反向线性扫描。
        # tool_call 追加时写入；命中/迁移状态后条目仍留在 timeline 里，key 不用清理。
        tool_call_index: dict[str, int] = {}

        def _tl_append_reasoning(text: str) -> None:
            if not text:
                return
            if timeline and timeline[-1].get("kind") == "reasoning":
                timeline[-1]["text"] += text
            else:
                timeline.append({"kind": "reasoning", "text": text})

        def _tl_append_narration(text: str) -> None:
            """工具步旁白（边说边调工具的 content）入 timeline 留存：它被撤回出
            正文，但作为"模型说了什么"记录在处理进度面板，不丢失。"""
            if not text:
                return
            text = text[:4000]
            if timeline and timeline[-1].get("kind") == "narration":
                timeline[-1]["text"] += text
            else:
                timeline.append({"kind": "narration", "text": text})

        def _tl_upsert_tool(evt: dict) -> None:
            """tool_executing → push；tool_result → 就地更新同 call_id 项。"""
            call_id = evt.get("call_id") or evt.get("id") or ""
            name = evt.get("tool_name") or evt.get("name") or ""
            event = evt.get("event")
            if event == "tool_executing":
                timeline.append({
                    "kind": "tool_call", "call_id": call_id, "name": name,
                    "arguments": evt.get("arguments") or "",
                    "status": "running",
                })
                if call_id:
                    tool_call_index[call_id] = len(timeline) - 1
                return
            # tool_result：优先 O(1) 命中同 call_id 的 tool_call；否则回退线性扫描
            preview = (evt.get("summary")
                       or _summarize_tool_result(evt.get("result"))
                       or "")
            if preview:
                preview = preview[:400]
            item = None
            if call_id:
                idx = tool_call_index.get(call_id)
                if idx is not None:
                    candidate = timeline[idx]
                    if (candidate.get("kind") == "tool_call"
                            and candidate.get("name") == name
                            and candidate.get("status") == "running"):
                        item = candidate
            if item is None:
                # 无 call_id 或索引未命中：反向扫描兜底
                for existing in reversed(timeline):
                    if (existing.get("kind") == "tool_call"
                            and (existing.get("call_id") == call_id or not call_id)
                            and existing.get("name") == name
                            and existing.get("status") == "running"):
                        item = existing
                        break
            if item is not None:
                item["status"] = "ok" if evt.get("ok") else "fail"
                if preview:
                    item["result_preview"] = preview
                if evt.get("error"):
                    item["error"] = str(evt["error"])[:400]
                cites = evt.get("citations")
                if cites:
                    item["citations"] = cites
                return
            orphan: dict[str, Any] = {
                "kind": "tool_call", "call_id": call_id, "name": name,
                "arguments": evt.get("arguments") or "",
                "status": "ok" if evt.get("ok") else "fail",
                "result_preview": preview,
                **({"error": str(evt["error"])[:400]} if evt.get("error") else {}),
            }
            if evt.get("citations"):
                orphan["citations"] = evt["citations"]
            timeline.append(orphan)
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
                # v7 timeline：工具事件在这里入 timeline，保序（跟 reasoning 交错）
                _tl_upsert_tool({"event": event, **data})
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
            # 增量缓存：避免每步全量重读 + 重解析 events，多步对话从 O(steps²) 降到 O(steps)。
            # 压缩重载 context 时只重建 system_content，累计的 model_messages 不动
            # （events 表未变，压缩只改 history 水位）。
            cumulative_model_messages: list[dict[str, Any]] = []
            last_model_seq: int = 0
            cached_system_content: str | None = None
            turn_body = ""  # 用户可见正文（工具步旁白 content_reset 后会清空）
            step = 0
            for step in range(1, max_steps + 1):
                # 与 deepseek-harness 契约对齐：ttft/llm/decode 只计量 LLM 调用本身，
                # 检索/精筛/prompt 组装的耗时单独进 context_ms。这样 ttft 跨轮稳定
                # （不会因冷缓存首轮飘高），context_ms 出现回归也一眼定位。
                context_prep_started_at = time.perf_counter()
                self.events.set_status(turn_id, "running", step=step)
                self.events.append(turn_id, "step.started", actor="host", step=step,
                                   payload={"reasoning_effort": reasoning_effort})
                await runtime_emit("step_started", {"turn_id": turn_id, "step": step})

                async def _progress(phase: str, label: str, detail: str = "") -> None:
                    await emit("step_progress", {
                        "turn_id": turn_id, "step": step,
                        "phase": phase, "label": label, "detail": detail,
                    })

                if turn_context is None:
                    await _progress("memory", "检索相关记忆", "向量检索与组装对话上下文")
                else:
                    await _progress("continue", "整合工具结果", f"进入第 {step} 步继续推理")
                if turn_context is None:
                    context_span = tracer.span_start("context.assemble", input={
                        "turn_id": turn_id, "step": step})
                    turn_context = await self.context_loader(
                        session_id=session_id, turn_id=turn_id, message=message,
                        onboarding=onboarding, step=step, handoff_path=handoff_path,
                        emit=emit)
                    memory_timeline = turn_context.get("memory_timeline") or []
                    if memory_timeline:
                        timeline.extend(memory_timeline)
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
                    # M3：项目 + 沙箱策略动态注入（仅项目会话）
                    project_ctx = turn_context.get("project_context")
                    if project_ctx:
                        self.events.append(turn_id, "context.project", actor="host",
                                           model_visible=True,
                                           payload={"content": project_ctx})
                    # 项目说明书 baseline（首次注入 project_instructions；后续文件
                    # 变化时通过 project_instructions_changes 追加 delta；未变时
                    # 两者均为 None，前缀 cache 完全命中）
                    proj_instructions = turn_context.get("project_instructions")
                    if proj_instructions:
                        self.events.append(turn_id, "context.project_instructions",
                                           actor="host", model_visible=True,
                                           payload={"content": proj_instructions})
                    proj_changes = turn_context.get("project_instructions_changes")
                    if proj_changes:
                        self.events.append(turn_id,
                                           "context.project_instructions_changes",
                                           actor="host", model_visible=True,
                                           payload={"content": proj_changes})
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
                system_content = cached_system_content
                if system_content is None:
                    system_content = self.system_prompt(
                        onboarding, location, session_id, context.get("dynamic_blocks"),
                        message)
                    cached_system_content = system_content
                # 增量拉取本步新增的 model-visible events，避免全量重读
                new_msgs, last_model_seq = self.events.model_messages(
                    turn_id, after_seq=last_model_seq)
                cumulative_model_messages.extend(new_msgs)
                prompt = [{"role": "system", "content": system_content}]
                prompt += context["history"] + cumulative_model_messages
                tools = self._project_tools(session_id)
                # v7：pre-step 自动压缩（step≥2 才检查，step 1 无累积压力）
                if step >= 2 and self.compaction_engine is not None:
                    await _progress("compact_check", "检查会话上下文容量",
                                    "判断是否需压缩早期对话")
                    try:
                        compact_result = await self.compaction_engine.compact_if_needed(
                            session_id=session_id, snap=snap,
                            messages=context["history"],
                            system=system_content, tools=tools,
                            message_ids=context.get("history_ids") or [])
                    except Exception:  # noqa: BLE001
                        logger.warning("压缩检查异常，跳过本轮", exc_info=True)
                        compact_result = None
                    if compact_result is not None:
                        await _progress(
                            "compact", "压缩早期对话",
                            f"已收起 {compact_result.shadowed_count} 条早期消息")
                        self.events.append(
                            turn_id, "context.compacted", actor="host",
                            model_visible=False,
                            payload={"trigger": compact_result.trigger,
                                     "shadowed_count": compact_result.shadowed_count,
                                     "released_tokens_est": compact_result.released_tokens_est,
                                     "total_before": compact_result.total_before,
                                     "total_after_est": compact_result.total_after_est})
                        await runtime_emit("context_compacted", {
                            "turn_id": turn_id, "step": step,
                            "trigger": compact_result.trigger,
                            "shadowed_count": compact_result.shadowed_count,
                            "released_tokens_est": compact_result.released_tokens_est})
                        await _progress("context_reload", "重新加载会话历史")
                        # 压缩推了水位——重新加载 context 拿新 history
                        turn_context = await self.context_loader(
                            session_id=session_id, turn_id=turn_id, message=message,
                            onboarding=onboarding, step=step, handoff_path=handoff_path,
                            emit=emit)
                        memory_timeline = turn_context.get("memory_timeline") or []
                        if memory_timeline:
                            timeline.extend(memory_timeline)
                        context = turn_context
                        # 压缩后 dynamic_blocks 可能变，system_content 需重建；
                        # model_messages 累计不受影响（events 表未变）
                        cached_system_content = None
                        system_content = self.system_prompt(
                            onboarding, location, session_id,
                            context.get("dynamic_blocks"), message)
                        cached_system_content = system_content
                        prompt = [{"role": "system", "content": system_content}]
                        prompt += context["history"] + cumulative_model_messages
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
                # 本步 reasoning 增量：对齐 deepseek-harness 的 CoT 回传，
                # 每个 assistant 事件带上本步 reasoning_content，让模型稳定停留在
                # 推理通道（工具步旁白不发到正文 content）。
                step_reasoning_parts: list[str] = []
                # 本步旁白是否已撤回（tool_start 时提前撤回后，步末不再重复）
                narration_reset_done = False
                first_token_at: float | None = None
                step_usage: dict[str, Any] = {}
                # LLM 调用起点：ttft/llm/decode 都以此为基准（对齐 harness step/start）。
                step_started_at = time.perf_counter()
                context_ms = max(0, round((step_started_at - context_prep_started_at) * 1000))
                llm_detail = "等待模型返回首 token"
                try:
                    if self.token_meter is not None:
                        meas = self.token_meter.measure(
                            session_id, prompt[1:], system=system_content, tools=tools)
                        k = int(meas.total_tokens or 0)
                        if k >= 10000:
                            llm_detail = f"约 {k / 1000:.0f}K 输入 token，首字可能需数十秒"
                        elif k >= 1000:
                            llm_detail = f"约 {k / 1000:.1f}K 输入 token"
                        elif k > 0:
                            llm_detail = f"约 {k} 输入 token"
                except Exception:  # noqa: BLE001
                    pass
                await _progress("llm", "调用模型推理", llm_detail)
                try:
                    # 流式：内容增量边收边发 content_delta，首字延迟由整段生成
                    # 时间降到首 chunk 到达时间；tool_calls 在流内累积，末尾 done
                    # 事件同时返回内容与工具调用，与非流式契约等价。
                    # 注意：工具步里模型可能"边说边调工具"（旁白也会走 content
                    # 增量），这些旁白先实时进正文、在本步确认带 tool_calls 后
                    # 由 content_reset 事件撤回，保证正文最终只留末步答案。
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
                            if not narration_reset_done:
                                turn_body += data
                            await emit("content_delta", {"text": data})
                        elif kind == "reasoning":
                            reasoning_source = "provider"
                            reasoning_parts.append(data)
                            step_reasoning_parts.append(data)
                            _tl_append_reasoning(data)
                            await emit("reasoning_delta", {"text": data, "source": "provider"})
                        elif kind == "tool_start":
                            # 首个 tool_call 增量到达即可确认本步是工具步：若已有旁白
                            # （之前收到的 content 增量），立刻撤回并转入面板，把定性
                            # 时机从"整步结束"提前到"旁白刚结束"，避免正文长时间滞留。
                            if content_parts and not narration_reset_done:
                                await emit("content_reset", {"turn_id": turn_id, "step": step})
                                _tl_append_narration("".join(content_parts))
                                narration_reset_done = True
                                turn_body = ""
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
                # v7：把 provider 精确 usage 折进 TokenMeter 的 session anchor —
                # 下一 step 的压力判断就用这个 anchor + tiktoken delta 而不是全估算
                if self.token_meter is not None and step_usage:
                    try:
                        # prompt[0] 是 system；后续都算 messages
                        self.token_meter.commit_anchor(
                            session_id, prompt[1:], step_usage,
                            system=prompt[0].get("content", ""))
                    except Exception:  # noqa: BLE001
                        logger.debug("commit_anchor 失败，忽略", exc_info=True)
                if not tool_calls:
                    content = "".join(content_parts)
                    analysis_metadata = self._analysis_metadata(
                        turn_id=turn_id, reasoning_effort=reasoning_effort,
                        reasoning_parts=reasoning_parts, system_parts=system_parts,
                        tool_events=tool_events, decision_notices=decision_notices,
                        reasoning_source=reasoning_source, end_reason="final_answer",
                        timeline=timeline)
                    msg_id = self.sessions.append_message(
                        session_id, "assistant", content,
                        thinking="".join(thinking_parts) or None,
                        analysis_metadata=analysis_metadata,
                        visuals=turn_visuals or None,
                        parent_id=assistant_parent_id,
                        version_group_id=assistant_version_group_id)
                    self.events.append(turn_id, "assistant.message", actor="model", step=step,
                                       model_visible=True, payload={"content": content,
                                                                    "message_id": msg_id,
                                                                    "reasoning_content": "".join(step_reasoning_parts)[:12000]})
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
                # 本步是工具步：模型边说边调工具，旁白会实时内联进正文。
                # content_reset 是给其它消费端（IM 网关）的信号——把这段工具步旁白
                # 从最终回复里剔除；web 端正文内联、turn 结束由 reload 折叠进面板。
                # 旁白本身记入 timeline 留存（处理进度面板可见、随消息持久化）。
                if content_parts and not narration_reset_done:
                    narration = "".join(content_parts)
                    await emit("content_reset", {"turn_id": turn_id, "step": step})
                    _tl_append_narration(narration)
                    turn_body = ""
                self.events.append(turn_id, "assistant.tool_calls", actor="model", step=step,
                                   model_visible=True, payload={"content": "".join(content_parts),
                                                                "tool_calls": tool_calls,
                                                                "reasoning_content": "".join(step_reasoning_parts)[:12000]})
                results = await self._run_tool_calls(turn_id, step, tool_calls, runtime_emit,
                                                     repeat_guard)
                # 收集图形工具产出，随最终消息持久化（刷新后可恢复渲染）
                for _tr in results:
                    if _tr.get("ok") and _tr.get("tool") in (
                            "render_flowchart", "render_mermaid"):
                        _vd = _tr.get("result")
                        if isinstance(_vd, dict) and _vd.get("type"):
                            turn_visuals.append({"type": _vd["type"], "data": _vd})
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
            # v7：自动压缩接管压力管控后，AGENT_MAX_STEPS 从"截断触发线"降为
            # "防死循环兜底"。如果模型已经产出内容就拼接，否则给拆分建议。
            content_so_far = "".join(content_parts).strip()
            if content_so_far:
                content = (content_so_far
                           + f"\n\n[已达 {max_steps} 步上限，以上为基于已收集信息给出的部分结果。"
                           + "如需继续探索请把问题拆得更具体或分多轮进行]")
            else:
                content = (f"本次任务经过 {max_steps} 步仍未产出回复，可能任务粒度过大或工具调用出现循环。"
                           "建议拆分为更小的子问题重试，或新开一个会话。")
            msg_id = self.sessions.append_message(
                session_id, "assistant", content,
                thinking="".join(thinking_parts) or None,
                analysis_metadata=self._analysis_metadata(
                    turn_id=turn_id, reasoning_effort=reasoning_effort,
                    reasoning_parts=reasoning_parts, system_parts=system_parts,
                    tool_events=tool_events, decision_notices=decision_notices,
                    reasoning_source=reasoning_source, end_reason="max_steps",
                    timeline=timeline),
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
                reasoning_source=reasoning_source, end_reason="max_steps",
                timeline=timeline)
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
            partial_body = turn_body.strip()
            has_progress = bool(
                partial_body or timeline or reasoning_parts or tool_events or thinking_parts)
            if has_progress:
                marker = "> ⚠️ 本回复未完成：生成已中断"
                content = (f"{partial_body}\n\n{marker}" if partial_body
                           else f"{marker}，仅输出了处理进度")
                analysis_metadata = self._analysis_metadata(
                    turn_id=turn_id, reasoning_effort=reasoning_effort,
                    reasoning_parts=reasoning_parts, system_parts=system_parts,
                    tool_events=tool_events, decision_notices=decision_notices,
                    reasoning_source=reasoning_source, end_reason="cancelled",
                    timeline=timeline)
                msg_id = self.sessions.append_message(
                    session_id, "assistant", content,
                    thinking="".join(thinking_parts) or None,
                    analysis_metadata=analysis_metadata,
                    parent_id=assistant_parent_id,
                    version_group_id=assistant_version_group_id)
                self.events.append(turn_id, "assistant.message", actor="host", step=step,
                                   model_visible=True,
                                   payload={"content": content, "message_id": msg_id})
                try:
                    await runtime_emit("turn_completed", {
                        "message_id": msg_id, "turn_id": turn_id,
                        "reasoning_effort": reasoning_effort,
                        "analysis_metadata": analysis_metadata,
                        "metrics": turn_metrics(self.db, turn_id),
                        "session_metrics": session_metrics(self.db, session_id,
                                                           current_turn_id=turn_id),
                    })
                except asyncio.CancelledError:
                    pass
            self.events.finish(turn_id, status="cancelled", end_reason="cancelled", step=step)
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
                           reasoning_source: str, end_reason: str,
                           timeline: list[dict[str, Any]] | None = None
                           ) -> dict[str, Any]:
        """Persist structured, safe display lanes instead of one mixed string.

        v7：新增 timeline 字段（按到达顺序交错的 reasoning + tool_call 项），
        前端优先按 timeline 重放"想 → 调 → 想 → 调"因果链。
        老字段 reasoning_text / tool_events 保留兼容旧消息回看。
        """
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
            # v7 交错时间线（限最近 160 项防 md 膨胀；正常一轮 <20 项）
            "timeline": (timeline or [])[-160:],
        }

    def _project_tools(self, session_id: str) -> list[dict]:
        """Expose the full catalog gated by session-level policy.

        Session-level gating (project attachment + sandbox mode) keeps the
        tools payload byte-stable across normal turns — the provider prefix
        cache reuses this prefix as long as neither of those two policies
        changes. Per-message keyword projection would collapse cache reuse.
        """
        return self.tool_prompts.schemas(self._session_ctx(session_id))

    def _session_ctx(self, session_id: str) -> SessionCtx:
        """Resolve the session's effective sandbox mode.

        Delegates to PolicyStore so this stays authoritative — normalizing
        legacy modes, respecting event-stream overrides, and inheriting from
        the project row all live in one place. Falls back to workspace-write
        on any error so gating never accidentally opens up.
        """
        try:
            resolver = getattr(self.executor, "workspace_resolver", None)
            if resolver is not None:
                policy = resolver.policy.resolve(session_id)
                return SessionCtx(sandbox_mode=policy.mode)
            row = self.db.query_one(
                "SELECT sandbox_mode FROM sessions WHERE session_id=?",
                (session_id,))
            if row and "sandbox_mode" in row.keys() and row["sandbox_mode"]:
                from tools.fs.policy import normalize_mode
                return SessionCtx(sandbox_mode=normalize_mode(row["sandbox_mode"]))
        except Exception:  # noqa: BLE001
            pass
        return SessionCtx()

    async def _run_tool_calls(self, turn_id: str, step: int, tool_calls: list[dict],
                              emit, repeat_guard: RepeatToolGuard) -> list[dict]:
        return await self._tool_runner.run_tool_calls(
            turn_id, step, tool_calls, emit, repeat_guard)

    async def _record_result(self, turn_id: str, step: int, call_id: str, name: str,
                             ok: bool, result: Any, emit,
                             arguments: Any = None) -> dict:
        return await self._tool_runner.record_result(
            turn_id, step, call_id, name, ok, result, emit, arguments=arguments)
