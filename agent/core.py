"""
Agent Core —— 八步对话流水线编排（产品文档 §对话调度引擎 / 开发文档 §1.1）。

输入预处理 → 上下文加载(冻结快照) → 记忆检索 → 意图识别 → 流程编排(DAG)
→ 工具执行 → 响应合成与输出 → 后置处理。
产出 SSE 事件流（async generator，yield {"event","data"}）。
同 session 串行化：同一 session 同时最多处理一个请求，超出排队（上限 session_queue_limit）。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict
from datetime import datetime
from typing import AsyncIterator

from infrastructure.llm_provider import CircuitOpenError
from infrastructure.observability import get_trace_id
from infrastructure.prompt_loader import PROMPTS
from langfuse.integration import get_tracer, mark_preview
from soul.constants import ONBOARDING_PERSONA

from . import response_synthesizer as rs
from .compression import Compressor, assemble_context, render_summary_body
from .dag_scheduler import SharedState, build_dag
from .degradation import (
    DegradationState,
    FailureType,
    decide_degradation,
)
from .intent_parser import (
    AttentionFocuser,
    DegradationError,
    EmotionState,
    FocusResult,
    GapDetector,
    GapResult,
    IntentParser,
    QuickIntentResult,
    Understanding,
)
from .strategy_engine import (
    ResponseStrategy,
    StrategyEngine,
    StrategyInputs,
)
from .meta_cognitive import CognitiveSkeleton, MetaCognitiveProtocol
from .next_step import NextStepPipeline, parse_suggestion, strip_suggestion_from_partial
from infrastructure.timeutil import now_cst

logger = logging.getLogger("second_person.core")

# 意图类型中文标签（思考过程展示用，提交/存储仍用英文枚举值）
INTENT_TYPE_LABELS = {
    "query_memory": "检索记忆", "query_knowledge": "查询知识库",
    "query_external": "查询外部信息", "compute": "计算任务",
    "file_op": "文件操作", "remember_intent": "记忆指令",
    "remember_confirm": "重要信息待确认",
    "soul_feedback": "风格反馈", "output_preference_feedback": "输出偏好反馈",
    "meta": "系统相关", "chat": "日常对话",
}


# 输入清洗：剔除控制字符（保留换行/制表符），首尾去空
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# 主动记忆检测：未识别 remember_intent 但消息含明确新事实时，
# 标记为下次被动回顾的候选（零 LLM 启发式）
_FACT_PATTERNS = [
    r"我(是|在|做)", r"我(喜欢|不喜欢|讨厌|偏好|习惯)", r"我决定", r"我打算",
    r"我的(目标|计划|原则)", r"以后(都|就)", r"从今天起", r"我最近",
]

# 约束检测：识别用户明确的范围限定 / 方法约束 / 否定要求（零 LLM 启发式）
_CONSTRAINT_PATTERNS = [
    r"只(看|考虑|要|用|关注)", r"不(要|用|考虑|看)", r"必须", r"一定要",
    r"绝对不", r"限定", r"仅限", r"除了.*都", r"改成", r"换成", r"以后(都|就)",
]


def _extract_constraints(message: str) -> list[str]:
    """从用户消息中抽取约束句（整句保留，非关键词）。零 LLM。
    命中任一约束模式 → 按标点切句，返回含约束信号的子句，每句截 40 字，最多 3 条。"""
    if not any(re.search(p, message or "") for p in _CONSTRAINT_PATTERNS):
        return []
    clauses = re.split(r"[。；;\n]", message or "")
    out = []
    for c in clauses:
        c = c.strip()
        if c and any(re.search(p, c) for p in _CONSTRAINT_PATTERNS):
            out.append(c[:40])
    return out[:3]


def _sanitize_input(text: str) -> str:
    """第 1 步输入清洗：去控制字符与首尾空白，不改写用户内容。"""
    return _CTRL_RE.sub("", text or "").strip()


def _strip_attachment_context(text: str) -> str:
    """剥离前端拼装的附件上下文前缀（【附件：…】块），返回用户真实提问。
    仅供记忆检索的本地 Embedding 使用：几万字附件正文会把本地 BGE-M3 拖入
    分钟级计算（触发 600s 总超时），且向量被海量代码/正文稀释后召回全是噪声。
    意图识别等云端 LLM 环节不得使用本函数——材料本身可能承载用户意图。"""
    if "【附件：" in text and "\n---\n" in text:
        tail = text.split("\n---\n")[-1].strip()
        if tail:
            return tail
    return text


# 附件块解析：与前端 ChatView.extractAttachments 的分块规则对齐
_ATT_BLOCK_RE = re.compile(r"【附件：([^】]+?)(?:（内容已截断）)?】\n?")


def _extract_attachment_blocks(text: str) -> list[tuple[str, str]]:
    """解析消息中的【附件：文件名】块，返回 [(文件名, 正文), ...]。
    无附件时返回空列表；与前端拼装格式（块间以空行分隔，末尾 \\n---\\n 接真实提问）对齐。"""
    if not text or "【附件：" not in text:
        return []
    head = text.rsplit("\n---\n", 1)[0] if "\n---\n" in text else text
    matches = list(_ATT_BLOCK_RE.finditer(head))
    out: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(head)
        body = head[start:end].strip()
        if body:
            out.append((m.group(1).strip(), body))
    return out


def _strip_mood_section(system_prompt: str) -> str:
    """剥离 system prompt 中的情绪注入段与主动行为段（纯导出等结构化输出场景用）。
    以 mood.md 的段首标记定位，删到下一段或结尾；同步剥离主动行为段。"""
    # 先剥离主动行为段（如果存在）
    action_marker = "【本轮主动行为："
    idx = system_prompt.find(action_marker)
    if idx >= 0:
        end = system_prompt.find("\n\n", idx + len(action_marker))
        if end < 0:
            end = len(system_prompt)
        system_prompt = system_prompt[:idx].rstrip() + system_prompt[end:]

    # 再剥离情绪注入段
    marker = "## 当前情绪状态"
    idx = system_prompt.find(marker)
    if idx < 0:
        return system_prompt
    end = system_prompt.find("\n## ", idx + len(marker))
    if end < 0:
        return system_prompt[:idx].rstrip()
    return system_prompt[:idx].rstrip() + system_prompt[end:]


def _format_intent_thinking(intents) -> str:
    """把意图识别结果格式化为可读的思考过程文本（意图理解 + 任务拆解）。"""
    lines = ["【意图理解】"]
    for i, it in enumerate(intents):
        summary = getattr(it, "intent_summary", "") or "（无摘要）"
        itype = INTENT_TYPE_LABELS.get(
            getattr(it, "intent_type", ""), getattr(it, "intent_type", ""))
        tools = getattr(it, "tools_needed", []) or []
        line = f"{i + 1}. {summary}（{itype}）"
        if tools:
            line += f"，计划调用工具：{'、'.join(tools)}"
        deps = getattr(it, "depends_on", []) or []
        if deps:
            line += f"，依赖任务：{'、'.join(deps)}"
        lines.append(line)
    if len(intents) > 1:
        lines.append(f"【任务拆解】共拆解为 {len(intents)} 个子任务，按依赖关系编排执行")
    return "\n".join(lines) + "\n"


class AgentCore:
    def __init__(self, *, db, config, session_store, context_entry, soul_manager,
                 profile_manager, retriever, tool_registry, tool_executor,
                 lifecycle, signal_collector, llm_client, provider_registry,
                 file_writer, skill_manager, event_bus=None, notifier=None,
                 mood_manager=None, mood_trigger=None,
                 mood_action_dispatcher=None):
        self.db = db
        self.config = config
        self.sessions = session_store
        self.ctx_entry = context_entry
        self.soul = soul_manager
        self.profile = profile_manager
        self.retriever = retriever
        self.registry = tool_registry
        self.executor = tool_executor
        self.lifecycle = lifecycle
        self.signals = signal_collector
        self.llm = llm_client
        self.providers = provider_registry
        self.fw = file_writer
        self.skills = skill_manager
        self.bus = event_bus
        self.notify = notifier or (lambda t, m: None)
        # 情绪模块（双源：用户情绪 + AI 自身情绪）；None 表示未启用
        self.mood = mood_manager
        # v2 情绪触发采集器（规则通道）
        self.mood_trigger = mood_trigger
        # v2 主动行为调度器
        self.mood_action_dispatcher = mood_action_dispatcher
        # 图片入库回调（container 在 ingest 就绪后接入）：async fn(images: list[dataURL])
        self.image_kb_fn = None
        self.intent_parser = IntentParser(
            llm_client, lambda: self.providers.snapshot_for("intent"))
        # 收敛式理解：注意力聚焦 + 缺口检测（LLM 调用走 convergence 槽位，
        # 轻量分析任务，默认回退 intent→agent→chat）
        self.attention_focuser = AttentionFocuser(
            llm_client, lambda: self.providers.snapshot_for("convergence"))
        self.gap_detector = GapDetector(
            llm_client, lambda: self.providers.snapshot_for("convergence"))
        # 响应策略引擎（v3 §四）：回答形态/角度/深度/语气的集中决策中枢；
        # 输入不含 memories（策略与记忆内容正交），先验全文注入自行匹配场景
        self.strategy_engine = StrategyEngine(
            llm_client, lambda: self.providers.snapshot_for("agent"),
            config, session_store.data_dir)
        # 元认知协议（v3 §六）：高复杂度问题的思考骨架，失败跳过不阻塞
        self.metacog = MetaCognitiveProtocol(
            llm_client, lambda: self.providers.snapshot_for("agent"))
        # 下一步建议模块：种子提取 + 门槛过滤 + 分隔符解析
        self.next_step = NextStepPipeline(config)
        self.compressor = Compressor(
            llm_client, lambda: self.providers.snapshot_for("agent"),
            lambda: self.providers.snapshot_for("chat"))
        # 压缩连续失败计数（达 3 次推通知建议新建会话）
        self._compress_fails: dict[str, int] = {}
        self._session_locks: dict[str,
                                  asyncio.Lock] = defaultdict(asyncio.Lock)
        self._session_queue: dict[str, int] = defaultdict(int)

    # ---- 主入口：SSE 事件流（队列驱动，支持工具执行中途 emit） ------------
    async def run(self, session_id: str, message: str,
                  client_request_id: str | None = None,
                  images: list[str] | None = None,
                  regenerate: bool = False,
                  location: str | None = None,
                  regenerate_message_id: str | None = None,
                  handoff_path: str | None = None,
                  think_mode: str = "auto") -> AsyncIterator[dict]:
        limit = self.config.get("session_queue_limit", 3)
        if self._session_queue[session_id] >= limit:
            yield {"event": "error", "data": {"code": 429, "message": "会话繁忙，请稍后再试"}}
            return
        self._session_queue[session_id] += 1
        lock = self._session_locks[session_id]
        if lock.locked():
            yield {"event": "queued", "data": {"session_id": session_id}}

        queue: asyncio.Queue = asyncio.Queue()
        _SENTINEL = object()

        async def emit(event: str, data: dict) -> None:
            await queue.put({"event": event, "data": data})

        async def worker() -> None:
            async with lock:
                try:
                    # 请求组：单请求超时 600 秒（进程内任务隔离）
                    await asyncio.wait_for(
                        self._pipeline(session_id, message, emit, images,
                                       regenerate, location,
                                       regenerate_message_id,
                                       handoff_path=handoff_path,
                                       think_mode=think_mode),
                        timeout=600)
                except asyncio.TimeoutError:
                    logger.warning("请求超时（600s）：session=%s", session_id)
                    await emit("error", {"code": 504, "message": "处理超时，请重试"})
                except Exception as e:  # noqa: BLE001
                    logger.exception("流水线异常")
                    await emit("error", {"code": 500, "message": str(e)})
                finally:
                    self._session_queue[session_id] -= 1
                    await queue.put(_SENTINEL)

        task = asyncio.create_task(worker())
        try:
            while True:
                item = await queue.get()
                if item is _SENTINEL:
                    break
                yield item
        finally:
            # 客户端断开/中断时取消后台 worker，避免流式输出继续跑完
            if not task.done():
                task.cancel()
        await task

    async def _pipeline(self, sid: str, message: str, emit, images=None,
                        regenerate=False, location=None,
                        regenerate_message_id: str | None = None,
                        handoff_path: str | None = None,
                        think_mode: str = "auto") -> None:
        """包一层 Langfuse trace，内部实现不变（步骤 span 在 _pipeline_impl 中）。"""
        tracer = get_tracer()
        trace = tracer.trace_start(
            name="chat.turn", session_id=sid, input=message,
            metadata={"internal_trace_id": get_trace_id(),
                      "images": len(images) if images else 0,
                      "regenerate": regenerate,
                      "regenerate_message_id": regenerate_message_id,
                      "location": location,
                      "handoff_path": handoff_path,
                      "think_mode": think_mode})
        try:
            await self._pipeline_impl(sid, message, emit, trace, images,
                                      regenerate, location,
                                      think_mode=think_mode)
        except Exception as e:  # noqa: BLE001
            trace.update(metadata={"internal_trace_id": get_trace_id(),
                                   "images": len(images) if images else 0,
                                   "regenerate": regenerate,
                                   "status": "error", "error": str(e)})
            raise
        finally:
            trace.end()

    async def _pipeline_impl(self, sid: str, message: str, emit, trace,
                             images=None, regenerate=False, location=None,
                             think_mode: str = "auto") -> None:
        tracer = get_tracer()
        onboarding = not self.config.get_raw("onboarding_completed", False)
        _start_time = time.monotonic()
        _llm_call_count = 0   # 仅计主回复流式调用；意图/参数推断等辅助调用见 token_usage 表
        _model_id = None

        # 思考过程累积：透明拦截所有 thinking_delta 增量（意图理解/工具调用/
        # 模型原生推理），回复落库时随消息持久化，历史消息可回看
        think_parts: list[str] = []
        _orig_emit = emit

        async def emit(event, data):  # noqa: F811 — 有意遮蔽，对下游透明
            if event == "thinking_delta":
                think_parts.append(data.get("text", ""))
            await _orig_emit(event, data)

        # 第 1 步 输入预处理 + 记录用户消息
        # 输入清洗；信号采集阶段二：新用户消息到达 → 回填上一条 assistant 回复的隐式反应/关键词
        # 重新生成时跳过：重发的提问不是对上一轮回复的真实反应
        message = _sanitize_input(message)
        # 记忆检索专用：剥离附件正文后的真实提问。仅因本地 Embedding 算不动超长文本，
        # 意图识别/技能匹配等云端 LLM 环节仍读完整消息（材料本身可能承载意图）
        core_query = _strip_attachment_context(message)
        if not onboarding and not regenerate:
            self._backfill_prev_signal(sid, message)
        # 图片落盘含 base64 解码 + 写盘（可达数十毫秒），丢工作线程避免阻塞事件循环
        persisted_imgs = await asyncio.to_thread(self._persist_images, images)
        user_msg_id = self.sessions.append_message(
            sid, "user", message, images=persisted_imgs)

        # v2 情绪触发检测（规则通道，零 LLM）：在意图识别前采集本轮触发事件
        self._detect_emotion_triggers(sid, message, user_msg_id)

        # 输入预处理：检测 URL → web_fetch 预加载（失败不中断本轮，作为附加上下文注入）
        preload_text = ""
        if not onboarding:
            preload_text = await self._preload_urls(message, emit)

        # 第 2 步 上下文加载（冻结快照）
        trigger_rounds = self.config.get("compression_trigger_rounds", 20)
        head_rounds = self.config.get("head_protected_rounds", 2)

        _sp = tracer.span_start("context_load", input={
            "session_id": sid, "onboarding": onboarding,
            "trigger_rounds": trigger_rounds,
            "head_protected_rounds": head_rounds,
        })
        try:
            system_prompt = self._build_system_prompt(
                onboarding, location, sid)
            history = self.sessions.load_recovery_context(sid, head_rounds)
            # 当前用户消息已提前落库：从历史中剔除，避免与 prompt 尾部重复拼接
            history = [m for m in history if m.get("id") != user_msg_id]

            # 压缩触发判定（纯轮次）：水位后原文轮数达阈值即压缩
            # history 里带 id 的是原文（摘要块无 id），据此算轮数
            raw_msg_count = sum(1 for m in history if m.get("id"))
            raw_rounds = (raw_msg_count + 1) // 2  # 向上取整为轮

            _compressed = False
            if raw_rounds >= trigger_rounds:
                _comp_sp = tracer.span_start("history_compression", input={
                    "raw_rounds": raw_rounds,
                    "trigger_rounds": trigger_rounds,
                    "triggered": True})
                try:
                    history, _compressed = await self._compress_history(sid, history)
                    _comp_sp.end(output={"compressed": True})
                except Exception:
                    _comp_sp.end(level="ERROR")
                    raise
            # 消息 id 仅供压缩水位推进，送 LLM 前剔除
            history = [{"role": m["role"], "content": m["content"]}
                       for m in history]
            _sp.end(output={
                "history_rounds": len(history),
                "raw_rounds": raw_rounds,
                "compressed": _compressed,
                "system_prompt": mark_preview(
                    system_prompt, content_type="system_prompt"),
                # 完整历史消息全文（与业务加载的 history 完全一致，不截断）
                "history": [
                    {"role": m["role"], "round_index": idx,
                     **mark_preview(m["content"],
                                    content_type="history_message")}
                    for idx, m in enumerate(history)
                ],
            })
        except Exception:
            _sp.end(level="ERROR")
            raise

        chat_snap = self.providers.snapshot_for("chat")
        chat_configured = chat_snap is not None
        llm_available = chat_configured and self.llm.breaker(
            chat_snap.model_id).allow()

        # 近期对话上下文：供收敛分析节点消解悬空指代、理解对话脉络
        recent_history = "\n".join(
            f"{'用户' if m['role'] == 'user' else 'AI'}：{m['content'] or ''}"
            for m in history[-6:])

        # ---- 收敛式理解：快速预判（§3.1） ----
        quick_result: QuickIntentResult | None = None
        understanding: Understanding | None = None
        strategy: ResponseStrategy | None = None  # 响应策略快照（v3，引导期/关闭时 None）
        if not onboarding and chat_configured:
            _sp = tracer.span_start("quick_intent", input={
                "message": message})
            try:
                quick_result = await self.intent_parser.quick_intent(
                    message, session_id=sid,
                    recent_history=recent_history)
                _sp.end(output={
                    "needs_convergence": quick_result.needs_convergence,
                    "hypothesis": quick_result.intent_hypothesis,
                    "reason": quick_result.complexity_reason,
                    "complexity_hint": quick_result.complexity_hint,
                    "consistency_corrected": quick_result.consistency_corrected,
                    "think_mode": think_mode,
                })
                # 用户指定思考模式优先（Web 端）：覆盖模型自我决策，
                # 下游收敛环/篇幅档位自动跟随有效值；
                # auto（IM 等无 UI 渠道）保留模型判断
                if think_mode == "deep" and not quick_result.needs_convergence:
                    quick_result.needs_convergence = True
                    quick_result.complexity_reason = "用户指定深度思考"
                elif think_mode == "quick" and quick_result.needs_convergence:
                    quick_result.needs_convergence = False
                    quick_result.complexity_reason = "用户指定快速回复"
                # 思考过程外露：快速预判结论（用户指定时标注来源）
                if think_mode == "deep":
                    _mode_tail = "（用户指定深度思考，进入收敛环）"
                elif think_mode == "quick":
                    _mode_tail = "（用户指定快速回复，快速通道）"
                else:
                    _mode_tail = ("（需深度收敛：" + quick_result.complexity_reason + "）"
                                  if quick_result.needs_convergence else "（简单，快速通道）")
                await emit("thinking_delta", {
                    "text": f"【快速预判】{quick_result.intent_hypothesis}{_mode_tail}\n"})
            except Exception:
                _sp.end(level="ERROR")
                raise

        # ---- 策略决策任务（v3 §二）：输入不含 memories/Understanding（正交性） ----
        # 快速通道：与检索/意图三路并行；收敛通道：与收敛环并行。
        # 耗时均被掩盖，零净增；若等收敛环结束再决策，实测被超时吞掉 100% fallback
        async def _run_strategy():
            if not self.config.get("strategy_engine_enabled", True):
                return None
            return await self._run_strategy_decision(
                sid, message, quick_result, tracer, emit,
                emotion=self._read_current_emotion())

        # ---- 收敛环（§3.5）仅在 LLM 可用 + 需深度收敛时进入 ----
        max_rounds = self.config.get("convergence_max_rounds", 2)
        _convergence_done = False
        _conv_strategy_task = None
        if (not onboarding and quick_result and quick_result.needs_convergence
                and llm_available):
            # 策略决策与收敛环同时启动：策略不依赖理解包，收敛环耗时完全掩盖决策耗时
            _conv_strategy_task = asyncio.create_task(_run_strategy())
            understanding, conv_memories = await self._convergence_loop(
                sid, message, quick_result, history, tracer, emit,
                max_rounds=max_rounds)
            if understanding is None:
                # 检查是否因 elicitation 而返回 None（缺口可枚举 → 走追问分支）
                if self._elicitation_seed is not None:
                    # 不从这里 abort：交给 _pipeline_impl 的 elicitation 判定处理
                    intents = []
                    memories = []
                    _convergence_done = True
                    strategy = await _conv_strategy_task if _conv_strategy_task and not _conv_strategy_task.done() else None
                else:
                    _conv_strategy_task.cancel()
                    return  # 收敛失败（已 emit error），中止
            else:
                # 从理解包提取 intents 给下游，跳过原有检索+意图流程
                intents = [understanding.rich_intent]
                memories = conv_memories
                loaded_ids = [m["id"] for m in conv_memories]
                _convergence_done = True
                # 思考过程外露：收敛后的丰满意图
                await emit("thinking_delta", {"text": _format_intent_thinking(intents)})
            # 收敛通道策略结果：并行任务此时通常已完成，零等待取回
            try:
                strategy = await _conv_strategy_task
            except Exception:  # noqa: BLE001 - 决策失败不阻塞主链
                strategy = None
        else:
            # 快速通道或引导模式
            memories = []
            loaded_ids: list[str] = []
        cited_ids: list[str] = []

        if not onboarding:
            retrieval_context = "\n".join(
                f"{'用户' if m['role'] == 'user' else 'AI'}：{m['content'] or ''}"
                for m in history[-4:])
        else:
            retrieval_context = ""

        async def _run_retrieval():
            """第 3 步 记忆检索（query 用真实提问，不含附件正文）。"""
            nonlocal memories, loaded_ids
            _sp = tracer.span_start("memory_retrieval", input={
                "query": core_query, "llm_available": llm_available,
                "retrieval_context": mark_preview(
                    retrieval_context, content_type="recent_dialogue")})
            try:
                retrieval = await self.retriever.retrieve(
                    core_query, llm_available, session_id=sid,
                    context_text=retrieval_context)
                memories = retrieval.hits + retrieval.related
                loaded_ids = retrieval.loaded_ids
                if memories:
                    await emit("memory_retrieved",
                               {"count": len(memories),
                                "titles": [m["title"] for m in memories]})
                    # 检索结果并入思考过程：区分个人记忆与知识库来源
                    await emit("thinking_delta",
                               {"text": self._format_retrieval_thinking(memories)})
                _span_out = {
                    "count": len(memories),
                    "titles": [m["title"] for m in memories][:20],
                    "memory_details": [
                        {"id": m["id"], "title": m["title"],
                         "source_type": m.get("source_type", ""),
                         "confidence": m.get("confidence", ""),
                         **mark_preview(m.get("detail") or m.get("summary", ""),
                                        content_type="memory_content")}
                        for m in memories[:30]
                    ],
                }
                # 从 diagnostics 字典读取检索质量指标
                _diag = getattr(retrieval, "diagnostics", None) or {}
                for _dk in ("degraded", "vector_hits", "fts_hits", "retrieval_time_ms",
                            "refined_count", "top_vector_score", "gate", "context_chars"):
                    _dv = _diag.get(_dk)
                    if _dv is not None:
                        _span_out[_dk] = _dv
                _sp.end(output=_span_out)
            except Exception:
                _sp.end(level="ERROR")
                raise

        tool_names = [s.name for s in self.registry.all_specs()]

        # 用于收敛环内向 elicitation 传递 seed 的 nonlocal 变量
        self._elicitation_seed = None       # 实例变量：收敛环 → try_elicitation
        self._elicitation_from_gap = False

        async def _run_intent():
            """第 4 步 意图识别（读完整消息：附件/长文本可能本身就是意图载体，
            云端模型处理长输入无本地算力瓶颈）。"""
            _sp = tracer.span_start("intent_parse", input={
                "message": message, "available_tools": tool_names})
            try:
                if onboarding:
                    _intents = [type("I", (), {"id": "i1", "intent_summary": message[:50],
                                               "intent_type": "chat", "tools_needed": [],
                                               "depends_on": []})()]
                else:
                    _intents = await self.intent_parser.parse(
                        message, tool_names, sid,
                        recent_history=history[-6:])
                    # 兜底：外部类意图若未选工具，自动补上 web_search（防止模型凭空回答实时信息）
                    if "web_search" in tool_names:
                        for it in _intents:
                            if it.intent_type == "query_external" and not it.tools_needed:
                                it.tools_needed = ["web_search"]
                _sp.end(output=[{"summary": getattr(i, "intent_summary", ""),
                                 "type": getattr(i, "intent_type", ""),
                                 "tools": getattr(i, "tools_needed", [])} for i in _intents])
                return _intents
            except Exception:
                _sp.end(level="ERROR")
                raise

        # ---- 快速通道策略决策任务已在上方定义：与检索/意图三路并行 ----
        if not _convergence_done and not onboarding and llm_available:
            # 记忆检索/意图识别/策略决策三者互不依赖，并行执行：
            # 第 2 层精筛 LLM 与策略决策的耗时均被掩盖，主链路零净增
            _results = await asyncio.gather(_run_retrieval(), _run_intent(),
                                            _run_strategy(),
                                            return_exceptions=True)
            # 意图解析失败（DegradationError）→ 路由到三态降级
            for _r in _results:
                if isinstance(_r, DegradationError):
                    _decision = _r.decision
                    _tracer = get_tracer()
                    _tracer.record_degradation(_decision)
                    if _decision.state == DegradationState.STATE_3:
                        await emit("error", {"code": 503,
                                             "message": _decision.message or "服务暂不可用，请稍后重试"})
                        return
                    # 态一/态二：继续但记录
                elif isinstance(_r, BaseException):
                    raise _r
            _intent_r = _results[1]
            if isinstance(_intent_r, BaseException):
                # 意图解析降级（态一/态二）：兜底空意图，避免异常对象流入下游格式化/迭代
                intents = []
            else:
                intents = _intent_r
            # 策略结果：_run_strategy 内部已全异常兜底，此处仅防御性提取
            _strategy_r = _results[2]
            strategy = _strategy_r if isinstance(
                _strategy_r, ResponseStrategy) else None
        elif not _convergence_done:
            if not onboarding:
                # 态三：LLM 不可用（熔断/未配置），不再降级硬答
                _decision = decide_degradation(
                    failed_step="chat_llm",
                    error="对话模型不可用（熔断或未配置）",
                    skip_causes_misleading=True,
                    failure_type=FailureType.SYSTEM_FAULT,
                )
                get_tracer().record_degradation(_decision)
                await emit("error", {"code": 503,
                                     "message": _decision.message or "对话模型不可用，请稍后重试"})
                return
            intents = await _run_intent()

        # 思考过程外露：意图理解与任务拆解以 thinking_delta 流式推送给前端
        if not onboarding and not _convergence_done:
            await emit("thinking_delta", {"text": _format_intent_thinking(intents)})

        # ---- 元认知协议（v3 §六）：高复杂度且非排除意图才触发 ----
        # 触发唯一条件：complexity_score≥7 且 intent_type∉排除集 且开关开启；
        # fallback 策略不触发（避免低质量决策叠加高成本环节）
        skeleton: CognitiveSkeleton | None = None
        next_step_seeds: list = []
        if (not onboarding and strategy is not None and intents
                and StrategyEngine.should_trigger_meta(
                    strategy, getattr(intents[0], "intent_type", "chat"),
                    self.config.get("meta_cognitive_enabled", True))):
            skeleton = await self._run_meta_cognitive(
                sid, message, strategy, memories, tracer, emit)

        # ---- 下一步建议：种子提取 + 门槛过滤 ----
        if not onboarding and skeleton is not None \
                and self.next_step.enabled:
            _ns_sp = tracer.span_start("next_step_pipeline", input={
                "skeleton_available": True})
            try:
                next_step_seeds = self.next_step.extract_seeds(skeleton)
                # 门槛过滤延迟到响应合成前（需 depth_level / doc_only / emotion）
            except Exception:  # noqa: BLE001 - 零阻塞
                logger.warning("种子提取失败", exc_info=True)
                next_step_seeds = []
            _ns_sp.end(output={"seeds_count": len(next_step_seeds),
                               "seeds": [{"kind": s.kind, "text": s.text[:60]}
                                         for s in next_step_seeds]})

        # mimo 内置联网搜索：query_external 意图且 chat 模型为 mimo 且开关开启时，
        # 由模型端执行搜索（博查源，带结构化引用），跳过自研 web_search；
        # 非 mimo 模型或开关关闭时仍走自研搜索兜底链路
        builtin_search_tools = None
        if (not onboarding and chat_configured and chat_snap is not None
                and self.config.get("mimo_builtin_search_enabled", True)
                and "xiaomimimo" in (chat_snap.base_url or "")
                and any(getattr(i, "intent_type", "") == "query_external" for i in intents)):
            builtin_search_tools = [{
                "type": "web_search",
                "max_keyword": self.config.get("builtin_search_max_keyword", 3),
                "force_search": True}]
            for it in intents:
                it.tools_needed = [
                    t for t in it.tools_needed if t != "web_search"]
            await emit("thinking_delta",
                       {"text": "【联网搜索】使用模型内置联网搜索（mimo · 博查源）\n"})

        # 显式记忆指令 + 存在图片 → 后台静默把图片存入知识库，不阻塞本轮回复
        # （如"把这张图存到知识库"）。当轮对话仍照常把图作多模态交给模型回应。
        if (not onboarding and images and self.image_kb_fn
                and any(getattr(i, "intent_type", "") == "remember_intent" for i in intents)):
            asyncio.create_task(self._image_kb_task(images))

        # 特殊意图隐式工具补全：soul_feedback / output_preference_feedback
        # 识别后若未指定工具，自动注入 memory_save 将用户反馈存入记忆层；
        # 输出偏好含附件时改注入 format_template_save（提取格式骨架存模板记忆），
        # 即使意图模型已选 memory_save 也强制纠正（附件 + 输出偏好 = 格式绑定）
        if not onboarding:
            for it in intents:
                if it.intent_type == "soul_feedback" and not it.tools_needed:
                    it.tools_needed = ["memory_save"]
                if it.intent_type == "output_preference_feedback":
                    if _extract_attachment_blocks(message) and (
                            not it.tools_needed
                            or it.tools_needed == ["memory_save"]):
                        it.tools_needed = ["format_template_save"]
                    elif not it.tools_needed:
                        it.tools_needed = ["memory_save"]

        # ---- 追问式补充信息：elicitation 判定与触发（产品方案 §05/06） ----
        # 触发条件：(未 onboarding) AND (策略已产出) AND (置信度<阈值 OR gap检测到缺口)
        elicitation_triggered = False
        if (not onboarding and strategy is not None
                and (intents or self._elicitation_from_gap)
                and self.config.get("elicitation_confidence_threshold", -1) > 0):
            _elicit_sp = tracer.span_start("elicitation_check", input={
                "session_id": sid,
                "confidence": getattr(intents[0], "confidence", 1.0) if intents else 1.0,
                "intent_type": getattr(intents[0], "intent_type", "") if intents else "",
                "gap_detected": self._elicitation_from_gap,
            })
            _elicitation = await self._try_elicitation(
                sid, message, intents, strategy,
                self.config.get("elicitation_confidence_threshold", 0.6),
                emit,
                gap_seed=self._elicitation_seed)
            _elicit_sp.end(output={
                "triggered": _elicitation is not None,
                "source": "gap_detect" if self._elicitation_from_gap and _elicitation is not None else "confidence",
            })
            if _elicitation is not None:
                # ask_user 已触发并完成：将 tool_result 注入 tool_results，跳过正常工具执行
                tool_results = [_elicitation]
                elicitation_triggered = True

        # 第 5 步 流程编排
        shared = SharedState()
        if not elicitation_triggered:
            tool_results: list[dict] = []
        skill_text = ""
        if not onboarding:
            # 请求级技能按需追加（第 4 步意图识别后）：命中的活跃技能加载 Level1 SKILL.md
            _skill_sp = None
            try:
                _skill_sp = tracer.span_start("skill_injection", input={
                    "query": mark_preview(message,
                                          content_type="user_message")})
                matched = self.skills.match_skills(
                    message + " " + " ".join(getattr(i, "intent_summary", "") for i in intents))
                matched_names = list(matched)
                for sk in matched:
                    body = self.skills.load_skill(sk)
                    if body:
                        # 技能正文完整注入（不截断）：Langfuse 记录与业务输入一致
                        skill_text += f"\n\n[技能：{sk}]\n{body}"
                        self.skills.record_use(sk)
                _skill_sp.end(output={
                    "matched_skills": matched_names,
                    "skill_text": mark_preview(
                        skill_text, content_type="skill_injection")})
            except Exception as e:  # noqa: BLE001
                logger.warning("技能按需追加失败", exc_info=True)
                if _skill_sp is not None:
                    _skill_sp.end(level="ERROR", status_message=str(e)[:500])
            if not elicitation_triggered:
                dag = build_dag(list(intents), set(tool_names))
                # DAG 环检测降级：向用户说明（thinking_delta 外露 + prompt 注入）
                if dag.degraded:
                    await emit("thinking_delta",
                               {"text": f"【流程编排】{dag.reason}\n"})
                    skill_text += f"\n（注意：{dag.reason}，本轮已降级为直接回答）"
                # 第 6 步 工具执行
                _sp = tracer.span_start("tool_execution", input={
                    "intent_count": len(intents),
                    "intents": [
                        {"type": getattr(i, "intent_type", ""),
                         "summary": getattr(i, "intent_summary", ""),
                         "tools": getattr(i, "tools_needed", [])}
                        for i in intents
                    ],
                    "dag_order": [list(layer) for layer in dag.order] if hasattr(dag, "order") else [],
                    "all_tool_names": tool_names,
                })
                try:
                    tool_results = await self._execute_tools(intents, dag, shared, message, emit, sid)
                    _sp.end(output={
                        "count": len(tool_results),
                        "tool_results": [
                            {"tool": r.get("tool"), "ok": r.get("ok"),
                             "error": r.get("error"),
                             **mark_preview(r.get("result"),
                                            content_type="tool_result")}
                            for r in tool_results
                        ],
                        "shared_keys": sorted(shared._data.keys()),
                        "deferred_writes": len(shared.deferred_writes),
                        "deferred_docs": len(shared.deferred_docs),
                    })
                except BaseException:
                    # BaseException：手动停止（CancelledError）时 span 同样收尾，避免悬空 trace
                    _sp.end(level="ERROR")
                    raise

        # 第 7 步 响应合成与输出（流式）
        assistant_text, citations = "", []
        # 流式增量缓冲提前定义：中断补救（CancelledError）需读取已产出部分
        buf: list[str] = []
        # 纯导出模式：已登记延迟导出文档 → 正文只写入文档，
        # 对话中不再重复展示，只回一句确认 + 下载卡片
        doc_only = bool(shared.deferred_docs)
        _sp = tracer.span_start("response_synthesis", input={
            "history_rounds": len(history),
            "raw_rounds": raw_rounds,
            "memories_count": len(memories),
            "tool_results_count": len(tool_results),
            "memory_titles": [m["title"] for m in memories[:20]],
            "history_sample": [
                {"role": m["role"],
                 "round_index": max(0, len(history) - 6) + idx,
                 **mark_preview(m["content"],
                                content_type="history_message")}
                for idx, m in enumerate(history[-6:])
            ],
            "skill_text": mark_preview(
                skill_text, content_type="skill_injection"),
            "preload_text": mark_preview(
                preload_text, content_type="url_preload"),
            "tool_result_summaries": [
                {"tool": r.get("tool"), "ok": r.get("ok"),
                 **mark_preview(r.get("result"),
                                content_type="tool_result")}
                for r in (tool_results or [])[:10]
            ],
            # 策略快照全量（含可解释性字段，v3 §十）：与业务注入 prompt 一致
            "strategy": (mark_preview(strategy.span_snapshot(),
                                      content_type="strategy_snapshot")
                         if strategy else None),
            "skeleton": (mark_preview(skeleton.to_dict(),
                                      content_type="cognitive_skeleton")
                         if skeleton else None),
        })
        _next_step_shown = None  # 建议句落盘数据（模型不可用/熔断等分支安全默认值）
        try:
            if not chat_configured and not onboarding:
                # 态三：模型不可用 → 记录 decision_reason
                _decision = decide_degradation(
                    failed_step="response_synthesis",
                    error="对话模型不可用",
                    skip_causes_misleading=True,
                    failure_type=FailureType.SYSTEM_FAULT,
                )
                get_tracer().record_degradation(_decision)
                assistant_text = "当前对话模型不可用，请在设置页检查模型配置。"
                await emit("content_delta", {"text": assistant_text})
                # 不可用提示文案不应被导出成文档
                doc_only = False
                shared.deferred_docs.clear()
            else:
                # 场景篇幅档位（纯规则，零 LLM）：brief 寒暄 / normal 常规 / detailed 深度；
                # 开关关闭时恒为 normal（不注入指令，行为与画像默认一致）
                depth_level = "normal"
                if self.config.get("response_depth_enabled", True):
                    depth_level = self._decide_depth_level(
                        message, quick_result, intents, memories, tool_results)
                # 门槛过滤（需 depth_level + doc_only + emotion 就绪）
                if next_step_seeds:
                    _emotion = self._read_current_emotion()
                    next_step_seeds = self.next_step.filter_gates(
                        next_step_seeds, emotion=_emotion, db=self.db,
                        session_id=sid, depth_level=depth_level,
                        doc_only=doc_only,
                        elicitation_active=elicitation_triggered)
                prompt = self._build_final_prompt(system_prompt, history, message,
                                                  tool_results, memories, onboarding,
                                                  skill_text, preload_text,
                                                  depth_level=depth_level,
                                                  strategy=strategy,
                                                  skeleton=skeleton,
                                                  next_step_seeds=next_step_seeds)
                # 关闭追问后新消息：注入临时决策指令
                if not elicitation_triggered:
                    row = self.db.query_one(
                        "SELECT id, status FROM elicitations "
                        "WHERE session_id=? AND status='closed' AND close_reason='user_x' "
                        "ORDER BY resolved_at DESC LIMIT 1", (sid,))
                    if row:
                        supplement = PROMPTS.load_raw(
                            "agent/prompts/elicitation_supplement")
                        prompt[0]["content"] = (prompt[0]["content"]
                                                + "\n\n" + supplement)
                valid_ids = {m["id"] for m in memories}

                # 推理模型（DeepSeek 等）的原生思考过程同样以 thinking_delta 外露
                async def on_reasoning(text: str) -> None:
                    await emit("thinking_delta", {"text": text})

                # 内置搜索引用源（流式首包到达）：收集并即时外露到思考过程
                search_refs: list[dict] = []

                async def on_annotations(items) -> None:
                    items = [a for a in (items or []) if a.get("url")]
                    if not items:
                        return
                    search_refs.extend(items)
                    titles = "、".join(
                        (a.get("title") or a.get("url"))[:30] for a in items[:5])
                    await emit("thinking_delta",
                               {"text": f"【联网搜索】命中 {len(items)} 个来源：{titles}\n"})

                _llm_call_count += 1
                _model_id = getattr(chat_snap, "model_id", None)
                if doc_only:
                    await emit("thinking_delta",
                               {"text": "【文档生成】检测到导出请求：正文将直接写入文档，不在对话中展示\n"})
                _doc_chars, _doc_progress = 0, 0
                try:
                    async for chunk in self.llm.stream(chat_snap, prompt, source="main_chat",
                                                       session_id=sid, images=images,
                                                       on_reasoning=on_reasoning,
                                                       on_annotations=on_annotations,
                                                       extra_tools=builtin_search_tools):
                        buf.append(chunk)
                        if doc_only:
                            # 正文不外露；以思考过程定期报进度，避免前端长时间无反馈
                            _doc_chars += len(chunk)
                            if _doc_chars - _doc_progress >= 1000:
                                _doc_progress = _doc_chars
                                await emit("thinking_delta",
                                           {"text": f"【文档生成】正文已生成约 {_doc_chars} 字…\n"})
                        else:
                            await emit("content_delta", {"text": chunk})
                    # 回复尾部追加联网来源列表（去重，随正文入库可溯源）
                    if search_refs and buf:
                        seen, uniq = set(), []
                        for a in search_refs:
                            if a["url"] not in seen:
                                seen.add(a["url"])
                                uniq.append(a)
                        src_md = "\n\n**联网来源**\n" + "\n".join(
                            f"{i + 1}. [{(a.get('title') or a['url'])[:60]}]({a['url']})"
                            for i, a in enumerate(uniq[:8]))
                        buf.append(src_md)
                        if not doc_only:
                            await emit("content_delta", {"text": src_md})
                except CircuitOpenError:
                    # 态三：熔断兜底 → 记录 decision_reason
                    _decision = decide_degradation(
                        failed_step="response_synthesis",
                        error="对话模型熔断中",
                        skip_causes_misleading=True,
                        failure_type=FailureType.SYSTEM_FAULT,
                    )
                    get_tracer().record_degradation(_decision)
                    fallback = "对话模型暂时不可用（熔断中），请稍后重试或切换模型。"
                    await emit("content_delta", {"text": fallback})
                    buf = [fallback]
                    # 熔断兜底文案不应被导出成文档
                    doc_only = False
                    shared.deferred_docs.clear()
                raw = "".join(buf)
                # 建议句解析：从 LLM 输出中分离正文与分隔符后的建议句
                raw, _suggestion_text = parse_suggestion(raw)
                _next_step_shown = None
                if _suggestion_text:
                    _next_step_shown = {"text": _suggestion_text}
                assistant_text, citations = rs.extract_citations(
                    raw, valid_ids)
                # low 待确认声明：用户在本轮明确确认/否认早前推断 → 升级/记录
                assistant_text, mem_confirm = rs.extract_memory_confirm(
                    assistant_text)
                if mem_confirm:
                    try:
                        await self.lifecycle.confirm_low(
                            mem_confirm["id"], mem_confirm["confirmed"])
                    except Exception:  # noqa: BLE001
                        logger.warning("low 确认处理失败", exc_info=True)
                # Strip duplicate Mermaid blocks when diagram tool succeeded
                if any(tr.get("ok") and tr.get("tool") in ("render_flowchart", "render_mermaid") for tr in (tool_results or [])):
                    assistant_text = rs.strip_mermaid_blocks(assistant_text)
                if citations:
                    cited_ids = citations
                    refs = [{"id": mid, "title": next(
                        (m["title"] for m in memories if m["id"] == mid), mid)}
                        for mid in citations]
                    await emit("citations", {"refs": refs})
                # generate_document 确定性兑底：模型未在正文保留下载链接时，
                # 自动追加文件卡片，保证下载入口必然可见
                _extra = self._append_file_cards(assistant_text, tool_results)
                if _extra:
                    assistant_text += _extra
                    await emit("content_delta", {"text": _extra})
                _sp.end(output=assistant_text)
        except asyncio.CancelledError:
            # 中断补救：刷新/关页导致 SSE 断开取消、600s 超时取消时，
            # 已流式产出的部分回复落库，刷新后历史仍可见（与 Langfuse 已记录内容一致）
            self._save_partial_reply(sid, buf, think_parts)
            _sp.end(level="ERROR")
            raise
        except Exception:
            _sp.end(level="ERROR")
            raise

        # 延迟写入：file_write 等工具在回复生成后执行，将回复正文写入文件
        if shared.deferred_writes:
            for dw in shared.deferred_writes:
                try:
                    result = await self.executor.execute_tool(
                        "file_write",
                        {"path": dw["path"], "content": assistant_text,
                         "mode": dw.get("mode", "w")},
                        emit=emit)
                    tool_results.append({"tool": "file_write",
                                         "result": result.get("result"),
                                         "ok": result.get("ok"),
                                         "error": result.get("error"),
                                         "deferred_done": True,
                                         "path": dw["path"]})
                    await emit("thinking_delta",
                               {"text": f"【工具调用】file_write 延迟写入完成：{dw['path']}\n"})
                except Exception as e:  # noqa: BLE001
                    logger.warning("延迟 file_write 失败：%s %s", dw["path"], e)
                    tool_results.append({"tool": "file_write",
                                         "result": False,
                                         "ok": False,
                                         "error": str(e),
                                         "deferred_done": False,
                                         "path": dw["path"]})

        # 延迟导出：generate_document 将主回复正文导出为文档。
        # doc_only 模式：正文只进文档，对话回复改为简短确认 + 下载卡片
        if shared.deferred_docs and assistant_text.strip():
            doc_body = assistant_text
            for dd in shared.deferred_docs:
                try:
                    result = await self.executor.execute_tool(
                        "generate_document",
                        {"title": dd["title"], "format": dd.get("format", "docx"),
                         "content": doc_body},
                        emit=emit)
                    tool_results.append({"tool": "generate_document",
                                         "result": result.get("result"),
                                         "ok": result.get("ok"),
                                         "error": result.get("error"),
                                         "deferred_done": True})
                    await emit("thinking_delta",
                               {"text": f"【工具调用】generate_document 导出完成：{dd['title']}\n"})
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "延迟 generate_document 失败：%s %s", dd["title"], e)
                    tool_results.append({"tool": "generate_document",
                                         "result": None, "ok": False,
                                         "error": str(e), "deferred_done": False})
            if doc_only:
                _docs_ok = any(
                    r.get("tool") == "generate_document" and r.get("ok")
                    and r.get("deferred_done") for r in tool_results)
                if _docs_ok:
                    # 对话只留确认句 + 文件卡片，正文不重复展示
                    assistant_text = "文档已生成，点击下方卡片下载："
                    assistant_text += self._append_file_cards(
                        assistant_text, tool_results)
                    await emit("content_delta", {"text": assistant_text})
                else:
                    # 导出失败兜底：正文不能丢，退回对话中完整展示
                    assistant_text = (doc_body
                                      + "\n\n> ⚠️ 文档导出失败，已改为在对话中展示完整内容")
                    await emit("content_delta", {"text": assistant_text})
            else:
                # 非纯导出模式：下载链接追加到已展示的回复末尾
                _card = self._append_file_cards(assistant_text, tool_results)
                if _card:
                    assistant_text += _card
                    await emit("content_delta", {"text": _card})

        # 第 8 步 后置处理
        _sp = tracer.span_start("post_process", input={
                                "session_id": sid, "cited_ids": cited_ids,
                                "loaded_ids": loaded_ids or []})
        try:
            # 从工具结果中提取图形数据供持久化（刷新后可恢复渲染）
            _visuals = []
            for tr in (tool_results or []):
                if tr.get("ok") and tr.get("tool") in ("render_flowchart", "render_mermaid"):
                    vd = tr.get("result")
                    if isinstance(vd, dict) and vd.get("type"):
                        _visuals.append({"type": vd["type"], "data": vd})
            msg_id = self.sessions.append_message(sid, "assistant", assistant_text,
                                                  citations=[{"id": c}
                                                             for c in cited_ids],
                                                  thinking="".join(
                                                      think_parts) or None,
                                                  visuals=_visuals or None,
                                                  strategy_snapshot=(
                                                      strategy.db_snapshot()
                                                      if strategy else None),
                                                  skeleton_snapshot=(
                                                      skeleton.to_dict()
                                                      if skeleton else None),
                                                  next_step_shown=_next_step_shown)
            _signal_shape = await self._post_process(sid, msg_id, assistant_text, loaded_ids, cited_ids,
                                                     message, onboarding, user_msg_id=user_msg_id, intents=intents)
            _sp.end(output={"message_id": msg_id, "cited": cited_ids,
                            "signal_shape": _signal_shape, "budget_checked": True})
        except Exception:
            _sp.end(level="ERROR")
            raise
        # 策略执行完成事件（v3 §事件总线）：反馈归因/观测订阅，零阻塞
        if strategy is not None and self.bus:
            try:
                from infrastructure.event_bus import EVT_STRATEGY_EXECUTED
                self.bus.publish_nowait(EVT_STRATEGY_EXECUTED, {
                    "session_id": sid, "message_id": msg_id,
                    "strategy": strategy.db_snapshot()})
            except Exception:  # noqa: BLE001
                pass
        trace.update(output=assistant_text,
                     metadata={"internal_trace_id": get_trace_id(),
                               "images": len(images) if images else 0,
                               "image_files": persisted_imgs,
                               "regenerate": regenerate,
                               "cited": cited_ids, "message_id": msg_id,
                               "total_latency_ms": round((time.monotonic() - _start_time) * 1000),
                               "llm_call_count": _llm_call_count,
                               "model_id": _model_id})

        await emit("turn_completed", {"message_id": msg_id})

        # v2 情绪快照推送：前端通过 SSE 实时更新情绪徽标（无需轮询）
        if self.mood and self.config.get("mood_enabled", True):
            try:
                row = self.db.query_one("SELECT * FROM mood_state WHERE id=1")
                if row:
                    row = dict(row)  # sqlite3.Row 不支持 .get()，转 dict 后用默认值访问
                    from soul.mood_manager import _mood_cn
                    await emit("mood_updated", {
                        "ai_mood": row["ai_mood"],
                        "ai_mood_cn": _mood_cn(row["ai_mood"]),
                        "ai_intensity": round(self.mood._decay(
                            row["ai_intensity"], row["ai_updated_at"]), 2),
                        "ai_attribution": row.get("ai_attribution", ""),
                        "ai_active_action": row.get("active_action", ""),
                    })
            except Exception:  # noqa: BLE001
                pass  # 静默降级，不影响对话

    async def _image_kb_task(self, images) -> None:
        """后台静默将当轮图片存入知识库（fire-and-forget，失败不影响对话）。"""
        from langfuse.integration import get_tracer
        tr = get_tracer().trace_start("image_kb_store", input={
            "image_count": len(images) if images else 0})
        try:
            await self.image_kb_fn(images)
            tr.end(output={"ok": True})
        except Exception as e:  # noqa: BLE001
            logger.warning("图片存入知识库失败", exc_info=True)
            tr.end(level="ERROR", status_message=str(e)[:500])

    def _save_partial_reply(self, sid: str, buf: list[str],
                            think_parts: list[str]) -> None:
        """中断补救：把已流式产出的部分回复落库（标注未完成）。
        同步写入（无 await）：取消处理中不得再挂起，避免二次取消丢失。"""
        partial = "".join(buf).strip()
        if not partial:
            return
        try:
            # 清理可能已生成的 citations 尾部声明（中断场景不做引用登记）
            partial, _ = rs.extract_citations(partial, set())
            # 清理可能已产出的建议句分隔符（中断场景建议句不完整，剥离）
            partial = strip_suggestion_from_partial(partial)
            self.sessions.append_message(
                sid, "assistant",
                partial + "\n\n> ⚠️ 本回复未完成：生成已中断，以上为已生成部分",
                thinking="".join(think_parts) or None)
            logger.info("中断补救：部分回复已落库 session=%s chars=%d",
                        sid, len(partial))
        except Exception:  # noqa: BLE001
            logger.warning("中断补救落库失败", exc_info=True)

    def _persist_images(self, images: list[str] | None) -> list[str] | None:
        """将当轮图片（dataURL）落盘 data/chat_images/，返回文件名列表。
        随用户消息持久化，刷新/切回会话后历史气泡仍可回看；失败不阻断对话。"""
        if not images:
            return None
        import base64
        import uuid
        from pathlib import Path
        out: list[str] = []
        img_dir = Path(self.sessions.data_dir) / "chat_images"
        img_dir.mkdir(parents=True, exist_ok=True)
        for du in images:
            try:
                head, _, b64 = du.partition(",")
                mime = head.split(";")[0].removeprefix("data:")
                ext = {"image/png": ".png", "image/jpeg": ".jpg",
                       "image/webp": ".webp", "image/gif": ".gif",
                       "image/bmp": ".bmp"}.get(mime, ".png")
                fname = f"img_{uuid.uuid4().hex[:12]}{ext}"
                (img_dir / fname).write_bytes(base64.b64decode(b64))
                out.append(fname)
            except Exception:  # noqa: BLE001
                logger.warning("对话图片落盘失败", exc_info=True)
        return out or None

    def _format_retrieval_thinking(self, memories: list[dict]) -> str:
        """把检索命中结果格式化为思考过程文本，区分记忆与知识库来源。"""
        ids = [m["id"] for m in memories]
        stype: dict[str, str] = {}
        if ids:
            ph = ",".join("?" * len(ids))
            for r in self.db.query_all(
                    f"SELECT id, source_type FROM memories WHERE id IN ({ph})",
                    tuple(ids)):
                stype[r["id"]] = r["source_type"]
        personal, kb = [], []
        for m in memories:
            if stype.get(m["id"]) == "knowledge":
                kb.append(m["title"])
            else:
                personal.append(m["title"])
        parts = []
        if personal:
            parts.append(f"命中记忆 {len(personal)} 条：{'、'.join(personal)}")
        if kb:
            parts.append(f"命中知识库 {len(kb)} 条：{'、'.join(kb)}")
        return "【记忆检索】" + "；".join(parts) + "\n"

    def _read_current_emotion(self) -> EmotionState | None:
        """读 MoodManager 当前衰减后的用户情绪（零 LLM，v3 策略引擎输入）。"""
        if not self.mood:
            return None
        try:
            row = self.mood.db.query_one("SELECT * FROM mood_state WHERE id=1")
            if row:
                rd = dict(row)
                em = rd.get("user_mood", "neutral") or "neutral"
                ei = self.mood._decay(
                    rd.get("user_intensity", 0) or 0, rd.get("user_updated_at"))
                return EmotionState(valence=em, intensity=round(ei, 2))
        except Exception:  # noqa: BLE001 - 静默降级
            logger.warning("情绪状态读取失败", exc_info=True)
        return None

    async def _run_strategy_decision(self, sid, message, quick_result, tracer,
                                     emit, rich_intent=None, emotion=None,
                                     focus=None) -> ResponseStrategy | None:
        """策略决策编排（v3 §十）：span 埋点 + narrative 外露 + 事件广播。

        失败时 span 强制携带三元 metadata（fallback_used/failure_reason/
        fallback_strategy_snapshot），支撑 Langfuse fallback 比例分析。
        引导期/开关关闭/预判缺失时返回 None，调用方照常继续。
        """
        if quick_result is None:
            return None
        _sp = tracer.span_start("strategy_decision", input={
            "message": mark_preview(message, content_type="user_message"),
            "complexity_hint": getattr(quick_result, "complexity_hint", None),
            "needs_convergence": getattr(quick_result, "needs_convergence", None),
            "consistency_corrected": getattr(
                quick_result, "consistency_corrected", False),
            "emotion": (f"{emotion.valence}({emotion.intensity})"
                        if emotion else None),
            "channel": "convergence" if rich_intent is not None else "fast",
        })
        try:
            priors = self.strategy_engine.load_priors()
            inputs = StrategyInputs(
                message=message, quick_result=quick_result, emotion=emotion,
                priors=priors, rich_intent=rich_intent, focus=focus)
            result = await self.strategy_engine.decide(inputs, session_id=sid)
        except Exception as e:  # noqa: BLE001 - 零阻塞铁律：静默降级不影响主链
            logger.warning("策略决策编排异常：%s", e, exc_info=True)
            result = self.strategy_engine._fallback("llm_error")
        if result.fallback_used:
            # span 失败三元 metadata（v3 R7）：_Span.end 不收 metadata，先 update 再 end
            _sp.update(metadata={"fallback_used": True,
                                 "failure_reason": result.failure_reason,
                                 "fallback_strategy_snapshot": result.db_snapshot()})
            _sp.end(level="ERROR",
                    status_message=result.failure_reason[:500],
                    output=result.span_snapshot())
        else:
            _sp.end(output=result.span_snapshot())
        # 思考过程外露：自然语言 narrative（非字段值，v3 R4）
        try:
            await emit("thinking_delta",
                       {"text": f"【策略决策】{result.strategy_narrative}\n"})
        except Exception:  # noqa: BLE001
            pass
        # 事件广播（观测/日志订阅，零阻塞）
        if self.bus:
            try:
                from infrastructure.event_bus import EVT_STRATEGY_DECIDED
                self.bus.publish_nowait(EVT_STRATEGY_DECIDED, {
                    "session_id": sid, "strategy": result.db_snapshot(),
                    "fallback_used": result.fallback_used})
            except Exception:  # noqa: BLE001
                pass
        return result

    async def _run_meta_cognitive(self, sid, message, strategy, memories,
                                  tracer, emit) -> CognitiveSkeleton | None:
        """元认知骨架提取编排（v3 §十）：span + narrative 外露 + 事件。

        失败（超时/LLM 不可用/解析失败）返回 None，态一跳过骨架直接生成。
        """
        _sp = tracer.span_start("skeleton_extraction", input={
            "message": mark_preview(message, content_type="user_message"),
            "complexity_score": strategy.complexity_score,
            "insight_hooks": strategy.insight_hooks,
            "memory_titles": [m["title"] for m in (memories or [])[:10]],
        })
        try:
            memories_text = "\n".join(
                f"[{m['id']}] {m['title']}：{m.get('detail', m.get('summary', ''))}"
                for m in (memories or [])[:15])
            skeleton = await self.metacog.extract(
                message, strategy, memories_text=memories_text, session_id=sid)
        except Exception as e:  # noqa: BLE001 - 零阻塞铁律
            logger.warning("元认知编排异常：%s", e, exc_info=True)
            skeleton = None
        if skeleton is None:
            _sp.update(metadata={"fallback_used": True,
                                 "failure_reason": "extract_failed_or_timeout"})
            _sp.end(level="ERROR",
                    status_message="骨架提取失败或超时，跳过骨架直接生成（态一）")
            return None
        _sp.end(output=skeleton.to_dict())
        # 思考过程外露：骨架摘要（自然语言，非 JSON）
        try:
            _el = skeleton.expert_lens
            _summary = "已构建思考骨架"
            if skeleton.reframe.get("needed") and skeleton.reframe.get("real_question"):
                _summary += f"：真正的问题是「{skeleton.reframe['real_question'][:40]}」"
            if _el.get("non_obvious_insight"):
                _summary += "，将带出一个关键洞察"
            await emit("thinking_delta", {"text": f"【思考骨架】{_summary}\n"})
        except Exception:  # noqa: BLE001
            pass
        if self.bus:
            try:
                from infrastructure.event_bus import EVT_SKELETON_EXTRACTED
                self.bus.publish_nowait(EVT_SKELETON_EXTRACTED,
                                        {"session_id": sid,
                                         "has_insight": bool(
                                             skeleton.expert_lens.get("non_obvious_insight"))})
            except Exception:  # noqa: BLE001
                pass
        return skeleton

    # ---- 工具执行子流程（直接用 emit，工具状态中途可推思考流） --------
    @staticmethod
    def _append_file_cards(text: str, tool_results: list[dict]) -> str:
        """收集 generate_document 成功产物中未出现在正文的下载链接，
        返回需追加的 Markdown 片段（空串表示无需追加）。"""
        parts = []
        for r in tool_results or []:
            if r.get("tool") != "generate_document" or not r.get("ok"):
                continue
            res = r.get("result")
            # 延迟导出的占位结果 result 为 bool，尚未真正生成，跳过
            if not isinstance(res, dict):
                continue
            url, fname = res.get("download_url"), res.get("filename")
            if url and fname and url not in (text or ""):
                parts.append(f"\n\n[{fname}]({url})")
        return "".join(parts)

    async def _try_elicitation(self, sid, message, intents, strategy,
                               confidence_threshold, emit, gap_seed=None) -> dict | None:
        """尝试触发追问式补充信息。

        返回 None = 不触发追问（继续正常流程）
        返回 dict = ask_user 已触发并完成，含 tool_result 格式的结果

        gap_seed: 从收敛环 gap_detect 传入的已判定可枚举的 seed（跳过 clarification_router）
        """
        # 优先使用 gap 检测出的 seed（已通过 clarification_router 判定）
        if gap_seed is not None:
            ask_params = {
                "questions": gap_seed["questions"],
                "reason": gap_seed.get("reason", ""),
            }
            result = await self.executor._execute_ask_user(ask_params, "gap_detect", emit, session_id=sid)
            if result.get("ok"):
                return {"tool": "ask_user", "ok": True, "result": result.get("result", "")}
            return None

        # 门槛：意图置信度低于阈值 或 走诚实澄清路径
        first_intent = intents[0] if intents else None
        if first_intent is None:
            return None
        confidence = getattr(first_intent, "confidence", 1.0) or 1.0
        if confidence >= confidence_threshold:
            return None

        # 检查会话级上限（计数门槛）和关闭标记（blocked 门槛）——两条独立判定
        row = self.db.query_one(
            "SELECT COUNT(*) as cnt FROM elicitations WHERE session_id=? AND status IN ('pending','answered_all','closed')",
            (sid,))
        max_per = self.config.get("elicitation_max_per_session", 3)
        if row and row["cnt"] >= max_per:
            return None
        row2 = self.db.query_one(
            "SELECT elicitation_blocked FROM sessions WHERE session_id=?", (sid,))
        if row2 and row2["elicitation_blocked"]:
            return None

        # 调用 clarification_router 判定可枚举性（零阻塞：异常跳过追问，主对话不受影响）
        gap = getattr(first_intent, "intent_summary", "") or ""
        try:
            seed = await self.strategy_engine.clarification_router(
                sid, message, gap,
                {k: self.config.get(k) for k in (
                    "elicitation_max_questions", "elicitation_max_per_session",
                    "elicitation_web_ttl_minutes", "elicitation_im_ttl_hours",
                ) if self.config.get(k) is not None})
        except Exception:  # noqa: BLE001 - 零阻塞：LLM 异常静默跳过追问
            logger.warning("clarification_router 异常，跳过追问", exc_info=True)
            return None
        if seed is None:
            return None

        # 组装 ask_user 入参并执行（通过 tool_executor 的 _execute_ask_user 暂停/恢复）
        ask_params = {
            "questions": seed["questions"],
            "reason": seed.get("reason", ""),
        }
        result = await self.executor._execute_ask_user(ask_params, "elicitation", emit, session_id=sid)
        if not result.get("ok"):
            return None
        # 返回 tool_result 格式（与正常工具执行一致的格式）
        return {"tool": "ask_user", "ok": True, "result": result.get("result", "")}

    async def _execute_tools(self, intents, dag, shared, message, emit, sid=None) -> list[dict]:
        tool_results: list[dict] = []
        self._replan_count = 0
        self._replan_max = self.config.get("replan_max_per_request", 3)
        by_id = {it.id: it for it in intents}
        for layer in dag.order:
            # 同层内并行执行（asyncio.gather）
            layer_intents = [by_id.get(iid) for iid in layer
                             if by_id.get(iid) and by_id[iid].tools_needed]
            if not layer_intents:
                continue
            results = await asyncio.gather(
                *[self._run_intent_tools(it, shared, message, emit, sid) for it in layer_intents])
            for r in results:
                tool_results.extend(r)
        return tool_results

    async def _run_intent_tools(self, intent, shared, message, emit, sid=None) -> list[dict]:
        """执行单个意图的工具序列（含 Replan），前序依赖由 shared feed 给
        LLM 参数推断。
        file_write 标记 __FROM_RESPONSE__ 时跳过执行，等主回复生成后写入。"""
        out: list[dict] = []
        deps = shared.get_for_intent(
            intent.depends_on) if intent.depends_on else {}
        for tool_name in intent.tools_needed:
            await emit("tool_executing", {"tool_name": tool_name, "status": "running"})
            # 工具调用状态并入思考过程流，不再依赖外部独立展示
            await emit("thinking_delta",
                       {"text": f"【工具调用】正在调用 {tool_name}…\n"})
            params = (await self._format_template_save_params(
                message, sid, intent.intent_summary)
                if tool_name == "format_template_save"
                else await self._memory_save_params(message, sid)
                if tool_name == "memory_save"
                else await self._infer_params(tool_name, message, deps, sid))
            # file_write 延迟写入：content 由主回复填充，此时跳过执行
            if tool_name == "file_write" and params.get("content") == "__FROM_RESPONSE__":
                shared.deferred_writes.append({
                    "path": params.get("path", "output.txt"),
                    "mode": params.get("mode", "w"),
                })
                shared.put(intent.id, tool_name, 0, True)
                await emit("thinking_delta",
                           {"text": f"【工具调用】{tool_name} 已登记，回复生成后自动写入 {params['path']}\n"})
                out.append({"tool": tool_name, "result": True,
                            "ok": True, "deferred": True,
                            "path": params["path"]})
                continue
            # generate_document 延迟导出：content 由主回复填充，此时跳过执行
            if tool_name == "generate_document" and params.get("content") == "__FROM_RESPONSE__":
                shared.deferred_docs.append({
                    "title": params.get("title", "文档"),
                    "format": params.get("format", "docx"),
                })
                shared.put(intent.id, tool_name, 0, True)
                await emit("thinking_delta",
                           {"text": f"【工具调用】{tool_name} 已登记，回复生成后自动导出文档\n"})
                out.append({"tool": tool_name, "result": True,
                            "ok": True, "deferred": True})
                continue
            result = await self.executor.execute_tool(
                tool_name, params, intent_summary=intent.intent_summary,
                emit=emit)
            # DAG 层面 Replan：核心意图失败且未达上限时补救
            if not result.get("ok") and not result.get("skipped") and self._replan_count < self._replan_max:
                self._replan_count += 1
                # 图形工具降级提示：render_flowchart 校验失败 → 尝试 render_mermaid
                if tool_name == "render_flowchart":
                    await emit("thinking_delta", {
                        "text": f"【工具调用】{tool_name} JSON 校验失败，"
                        f"尝试降级为 render_mermaid 重出…\n"})
                decision = await self.executor.replan(
                    intent.intent_summary, tool_name,
                    result.get("error", ""), lambda *a, **kw: self._replan_fn(*a, sid=sid, **kw))
                act = decision.get("action")
                if act in ("retry_other_tool", "retry_same_tool") and decision.get("tool"):
                    new_tool = decision["tool"]
                    new_params = decision.get(
                        "params") or await self._infer_params(new_tool, message, deps, sid)
                    if self.registry.has(new_tool):
                        await emit("tool_executing",
                                   {"tool_name": new_tool, "status": "replan"})
                        await emit("thinking_delta",
                                   {"text": f"【工具调用】{tool_name} 失败，重新规划改用 {new_tool}…\n"})
                        result = await self.executor.execute_tool(
                            new_tool, new_params,
                            intent_summary=intent.intent_summary, emit=emit)
                        tool_name = new_tool
                elif tool_name == "render_flowchart":
                    # 图形工具降级兜底：Replan 也未给出有效方案 → 明确告知
                    await emit("thinking_delta", {
                        "text": "【工具调用】图形生成失败，已尝试降级但不可用，"
                        "将在回复中以文字说明\n"})
            # 图形工具执行成功 → 发射 tool_visual 事件供前端渲染
            if result.get("ok") and tool_name in ("render_flowchart", "render_mermaid"):
                visual_data = result.get("result")
                if isinstance(visual_data, dict) and visual_data.get("type"):
                    await emit("tool_visual", {
                        "type": visual_data["type"],
                        "data": visual_data,
                    })
            shared.put(intent.id, tool_name, 0, result.get("result"))
            ok = result.get("ok")
            await emit("thinking_delta",
                       {"text": f"【工具调用】{tool_name} 执行{'成功' if ok else '失败：' + str(result.get('error', ''))[:80]}\n"})
            out.append({"tool": tool_name, "result": result.get("result"),
                        "ok": result.get("ok"), "error": result.get("error")})
        return out

    # ---- L1 压缩编排（Head-Middle-Tail + 落盘 + 失败兑底） ----------------

    # ---- 收敛式理解循环（§3.5） --------------------------------------------
    async def _convergence_loop(self, sid: str, message: str,
                                quick_result: QuickIntentResult,
                                history: list[dict],
                                tracer, emit,
                                max_rounds: int = 2) -> tuple[Understanding | None, list[dict]]:
        """收敛式理解循环：广撒网 → 意图收敛 → 缺口检测 → 定向重收集 → 再收敛。

        返回 (Understanding | None, 最后一轮检索结果 list[dict])。"""
        core_query = _strip_attachment_context(message)
        retrieval_context = "\n".join(
            f"{'用户' if m['role'] == 'user' else 'AI'}：{m['content'] or ''}"
            for m in history[-4:])
        recent_history = "\n".join(
            f"{'用户' if m['role'] == 'user' else 'AI'}：{m['content'] or ''}"
            for m in history[-6:])
        memories_text = ""
        emotion = EmotionState(valence="平静", intensity=0.0)
        focus = None
        all_mems: list[dict] = []  # 收敛环内检索结果，函数返回时带出
        prev_gap_types: set[str] = set()  # 上一轮缺口类型，用于检测收敛停滞

        for round_num in range(1, max_rounds + 1):
            round_sp = tracer.span_start(
                f"convergence_round_{round_num}",
                input={"round": round_num, "max_rounds": max_rounds,
                       "hypothesis": quick_result.intent_hypothesis})

            # 1. 上下文收集：检索记忆（复用现有检索器，每轮可能不同策略）
            cg_sp = tracer.span_start(
                "context_gather",
                input={"query": core_query, "round": round_num},
                parent_observation_id=round_sp.id if hasattr(round_sp, 'id') else None)
            try:
                retrieval = await self.retriever.retrieve(
                    core_query,
                    llm_available=True,
                    session_id=sid,
                    context_text=retrieval_context)
                all_mems = retrieval.hits + retrieval.related
                memories_text = "\n".join(
                    f"[{m['id']}] {m['title']}：{m.get('detail', m.get('summary', ''))}"
                    for m in all_mems[:15])
                cg_sp.end(output={"count": len(all_mems),
                                  "titles": [m["title"] for m in all_mems[:10]]})
            except Exception as e:
                cg_sp.end(level="ERROR", status_message=str(e))
                logger.warning("收敛环检索失败：%s", e)
                # 检索失败 → 态一安全跳过（后续收敛缺少记忆，但可继续）

            # 2. 情绪评估：读 MoodManager 当前衰减后的状态（零 LLM 成本）
            ea_sp = tracer.span_start(
                "emotion_assess",
                input={"round": round_num},
                parent_observation_id=round_sp.id if hasattr(round_sp, 'id') else None)
            try:
                if self.mood:
                    row = self.mood.db.query_one(
                        "SELECT * FROM mood_state WHERE id=1")
                    if row:
                        # sqlite3.Row 不支持 .get()，转为 dict 访问
                        rd = dict(row)
                        em = rd.get("user_mood", "neutral") or "neutral"
                        ei = self.mood._decay(
                            rd.get("user_intensity", 0) or 0,
                            rd.get("user_updated_at"))
                        emotion = EmotionState(
                            valence=em, intensity=round(ei, 2))
                ea_sp.end(output={"valence": emotion.valence,
                                  "intensity": emotion.intensity})
            except Exception as e:
                ea_sp.end(level="ERROR", status_message=str(e))

            # 3. 注意力聚焦：LLM 分析诉求点权重
            af_sp = tracer.span_start(
                "attention_focus",
                input={"message": message[:500], "round": round_num},
                parent_observation_id=round_sp.id if hasattr(round_sp, 'id') else None)
            try:
                focus = await self.attention_focuser.focus(
                    message, memories_text[:3000], emotion, session_id=sid,
                    recent_history=recent_history)
                af_sp.end(output={
                    "primary_focus": focus.primary_focus,
                    "is_competitive": focus.is_competitive,
                    "demand_count": len(focus.demand_points),
                })
            except Exception as e:
                af_sp.end(level="ERROR", status_message=str(e))
                focus = FocusResult(
                    demand_points=[{"point": message[:60], "weight": 1.0}],
                    primary_focus=message[:60])

            # 4. 意图收敛：整合三者产出
            ic_sp = tracer.span_start(
                "intent_converge",
                input={"hypothesis": quick_result.intent_hypothesis,
                       "memories_count": len(all_mems) if 'all_mems' in dir() else 0,
                       "emotion": f"{emotion.valence}({emotion.intensity})"},
                parent_observation_id=round_sp.id if hasattr(round_sp, 'id') else None)
            try:
                tool_names = [s.name for s in self.registry.all_specs()]
                intents, correction_note = await self.intent_parser.converge_intent(
                    message, tool_names, quick_result,
                    memories_text=memories_text,
                    emotion_state=emotion,
                    focus_result=focus,
                    session_id=sid,
                    recent_history=recent_history)
                ic_sp.end(output={
                    "intent_count": len(intents),
                    "correction": correction_note,
                })
            except DegradationError as e:
                ic_sp.end(level="ERROR")
                _decision = e.decision
                tracer.record_degradation(_decision)
                if _decision.state == DegradationState.STATE_3:
                    await emit("error", {"code": 503,
                                         "message": _decision.message or "意图收敛失败"})
                    return None, []
                intents = [type("I", (), {
                    "id": "i1", "intent_summary": quick_result.intent_hypothesis,
                    "intent_type": "chat", "tools_needed": [],
                    "depends_on": []})()]

            # 构建当前轮次的理解包
            current_understanding = Understanding(
                rich_intent=intents[0] if intents else type("I", (), {
                    "id": "i1", "intent_summary": quick_result.intent_hypothesis,
                    "intent_type": "chat", "tools_needed": [],
                    "depends_on": []})(),
                emotion_state=emotion,
                focus=focus,
            )

            # 5. 缺口检测
            gd_sp = tracer.span_start(
                "gap_detect",
                input={"round": round_num},
                parent_observation_id=round_sp.id if hasattr(round_sp, 'id') else None)
            try:
                gap_result = await self.gap_detector.detect(
                    current_understanding, message, session_id=sid,
                    recent_history=recent_history)
                gd_sp.end(output={
                    "has_gaps": gap_result.has_gaps,
                    "gap_count": len(gap_result.gaps),
                    "unresolvable": gap_result.unresolvable,
                })
            except Exception as e:
                gd_sp.end(level="ERROR", status_message=str(e))
                gap_result = GapResult(
                    gaps=[], has_gaps=False, retarget_tasks=[])

            round_sp.end(output={
                "intent": current_understanding.rich_intent.intent_summary,
                "has_gaps": gap_result.has_gaps,
            })

            # 无缺口 → 理解完整，退出
            if not gap_result.has_gaps:
                return current_understanding, all_mems

            # 缺口收敛停滞检测：当前缺口类型与上一轮无变化或为其子集 → 早停
            cur_gap_types = {g.get("type", "") for g in gap_result.gaps}
            if prev_gap_types and cur_gap_types and cur_gap_types.issubset(prev_gap_types):
                logger.info(
                    "收敛环第 %d 轮缺口无改善（prev=%s cur=%s），提前退出",
                    round_num, prev_gap_types, cur_gap_types)
                round_sp.end(output={
                    "intent": current_understanding.rich_intent.intent_summary,
                    "has_gaps": gap_result.has_gaps,
                    "early_exit_reason": "gap_stagnation",
                })
                return current_understanding, all_mems
            prev_gap_types = cur_gap_types

            # 纯焦点竞争缺口：多诉求是用户提问结构的固有特征，
            # 重收敛无法"修复"——用户确实需要多方面的回答，不应在此循环
            if cur_gap_types == {"focus_competition_gap"}:
                logger.info(
                    "收敛环第 %d 轮仅剩 focus_competition_gap（多诉求固有特征），提前退出",
                    round_num)
                round_sp.end(output={
                    "intent": current_understanding.rich_intent.intent_summary,
                    "has_gaps": gap_result.has_gaps,
                    "early_exit_reason": "focus_competition_inherent",
                })
                return current_understanding, all_mems

            # 缺口无法消解 → 先尝试追问（elicitation），不可枚举则诚实澄清（态二）
            if gap_result.unresolvable:
                # 尝试将缺口转为结构化追问
                seed = await self.strategy_engine.clarification_router(
                    sid, message,
                    "；".join(g.get("description", "")
                             for g in gap_result.gaps[:2]),
                    {k: self.config.get(k) for k in (
                        "elicitation_max_questions",
                    ) if self.config.get(k) is not None})
                if seed is not None:
                    # 可枚举：通过实例变量传递给外部 _try_elicitation
                    self._elicitation_seed = seed
                    self._elicitation_from_gap = True
                    return None, []
                # 不可枚举：走诚实澄清
                _decision = decide_degradation(
                    failed_step="gap_detect",
                    error="理解缺口无法在系统内消解",
                    skip_causes_misleading=True,
                    failure_type=FailureType.CAPABILITY_BOUNDARY,
                )
                _decision.message = "；".join(
                    g.get("description", "") for g in gap_result.gaps[:2])
                tracer.record_degradation(_decision)
                await self._emit_honest_clarify(
                    emit, gap_result, sid)
                return None, []

            # 有缺口且未达上限 → 准备下一轮定向重收集
            if round_num < max_rounds:
                await emit("thinking_delta", {
                    "text": f"【理解收敛】第 {round_num} 轮发现 {len(gap_result.gaps)} 个缺口，进入定向重收集…\n"})
                # 将缺口翻译为重收集任务，修改下一轮的检索查询
                retarget_descs = "；".join(
                    t.get("description", "") for t in gap_result.retarget_tasks[:3])
                if retarget_descs:
                    core_query = f"{message} {retarget_descs}"
                continue

        # 达到轮次上限：带当前理解退出
        return current_understanding, all_mems

    async def _emit_honest_clarify(self, emit, gap_result: GapResult,
                                   sid: str | None = None) -> None:
        """态二：生成诚实澄清回复（LLM 基于缺口描述生成自然的反问）。"""
        gap_desc = "；".join(
            g.get("description", "") for g in gap_result.gaps[:2])
        snap = self.providers.snapshot_for("chat")
        if snap is None:
            await emit("content_delta",
                       {"text": f"我需要确认一下：{gap_desc}，能再帮我说明一下吗？"})
            return
        try:
            system = PROMPTS.render(
                "agent/prompts/honest_clarify", gap_description=gap_desc)
            resp = await self.llm.chat(
                snap,
                [{"role": "system", "content": system},
                 {"role": "user", "content": gap_desc}],
                source="honest_clarify",
                session_id=sid)
            await emit("content_delta", {"text": resp["content"]})
        except Exception:
            await emit("content_delta",
                       {"text": f"关于这个问题，我需要确认一下：{gap_desc}"})

    async def _compress_history(self, sid: str,
                                history: list[dict]) -> tuple[list[dict], bool]:
        """按配置切 head/middle/tail，只压 middle；成功则摘要落盘+推进水位，
        失败则退化为 Head+Tail（丢 middle）并标记 compression_failed，
        连续 3 次失败推系统通知建议新建会话。"""
        # 已有摘要单独抽出（二次压缩合并旧摘要，不嵌套）
        prev_text = None
        body: list[dict] = []
        for m in history:
            if m["role"] == "system" and "[CONTEXT COMPACTION]" in m.get("content", ""):
                # 前缀固定占首行（compact_prefix.md 及旧版硬编码均如此），
                # 按首行切分取摘要正文，不再依赖具体文案
                prev_text = m["content"].split("\n", 1)[-1]
            else:
                body.append(m)
        head_n = self.config.get("head_protected_rounds", 2) * 2   # 轮 → 条
        tail_n = self.config.get("tail_protected_rounds", 3) * 2   # 轮 → 条
        if len(body) <= head_n + tail_n:
            return history, False  # 无可压缩的 middle 段
        head, middle, tail = body[:head_n], body[head_n:-
                                                 tail_n], body[-tail_n:]
        window_check = self.config.get("compression_model_window", 80000)
        summary, ok = await self.compressor.compress(
            middle, prev_summary_text=prev_text, threshold_tokens=window_check,
            session_id=sid)
        if ok:
            self._compress_fails.pop(sid, None)

            # 把 pinned 区约束并入摘要 S0（去重），保证压缩后约束不丢
            pinned_raw = self.ctx_entry.read_consciousness_hint()
            if pinned_raw:
                pinned_list = [c.strip()
                               for c in pinned_raw.split("；") if c.strip()]
                s0 = summary.setdefault("S0_constraints", [])
                for pc in pinned_list:
                    if pc not in s0:
                        s0.append(pc)

            # 水位 = middle 最后一条原文消息 id；摘要 md 落盘 + sessions 水位推进
            watermark = next(
                (m["id"] for m in reversed(middle) if m.get("id")), None)
            if watermark:
                try:
                    self.sessions.save_summary(
                        sid, render_summary_body(summary), watermark)
                except Exception:  # noqa: BLE001
                    logger.warning("压缩摘要落盘失败", exc_info=True)
            return assemble_context(head, summary, tail), True
        # 失败兑底：只保留 Head +（旧摘要）+ Tail，middle 原文丢弃（已装不下）
        fails = self._compress_fails.get(sid, 0) + 1
        self._compress_fails[sid] = fails
        try:
            self.sessions.mark_compression_failed(sid)
        except Exception:  # noqa: BLE001
            logger.warning("compression_failed 标记写入失败", exc_info=True)
        if fails >= 3:
            # 每次失败都提醒（>= 而非 ==）：持续失败说明问题未解决，
            # 必须让用户持续感知（60s 推送去重只防同一事件的连发）
            self.notify("compression_failed",
                        "上下文压缩连续 3 次失败，建议新建会话继续对话")
        degraded = list(head)
        if prev_text:
            # 前缀与 assemble_context 统一走 compact_prefix.md，避免文案漂移
            degraded.append({"role": "system",
                             "content": PROMPTS.load_raw("agent/prompts/compact_prefix")
                             + "\n" + prev_text})
        degraded.extend(tail)
        return degraded, False

    async def _preload_urls(self, message: str, emit) -> str:
        """第 1 步：检测消息中的 URL 并用 web_fetch 预加载；失败不中断，失败信息作为上下文。"""
        import re
        from tools import hooks as tool_hooks
        from langfuse.integration import get_tracer, mark_preview
        urls = re.findall(r"https?://[^\s]+", message)
        if not urls:
            return ""
        tool = self.registry.get("web_fetch")
        if not tool:
            return ""
        tracer = get_tracer()
        _sp = tracer.span_start("url_preload", input={
            "urls": urls[:2], "target_count": len(urls)})
        blocks = []
        failed = 0
        injection_flagged = 0
        for url in urls[:2]:
            try:
                text = await tool.run(url=url)
                # 直调 tool.run 绕过了 post_tool 漏斗，补一次注入防护（先截断再包裹，保标注完整）；
                # 上限 30000 对齐 _trim 兜底：网页正文基本完整注入，Langfuse 记录与业务输入一致
                guarded, inj = tool_hooks.guard_external(str(text)[:30000])
                if inj:
                    injection_flagged += 1
                    logger.warning("URL 预加载内容疑似含注入指令，已隔离标注：%s", url)
                    self.notify("injection_guard",
                                f"预加载网页 {url} 疑似包含注入指令，已隔离标注")
                blocks.append(f"【预加载 {url}】\n{guarded}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                blocks.append(f"（注：URL {url} 抓取失败，原因 {e}）")
        joined = "\n\n".join(blocks)
        _sp.end(output={
            "fetched": len(blocks) - failed, "failed": failed,
            "injection_flagged": injection_flagged,
            "content": mark_preview(
                joined, content_type="url_preload_content")})
        return joined

    def _backfill_prev_signal(self, sid: str, new_message: str) -> None:
        """信号采集阶段二：回填上一条 assistant 回复的隐式反应与隐式关键词。"""
        try:
            row = self.db.query_one(
                "SELECT id FROM conversations WHERE session_id=? AND role='assistant' "
                "AND message_type='normal' ORDER BY id DESC LIMIT 1", (sid,))
            if not row:
                return
            keywords = rs.detect_implicit_keywords(new_message)
            followup = keywords or any(
                k in new_message for k in ("为什么", "怎么", "是不是", "吗？", "？", "?",
                                           "展开", "继续", "详细", "再说", "什么意思"))
            reaction = "追问澄清" if followup else "继续新话题"
            self.signals.backfill_reaction(row["id"], reaction, keywords)
        except Exception:  # noqa: BLE001
            logger.warning("信号阶段二回填失败", exc_info=True)

    async def _replan_fn(self, intent_summary: str, tool_name: str, error: str, *, sid: str | None = None) -> dict:
        """Replan 判定用 chat_model（§6.4）。"""
        snap = self.providers.snapshot_for("chat")
        if snap is None:
            return {"action": "skip", "reason": "无可用模型"}
        from infrastructure.json_repair import repair_json
        prompt = [{"role": "system", "content":
                   PROMPTS.load_raw("agent/prompts/replan")},
                  {"role": "user", "content":
                   f"意图：{intent_summary}\n失败工具：{tool_name}\n错误：{error}"}]
        resp = await self.llm.chat(snap, prompt, source="replan", session_id=sid)
        return repair_json(resp["content"])

    async def _memory_save_params(self, message: str, sid: str | None = None) -> dict:
        """主动/半主动记忆入库参数：标题与摘要由 LLM 提炼，详情保留用户原始描述。"""
        snap = self.providers.snapshot_for("chat")
        if snap is None:
            # 兼容降级：惰性调用（_infer_params 是 async，必须 await）
            return await self._infer_params("memory_save", message, sid=sid)
        try:
            from infrastructure.json_repair import repair_json
            prompt = [{"role": "system",
                       "content": PROMPTS.load_raw("agent/prompts/memory_card")},
                      {"role": "user", "content": message}]
            resp = await self.llm.chat(snap, prompt, source="system_agent", session_id=sid)
            data = repair_json(resp["content"]) or {}
            title = str(data.get("title") or "").strip()
            summary = str(data.get("summary") or "").strip()
            if title and summary:
                return {"title": title[:30], "summary": summary[:30],
                        "detail": message, "domain": "general"}
        except Exception:  # noqa: BLE001
            logger.warning("主动记忆标题/摘要提炼失败，回退截断策略", exc_info=True)
        return await self._infer_params("memory_save", message, sid=sid)

    async def _format_template_save_params(self, message: str,
                                           sid: str | None = None,
                                           intent_summary: str = "") -> dict:
        """格式绑定工具参数：附件正文从当轮消息解析（无则回溯近 3 轮），
        适用场景由轻量 LLM 调用从用户真实提问中提取（失败回退意图摘要）。"""
        atts = _extract_attachment_blocks(message)
        if not atts and sid:
            # 回溯：用户可能先上传附件、隔一轮才说"按这个格式"；
            # 当轮消息已提前落库，LIMIT 4 跳过自身取更早三轮
            rows = self.db.query_all(
                "SELECT content FROM conversations "
                "WHERE session_id=? AND role='user' "
                "ORDER BY id DESC LIMIT 4", (sid,))
            for r in rows[1:]:
                atts = _extract_attachment_blocks(r["content"] or "")
                if atts:
                    break
        attachment_text = "\n\n".join(body for _n, body in atts)
        # 适用场景提取：intent 槽位（轻量分析），失败/不可用时回退意图摘要
        scenario = ""
        question = _strip_attachment_context(message)
        snap = self.providers.snapshot_for("intent")
        if snap is not None and attachment_text:
            try:
                resp = await self.llm.chat(
                    snap, [{"role": "system", "content":
                            PROMPTS.load_raw("agent/prompts/format_scenario")},
                           {"role": "user", "content": question}],
                    source="intent", session_id=sid)
                scenario = (resp.get("content")
                            or "").strip().strip('"\'`')[:12]
            except Exception:  # noqa: BLE001
                logger.warning("格式绑定场景提取失败，回退意图摘要", exc_info=True)
        if not scenario:
            scenario = (intent_summary or "").strip()[:12]
        return {"scenario": scenario, "attachment_text": attachment_text}

    async def _infer_params(self, tool_name: str, message: str,
                            deps: dict | None = None, sid: str | None = None) -> dict:
        """LLM 驱动工具参数推断（ReAct 模式）。利用 function_call + 依赖结果 feed。
        LLM 不可用时退化为启发式正则匹配。
        file_write 使用 relaxed schema：content 非必填，推迟到回复生成后写入。
        参数推断走 agent 槽位（快速模型），避免 chat 槽位推理模型的长 CoT 拖慢工具链。"""
        snap = self.providers.snapshot_for("agent")
        if snap is not None:
            try:
                if tool_name == "file_write":
                    return await self._infer_file_write_params(message, snap, deps, sid=sid)
                if tool_name == "generate_document":
                    return await self._infer_generate_document_params(message, snap, deps, sid=sid)
                return await self._infer_params_llm(tool_name, message, snap, deps, sid=sid)
            except Exception:  # noqa: BLE001
                logger.warning("LLM 参数推断失败，退化：%s", tool_name, exc_info=True)
        return self._infer_params_heuristic(tool_name, message)

    async def _infer_file_write_params(self, message: str, snap,
                                       deps: dict | None = None,
                                       *, sid: str | None = None) -> dict:
        """file_write 参数推断：只让 LLM 推断 path，content 推迟到回复生成后。
        短内容场景 LLM 仍可直接返回 content（向后兼容）。
        推断失败/无 path 时兜底生成默认文件名，保证该意图永不因参数缺失卡死。"""
        # content 非必填，LLM 不会因尝试生成全文而超时
        relaxed_schema = {
            "type": "function",
            "function": {
                "name": "file_write",
                "description": "把内容保存为工作区文件。用户说\"写入/更新/保存到文档\""
                               "通常指把 AI 整理好的回复内容存成文件，此时只需给出 path；"
                               "content 留空即可，回复正文会在生成后自动写入。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string",
                                 "description": "文件路径：从用户消息提取文件名；"
                                                "用户未指明时按主题起简短文件名，"
                                                "默认 .md 扩展名"},
                        "content": {"type": "string",
                                    "description": "文件内容（可选，通常留空）"},
                        "mode": {"type": "string", "enum": ["w", "a"]},
                    },
                    "required": ["path"],
                },
            },
        }
        try:
            params = await self._infer_params_llm(
                "file_write", message, snap, deps, sid=sid,
                tool_schema_override=relaxed_schema)
        except Exception:  # noqa: BLE001
            logger.warning("file_write 参数推断异常，走默认文件名兜底", exc_info=True)
            params = {}
        if not isinstance(params, dict):
            params = {}
        # 兜底：LLM 未给出 path（弱模型/空返回）→ 默认文件名，绝不让流程卡死
        if not params.get("path"):
            params["path"] = f"回复整理_{now_cst():%Y%m%d_%H%M%S}.md"
        # LLM 未返回 content → 标记延迟写入，等主回复生成后填充
        if not params.get("content"):
            params["content"] = "__FROM_RESPONSE__"
        return params

    async def _infer_generate_document_params(self, message: str, snap,
                                              deps: dict | None = None,
                                              *, sid: str | None = None) -> dict:
        """generate_document 参数推断：只让 LLM 推断 title/format，content 推迟到回复生成后。
        与 file_write 同源——避免 tool_infer 因填不出长正文而参数校验失败、进而无谓 replan。"""
        relaxed_schema = {
            "type": "function",
            "function": {
                "name": "generate_document",
                "description": "生成文档文件供用户下载。只需提供 title（文档标题/文件名）"
                               "与 format（docx 或 md，默认 docx）；content 留空即可，"
                               "将由本轮回复正文自动填充。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string",
                                  "description": "文档标题，用作文件名；用户未指明时按主题起简短标题"},
                        "format": {"type": "string", "enum": ["docx", "md"],
                                   "description": "文件格式，默认 docx"},
                        "content": {"type": "string",
                                    "description": "文档正文（可选，通常留空）"},
                    },
                    "required": ["title"],
                },
            },
        }
        try:
            params = await self._infer_params_llm(
                "generate_document", message, snap, deps, sid=sid,
                tool_schema_override=relaxed_schema)
        except Exception:  # noqa: BLE001
            logger.warning("generate_document 参数推断异常，走默认标题兜底", exc_info=True)
            params = {}
        if not isinstance(params, dict):
            params = {}
        # 兜底：无 title → 默认标题，绝不让流程因参数缺失卡死
        if not params.get("title"):
            params["title"] = "文档"
        if (params.get("format") or "docx") not in ("docx", "md"):
            params["format"] = "docx"
        # 正文始终由主回复填充（强制延迟导出）：参数推断的快速模型偶尔会自行
        # 编一段短 content，导致路径不确定（时而直接导出劣质正文、时而延迟导出），
        # 且纯导出模式（doc_only）依赖延迟路径才能抑制正文重复展示
        params["content"] = "__FROM_RESPONSE__"
        return params

    async def _infer_params_llm(self, tool_name: str, message: str, snap,
                                deps: dict | None = None, *, sid: str | None = None,
                                tool_schema_override: dict | None = None) -> dict:
        """用 function_call 让 LLM 输出工具参数。
        tool_schema_override：可选，用于 file_write 等需放松参数约束的场景。"""
        spec = self.registry.get(tool_name)
        if not spec:
            return {}
        tool_schema = tool_schema_override or {
            "type": "function",
            "function": {"name": tool_name, "description": spec.spec.description,
                         "parameters": spec.spec.parameters}
        }
        ctx = f"用户消息：{message}"
        if deps:
            # 前序任务结果完整注入（SharedState 已有 1MB 兜底，不在此截断）：
            # Langfuse 记录与业务输入一致
            ctx += "\n\n前序任务结果：\n" + "\n".join(
                f"  {k}: {v}" for k, v in deps.items())
        ctx += f"\n\n请为工具 {tool_name} 生成合适的参数。"
        if tool_name == "file_write":
            ctx += ("\n注意：用户要求把 AI 输出/整理的内容保存到文件时，"
                    "只需给出 path（简短文件名，默认 .md），不要生成 content。")
        resp = await self.llm.function_call(
            snap, [{"role": "user", "content": ctx}],
            tools=[tool_schema], source="tool_infer", session_id=sid)
        tc = resp.get("tool_calls") or []
        if tc:
            import json as _json
            args = tc[0].get("function", {}).get("arguments", "{}")
            if isinstance(args, str):
                args = _json.loads(args)
            return args if isinstance(args, dict) else {}
        return {}

    @staticmethod
    def _infer_params_heuristic(tool_name: str, message: str) -> dict:
        """LLM 不可用时的启发式正则匹配（退化兜底）。"""
        if tool_name == "memory_search":
            return {"query": message}
        if tool_name == "memory_save":
            # 兜底截断策略（正常路径由 _memory_save_params LLM 提炼标题/摘要）；
            # 标题/摘要剔除指令前缀后截断，详情始终保留用户原始描述
            import re
            text = re.sub(r"^(请|麻烦)?(帮我?)?(记住|记录一下|remember|/remember)[：:，,\s]*",
                          "", message).strip() or message
            return {"title": text[:30], "summary": text[:30],
                    "detail": message, "domain": "general"}
        if tool_name == "file_write":
            return AgentCore._heuristic_file_write(message)
        if tool_name == "web_search":
            return {"query": message}
        if tool_name == "web_fetch":
            import re
            m = re.search(r"https?://\S+", message)
            return {"url": m.group(0)} if m else {"url": ""}
        if tool_name == "datetime_now":
            return {}
        if tool_name == "calculator":
            import re
            m = re.search(r"[\d\.\+\-\*/\(\)\s]+", message)
            return {"expression": m.group(0).strip()} if m else {"expression": "0"}
        return {}

    @staticmethod
    def _heuristic_file_write(message: str) -> dict:
        """file_write 参数启发式推断：从消息中提取文件名，消息体作为内容。
        LLM 推断失败或超时时兜底，避免因缺少必填参数导致工具链中断。"""
        import re
        # 尝试从消息中提取文件名（支持中英文、常见扩展名）
        path_match = re.search(
            r'(?:文件|输出|生成|保存|写入|命名|叫|为)\s*[：:，,]*\s*["\']?([^\s"\',，。：:]+(?:\.\w{1,10}))',
            message)
        if path_match:
            path = path_match.group(1).strip()
        else:
            # 尝试匹配消息中任何带扩展名的词（HTML/代码文件）
            ext_match = re.search(r'["\']?(\w[\w-]*\.(?:html|md|txt|py|js|css|json|xml|yaml|yml))["\']?',
                                  message)
            path = ext_match.group(1).strip() if ext_match else "output.html"
        return {"path": path, "content": message}

    # ---- 约束钉住与滚动更新 ------------------------------------------------
    def _roll_pinned_constraints(self, new_constraints: list[str]) -> None:
        """滚动更新会话 pinned 约束区。

        规则（零 LLM，字符级启发式）：
        - 完全相同 → 跳过
        - 同一约束动词但内容不同 → 视为修正，替换旧条
        - 全新 → 追加
        上限保留最近 10 条，超出淘汰最旧。
        """
        existing_raw = self.ctx_entry.read_consciousness_hint()
        existing = [c.strip() for c in existing_raw.split("；") if c.strip()]

        for nc in new_constraints:
            replaced = False
            for i, ec in enumerate(existing):
                if nc == ec:
                    replaced = True  # 完全重复
                    break
                if self._is_constraint_revision(ec, nc):
                    existing[i] = nc  # 修正 → 替换
                    replaced = True
                    break
            if not replaced:
                existing.append(nc)

        merged = existing[-10:]  # 保留最近 10 条
        self.ctx_entry.set_consciousness_hint_raw("；".join(merged))

    @staticmethod
    def _is_constraint_revision(old: str, new: str) -> bool:
        """判断 new 是否是对 old 约束的修正（同主题、不同内容）。"""
        verbs = ["看", "考虑", "用", "关注", "要"]
        for v in verbs:
            if v in old and v in new:
                old_obj = old.split(v, 1)[-1][:10]
                new_obj = new.split(v, 1)[-1][:10]
                if old_obj != new_obj:
                    return True
        return False

    # ---- 情绪触发检测 v2（规则通道，零 LLM） -------------------------------
    def _detect_emotion_triggers(self, sid: str, message: str,
                                 message_id: int | None) -> None:
        """每轮消息接收后统一入口：调用全部规则触发检测器。"""
        if not self.mood_trigger:
            return
        self._detect_task_repeat(sid, message, message_id)
        self._detect_temporal_triggers(sid, message_id)

    def _detect_task_repeat(self, sid: str, message: str,
                            message_id: int | None) -> None:
        """检测任务重复失败：用户连续否定表达 + 连续被踩。
        阈值和窗口均从 config 读取，不再硬编码。"""
        keywords = ["不对", "不行", "还是不", "重新", "再改", "不是这样",
                    "错了", "不是我要的", "还不行"]
        hit = any(k in message for k in keywords)
        if not hit:
            return

        window = self.config.get("mood_task_repeat_window", 20)
        threshold = self.config.get("mood_task_repeat_threshold", 3)

        recent_downvote = self.db.query_one(
            "SELECT count(*) c FROM conversations "
            "WHERE session_id=? AND role='assistant' AND feedback=2 AND id > "
            "(SELECT COALESCE(MAX(id)-?, 0) FROM conversations WHERE session_id=?)",
            (sid, window, sid))["c"]

        recent_negative = self.db.query_one(
            "SELECT count(*) c FROM conversations "
            "WHERE session_id=? AND role='user' AND id > "
            "(SELECT COALESCE(MAX(id)-?, 0) FROM conversations WHERE session_id=?) "
            "AND (content LIKE '%不对%' OR content LIKE '%不行%' "
            "     OR content LIKE '%还是不%' OR content LIKE '%重新%')",
            (sid, window // 2, sid))["c"]

        if recent_negative >= threshold or recent_downvote >= max(2, threshold - 1):
            self.mood_trigger.record(
                session_id=sid, message_id=message_id,
                scope="user", source_type="task", event_key="task_repeat_fail",
                mood_hint="frustrated", intensity_hint=0.5,
                note=f"近期否定表达 {recent_negative} 次，被踩 {recent_downvote} 次")
            self.mood_trigger.record(
                session_id=sid, message_id=message_id,
                scope="ai", source_type="task", event_key="task_repeat_fail",
                mood_hint="anxious", intensity_hint=0.4,
                note="连续任务未达用户期望")

    def _detect_temporal_triggers(self, sid: str, message_id: int) -> None:
        """检测时间/节奏型触发：长时间未对话、连续轮数过多、深夜时段。"""
        from datetime import timedelta

        # 长时间未对话（>24h）→ AI curious
        last_row = self.db.query_one(
            "SELECT create_time FROM conversations WHERE session_id=? "
            "AND id < ? ORDER BY id DESC LIMIT 1", (sid, message_id))
        if last_row:
            try:
                last_dt = datetime.fromisoformat(last_row["create_time"])
                elapsed = now_cst() - last_dt
                if elapsed > timedelta(hours=24):
                    self.mood_trigger.record(
                        sid, message_id, "ai", "temporal",
                        "long_absence_return", "none", "curious", 0.3,
                        f"上次对话 {int(elapsed.total_seconds() // 3600)} 小时前")
            except (ValueError, TypeError):
                pass

        # 连续轮数过多（≥15）→ AI tired
        consecutive = self.db.query_one(
            "SELECT count(*) c FROM conversations WHERE session_id=? AND role='user'",
            (sid,))["c"]
        threshold_tired = self.config.get("mood_consecutive_turns_tired", 15)
        if consecutive >= threshold_tired:
            self.mood_trigger.record(
                sid, message_id, "ai", "temporal",
                "consecutive_turns_tired", "shared", "tired", 0.3,
                f"本 session 连续 {consecutive} 轮")

        # 深夜时段（22:00-02:00）→ AI warm
        hour = now_cst().hour
        if 22 <= hour or hour < 2:
            self.mood_trigger.record(
                sid, message_id, "ai", "temporal",
                "late_night_conversation", "none", "warm", 0.2,
                f"深夜时段（{hour}:00）")

    # ---- prompt 构建 ------------------------------------------------------
    def _build_system_prompt(self, onboarding: bool, location: str | None = None,
                             sid: str = "") -> str:
        # 当前时间（北京时间），让模型始终知道"现在"
        try:
            now = now_cst()
            wd = "一二三四五六日"[now.weekday()]
            time_hint = f"当前时间（北京时间 UTC+8）：{now:%Y-%m-%d %H:%M} 星期{wd}"
        except Exception:  # noqa: BLE001
            time_hint = ""
        if onboarding:
            return (ONBOARDING_PERSONA + "\n\n" + time_hint) if time_hint else ONBOARDING_PERSONA
        parts = [self.soul.read_core(), self.soul.full_style_text()]
        # 语言约束（强，前置）：模型原生推理（reasoning_content）默认偏英文，
        # 必须明确约束思考过程与回复全文使用中文，仅代码/专有名词等例外
        parts.append(
            "## 语言要求（必须严格遵守）\n"
            "全程使用中文：包括回复正文、思考过程、推理、内心独白、任务拆解、"
            "总结与记忆描述，一律使用中文。\n"
            "允许保留原文的例外仅限：代码、变量名、API 名称、专有名词、"
            "国际通用术语（如 AI、OK、GitHub）。\n"
            "禁止出现英文长句或英文段落式的思考内容。")
        if time_hint:
            parts.append(time_hint)
        # 用户位置（浏览器定位，随请求携带）：天气/附近/本地类查询直接可用
        if location:
            parts.append(f"用户当前位置：{location}（浏览器定位）。"
                         f"涉及天气、附近、本地信息的查询时直接使用该位置，无需再询问用户在哪。")
        hint = self.ctx_entry.read_consciousness_hint()
        if hint:
            parts.append(
                f"## 本会话用户约束（高优先级，回答时必须遵守）\n{hint}")
        ident = self.profile.identity_snippet()
        if ident:
            parts.append(ident)
        # 技能预加载（Level 0 目录，完整注入：Langfuse 记录与业务输入一致）
        try:
            skill_index = self.skills.load_index()
            if skill_index.strip():
                parts.append(f"可用技能目录（需要时可展开）：\n{skill_index}")
        except Exception:  # noqa: BLE001
            pass
        # 画像调整已全部迁移到后台审核队列（profile_review_queue），
        # AI 不再在对话中主动询问画像变更。
        # low 待确认记忆：每轮最多追问一条（独立于画像审核机制）
        cand = self.lifecycle.next_low_confirm_candidate()
        if cand:
            self.lifecycle.mark_low_confirm_asked(cand["id"])
            parts.append(
                f"（本轮回复末尾请自然地向用户确认一条早前的推断是否属实："
                f"「{cand['title']}——{cand.get('summary') or ''}」。"
                f"若用户在本轮消息中已明确表态，则在回复最末尾另起一行输出 "
                f'{{"memory_confirm":{{"id":"{cand["id"]}","confirmed":true或false}}}} '
                f"声明；用户未表态则不输出该声明。）")
        # draft 技能待确认：AI 主动向用户提议启用
        try:
            drafts = self.skills.list_drafts()
            if drafts:
                names = "、".join(d.get("skill_name", "") for d in drafts[:2])
                parts.append(
                    f"（系统从最近工作模式中提炼出 {len(drafts)} 个技能模板：{names}。"
                    f"本轮请在合适时机询问用户是否启用，告知用户可在记忆中心·健康度管理。）")
        except Exception:  # noqa: BLE001
            pass
        # 情绪注入（收敛式优化：永远在场，强度决定浓淡）
        if self.mood and self.config.get("mood_enabled", True):
            try:
                mood_hint = self.mood.build_hint()
                if mood_hint:
                    parts.append(mood_hint)

                # v2 主动行为评估：根据情绪状态 + 对话上下文决定是否注入行为指令
                if self.mood_action_dispatcher:
                    state_row = self.db.query_one(
                        "SELECT * FROM mood_state WHERE id=1")
                    if state_row:
                        state = {
                            "user_mood": state_row["user_mood"],
                            "user_intensity": self.mood._decay(
                                state_row["user_intensity"],
                                state_row["user_updated_at"]),
                            "user_attribution": state_row["user_attribution"] or "",
                            "ai_mood": state_row["ai_mood"],
                            "ai_intensity": self.mood._decay(
                                state_row["ai_intensity"],
                                state_row["ai_updated_at"]),
                            "ai_attribution": state_row["ai_attribution"] or "",
                        }
                        action_ctx = self._build_action_ctx(sid)
                        action_key, action_prompt = \
                            self.mood_action_dispatcher.evaluate(
                                state, action_ctx)
                        if action_prompt:
                            parts.append(action_prompt)
                            self.db.execute(
                                "UPDATE mood_state SET active_action=? WHERE id=1",
                                (action_key,))
            except Exception:  # noqa: BLE001
                logger.warning("情绪注入失败（静默跳过）", exc_info=True)
        # 思考过程中文化：模型原生推理默认偏英文，末尾再次强调（首尾呼应，双保险）
        parts.append("再次强调：思考过程（包括模型原生推理）与回复正文必须使用中文，"
                     "仅代码、变量名、API 名称、专有名词与通用术语可保留英文原文。")
        return "\n\n".join(parts)

    def _build_action_ctx(self, sid: str) -> dict:
        """构建主动行为评估所需的对话上下文指标。"""
        window = self.config.get("mood_task_repeat_window", 20)
        task_repeat = self.db.query_one(
            "SELECT count(*) c FROM conversations "
            "WHERE session_id=? AND role='user' AND id > "
            "(SELECT COALESCE(MAX(id)-?, 0) FROM conversations WHERE session_id=?) "
            "AND (content LIKE '%不对%' OR content LIKE '%不行%' "
            "     OR content LIKE '%还是不%' OR content LIKE '%重新%')",
            (sid, window, sid))["c"]

        consecutive = self.db.query_one(
            "SELECT count(*) c FROM conversations WHERE session_id=? AND role='user'",
            (sid,))["c"]

        last_up = self.db.query_one(
            "SELECT feedback FROM conversations "
            "WHERE session_id=? AND role='assistant' "
            "ORDER BY id DESC LIMIT 1", (sid,))
        just_completed = last_up and last_up["feedback"] == 1

        return {
            "task_repeat_count": task_repeat,
            "consecutive_turns": consecutive,
            "just_completed_task": 1 if just_completed else 0,
        }

    def _build_final_prompt(self, system_prompt, history, message, tool_results,
                            memories, onboarding, skill_text="", preload_text="",
                            depth_level: str = "normal",
                            strategy: ResponseStrategy | None = None,
                            skeleton: CognitiveSkeleton | None = None,
                            next_step_seeds: list | None = None):
        if onboarding:
            return [{"role": "system", "content": system_prompt}] + history + \
                   [{"role": "user", "content": message}]
        # 结构化落盘场景（generate_document/file_write 延迟写入）：工程级剥离情绪
        # 注入段——情绪表达只发生在对话层，不进入文档/文件正文
        # （避免模型受情绪注入影响写出对话式正文/思考过程）
        if any(r.get("deferred") for r in (tool_results or [])):
            system_prompt = _strip_mood_section(system_prompt)
        synth = rs.build_response_prompt(
            message, tool_results, memories, depth_level=depth_level,
            next_step_seeds=next_step_seeds)
        # synth[0] 是含上下文的 system；合并 SOUL system_prompt + 按需技能
        merged_system = system_prompt + "\n\n" + synth[0]["content"]
        # 响应策略硬约束注入（v3 §六）：策略是逐轮决策，优先于长期风格基线
        if strategy is not None:
            hooks = "、".join(
                strategy.insight_hooks) if strategy.insight_hooks else "无"
            merged_system += (
                "\n\n## 响应策略（本轮硬约束）\n"
                f"- 回答角度：{strategy.angle}\n"
                f"- 思考深度：{strategy.depth}（0 最浅，3 最深）\n"
                f"- 表达形态：{strategy.form}\n"
                f"- 语气：{strategy.tone}\n"
                f"- 洞察触发点：{hooks}\n"
                "优先级：人格底线（诚实、不伪装身份）高于一切；本轮策略优先于"
                "输出风格基线；两者冲突时以本轮策略为准，但不得突破人格底线。")
        # 思考骨架注入（v3 §六）：有则遵循，无则跳过
        if skeleton is not None:
            merged_system += (
                "\n\n## 思考骨架（有则遵循）\n" + skeleton.to_prompt_text()
                + "\n遵循规则：opening_move 与 closing_move 必须遵循；关键洞察"
                "必须在回答中以某种形式呈现，不可稀释。")
        if skill_text:
            merged_system += "\n\n可参考的技能内容：" + skill_text
        if preload_text:
            merged_system += "\n\n预加载的外部内容（URL/附件）：\n" + preload_text
        # 近期对话锚定：长会话时将最近6轮注入 system prompt，防止模型在长上下文中丢失近邻关联
        if len(history) > 6:
            recent = history[-6:]
            older = history[:-6]
            recent_text = "\n".join(
                f"{'用户' if m['role'] == 'user' else 'AI'}：{m['content'] or ''}"
                for m in recent)
            merged_system += (
                "\n\n## 近期对话（请务必基于此理解用户当前消息的指代和上下文）\n\n"
                + recent_text)
            return [{"role": "system", "content": merged_system}] + older + \
                   [{"role": "user", "content": message}]
        else:
            # 会话短（≤6 轮），全部进 messages，不做锚定段
            return [{"role": "system", "content": merged_system}] + history + \
                   [{"role": "user", "content": message}]

    async def _post_process(self, sid, msg_id, content, loaded_ids, cited_ids,
                            message, onboarding, user_msg_id=None, intents=None):
        if onboarding:
            return None  # 引导期只写 conversations（已在 append 完成）
        # 信号采集阶段一（context_label 复用第 4 步意图类型，零额外 LLM 成本）
        shape = rs.collect_signal_shape(content)
        self.signals.record_shape(
            msg_id, shape, context_label=self._context_label(intents, message))
        # 主动记忆检测：未识别 remember_intent 但含明确新事实 → 被动回顾候选
        try:
            self._mark_review_candidate(
                sid, user_msg_id or msg_id, message, intents)
        except Exception:  # noqa: BLE001
            logger.warning("回顾候选标记失败", exc_info=True)

        # 会话内约束提取 + 钉住（新增）
        try:
            new_constraints = _extract_constraints(message)
            if new_constraints:
                self._roll_pinned_constraints(new_constraints)
        except Exception:  # noqa: BLE001
            logger.warning("约束钉住失败", exc_info=True)
        # 频次三类更新
        self.lifecycle.update_access_stats(loaded_ids, cited_ids)
        # 引用明细落表：记忆/知识库统一溯源（被哪条消息、何时引用）
        self.lifecycle.record_citations(cited_ids, msg_id, sid)
        # stale 命中恢复（强化门：仅真正被引用才恢复——候选池冒泡不算真实使用，
        # 避免噪声检索污染生命周期与意识提示通路）
        for mid in cited_ids:
            await self.lifecycle.recover_on_hit(mid)
        # lifecycle 流转检查（stable 升级）
        for mid in cited_ids:
            await self.lifecycle.check_stable_upgrade(mid)
        # index 刷新
        await self.fw.flush_pending_index()
        # 成本控制：预算告警（over_budget_strategy 决定处置，当前 remind_only 仅提醒不阻断）
        try:
            self._check_budget_alert()
        except Exception:  # noqa: BLE001
            logger.warning("预算告警检查失败", exc_info=True)
        # 情绪更新：turn 后异步判定（双源：用户情绪 + AI 自身情绪），
        # 零阻塞——回复已发出，判定失败静默降级，绝不影响主流程
        if self.mood and self.config.get("mood_enabled", True) \
                and self.config.get("mood_influence_strength", 0.5) > 0:
            try:
                self._mood_task = asyncio.create_task(
                    self._update_mood(sid, message, content, message_id=user_msg_id))
            except Exception:  # noqa: BLE001
                logger.warning("情绪更新任务创建失败（静默跳过）", exc_info=True)
            # v2 自然回落：fire-and-forget 异步调度，静默降级
            if self.mood:
                try:
                    asyncio.create_task(self._natural_decline_task(sid))
                except Exception:  # noqa: BLE001
                    pass
        if self.bus:
            from infrastructure.event_bus import EVT_TURN_COMPLETED
            await self.bus.publish(EVT_TURN_COMPLETED, {"session_id": sid})
        return shape

    async def _update_mood(self, sid: str, user_msg: str, ai_reply: str,
                           message_id: int | None = None) -> None:
        """turn 后异步情绪判定 v2：规则触发摘要 + 近轮历史 →
        mood_judge_v2 LLM 判定（走 DeepSeek-V4-Flash mood 槽位）→
        apply_v2 融合/传染/平复落库 → 发事件。
        全异常捕获静默降级（模型不可用/解析失败仅跳过本轮，不阻断任何流程）。"""
        if not self._should_judge_mood(user_msg):
            return
        try:
            snap = self.providers.snapshot_for("mood")
            if snap is None:
                return
            from infrastructure.json_repair import repair_json

            rule_summary = ""
            if self.mood_trigger and message_id:
                rule_summary = self.mood_trigger.summarize_for_turn(
                    sid, message_id)

            recent_history = self._format_recent_history(sid, limit=5)

            state = self.db.query_one(
                "SELECT * FROM mood_state WHERE id=1")
            # sqlite3.Row 不支持 .get()，转 dict 后保留默认值访问
            state = dict(state) if state else {}
            prev_user = state.get("user_mood", "neutral")
            prev_user_i = self.mood._decay(
                state.get("user_intensity", 0), state.get("user_updated_at"))
            prev_ai = state.get("ai_mood", "neutral")
            prev_ai_i = self.mood._decay(
                state.get("ai_intensity", 0), state.get("ai_updated_at"))

            prompt = [{"role": "system", "content": PROMPTS.render(
                "agent/prompts/mood_judge_v2",
                rule_triggers_summary=rule_summary,
                prev_user_mood=prev_user, prev_user_intensity=round(
                    prev_user_i, 2),
                prev_ai_mood=prev_ai, prev_ai_intensity=round(prev_ai_i, 2),
                recent_history=recent_history,
                user_message=str(user_msg or "")[:800],
                assistant_reply=str(ai_reply or "")[:800])}]

            resp = await self.llm.chat(snap, prompt, source="mood", session_id=sid)
            data = repair_json(resp["content"]) or {}

            result = self.mood.apply_v2(
                user_res={
                    "mood": data.get("user_mood") or "neutral",
                    "intensity": float(data.get("user_intensity") or 0.0),
                    "attribution": data.get("user_attribution") or "none",
                    "confidence": float(data.get("confidence") or 0.0),
                    "note": data.get("note"),
                },
                ai_res={
                    "mood": data.get("ai_mood") or "neutral",
                    "intensity": float(data.get("ai_intensity") or 0.0),
                    "attribution": data.get("ai_attribution") or "none",
                    "confidence": float(data.get("confidence") or 0.0),
                    "note": data.get("note"),
                },
                peace_event=data.get("peace_event") or "none",
            )

            if self.bus and (result.get("user_changed") or result.get("ai_changed")
                             or result.get("peace_event_applied")):
                from infrastructure.event_bus import EVT_MOOD_UPDATED
                self.bus.publish_nowait(EVT_MOOD_UPDATED, result)

        except Exception:  # noqa: BLE001
            logger.warning("情绪更新失败（静默降级）", exc_info=True)

    def _should_judge_mood(self, user_msg: str) -> bool:
        """判定成本控制：消息过短或距上次判定太近时跳过 LLM 调用。"""
        min_chars = self.config.get("mood_judge_min_msg_chars", 4)
        if len(user_msg or "") < min_chars:
            return False

        min_interval = self.config.get("mood_judge_min_interval_sec", 30)
        row = self.db.query_one(
            "SELECT user_updated_at FROM mood_state WHERE id=1")
        if row and row["user_updated_at"]:
            try:
                last_dt = datetime.fromisoformat(row["user_updated_at"])
                elapsed = (now_cst() - last_dt).total_seconds()
                if elapsed < min_interval:
                    return False
            except (ValueError, TypeError):
                pass
        return True

    def _format_recent_history(self, sid: str, limit: int = 5) -> str:
        """取最近 N 轮对话，格式化为 mood_judge_v2 参考文本。"""
        rows = self.db.query_all(
            "SELECT role, content FROM conversations "
            "WHERE session_id=? AND message_type='normal' "
            "ORDER BY id DESC LIMIT ?", (sid, limit * 2))
        lines = []
        for r in reversed(rows):
            role = "用户" if r["role"] == "user" else "助手"
            content = str(r["content"] or "")[:200]
            lines.append(f"[{role}] {content}")
        return "\n".join(lines) or "（无历史）"

    async def _natural_decline_task(self, sid: str) -> None:
        """自然回落异步任务：fire-and-forget，静默降级。"""
        try:
            self.mood.natural_decline(sid)
        except Exception:  # noqa: BLE001
            logger.warning("自然回落失败", exc_info=True)

    @staticmethod
    def _context_label(intents, message: str) -> str:
        """问题类型标签：fact_query/opinion/chat/tech_help/other（供输出画像分箱）。"""
        types = {getattr(i, "intent_type", "") for i in (intents or [])}
        if any(k in (message or "") for k in ("怎么看", "你觉得", "建议", "评价", "意见", "值不值得")):
            return "opinion"
        if types & {"query_external", "query_knowledge", "query_memory"}:
            return "fact_query"
        if types & {"compute", "file_op"}:
            return "tech_help"
        if types and types <= {"chat", "meta"}:
            return "chat"
        return "other"

    @staticmethod
    def _decide_depth_level(message: str, quick_result, intents,
                            memories, tool_results) -> str:
        """场景篇幅档位决策（纯规则，零 LLM 成本）：
        brief 寒暄 / normal 常规 / detailed 深度解答。
        brief 优先于 detailed：简单问候即使命中背景记忆也保持简短。"""
        types = {getattr(i, "intent_type", "") for i in (intents or [])}
        tools = {t for i in (intents or [])
                 for t in (getattr(i, "tools_needed", None) or [])}
        # brief：快速通道 + 短消息 + 无工具需求 + 纯 chat/meta 意图
        if (quick_result is not None and not quick_result.needs_convergence
                and len(message) <= 20 and not tools
                and types and types <= {"chat", "meta"}):
            return "brief"
        # detailed：需深度收敛 / 工具类意图 / 有工具执行 / 记忆命中较多
        if (quick_result is not None and quick_result.needs_convergence
                or bool(types & {"query_external", "query_knowledge",
                                 "compute", "file_op"})
                or bool(tool_results)
                or len(memories or []) >= 3):
            return "detailed"
        return "normal"

    def _mark_review_candidate(self, sid: str, user_msg_id: int, message: str,
                               intents, priority: int = 0) -> None:
        """第 8 步主动记忆检测：含新事实句式且非记忆指令 → 写入回顾候选表。"""
        types = {getattr(i, "intent_type", "") for i in (intents or [])}
        if "remember_intent" in types or "remember_confirm" in types:
            return  # 已走主动记忆路径
        if not any(re.search(p, message or "") for p in _FACT_PATTERNS):
            return
        self.db.execute(
            "INSERT OR IGNORE INTO review_candidates"
            "(message_id,session_id,created_at,priority) VALUES(?,?,?,?)",
            (user_msg_id, sid, now_cst().isoformat(timespec="seconds"), priority))

    def _check_budget_alert(self) -> None:
        """成本控制（产品文档 §预算告警 / 超预算策略）。

        统计今日 / 本月 token 用量，达到 budget_alert_ratio（默认 80%）或 100%
        时按 over_budget_strategy 处置。当前唯一策略 remind_only：仅推系统通知
        提醒（配合 NotificationManager 24h 去重），不阻断对话。
        """
        strategy = self.config.get("over_budget_strategy", "remind_only")
        alert_ratio = self.config.get("budget_alert_ratio", 80)
        now = now_cst()
        checks = [
            ("今日", "daily", self.config.get("daily_token_budget", 500000),
             now.strftime("%Y-%m-%d")),
            ("本月", "monthly", self.config.get("monthly_token_budget", 10000000),
             now.strftime("%Y-%m")),
        ]
        for label, scope, budget, prefix in checks:
            if not budget:  # 预算为 0 视为不限额，不告警
                continue
            used = self.db.query_one(
                "SELECT COALESCE(SUM(input_tokens+output_tokens),0) t FROM token_usage "
                "WHERE create_time LIKE ?", (f"{prefix}%",))["t"]
            ratio = used / budget * 100
            if ratio >= 100:
                self._notify_budget(
                    strategy, f"budget_exceeded_{scope}",
                    f"{label} Token 预算已用完（{used}/{budget}），继续对话将超额。")
            elif ratio >= alert_ratio:
                self._notify_budget(
                    strategy, f"budget_alert_{scope}",
                    f"{label}已使用 {ratio:.0f}% Token 预算（{used}/{budget}）。")

    def _notify_budget(self, strategy: str, ntype: str, message: str) -> None:
        """按超预算策略处置。remind_only：仅提醒不阻断（当前唯一策略）。"""
        if strategy == "remind_only":
            self.notify(ntype, message)
