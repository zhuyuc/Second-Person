"""
意图识别与收敛式理解（开发文档 §1.1 / 收敛式理解优化方案 §2-4）。

核心改动（v8.0）：
- 意图解析从流水线第一站拆为"快速预判 + 收敛"两段
- 新增 Understanding / EmotionState / FocusResult / QuickIntentResult 数据结构
- 新增 AttentionFocuser / GapDetector 协作模块
- LLM 结构化输出拆解所有独立意图
- JSON 修复链失败重试最多 3 次

三态降级（§5）：
- LLM 不可用或重试耗尽时不再静默返回 chat 意图，改为抛出 DegradationError
- 非法 intent_type 做安全跳过（态一），但加 WARNING 日志标记
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from infrastructure.json_repair import repair_json
from infrastructure.prompt_loader import PROMPTS

from .degradation import (
    DegradationDecision,
    FailureType,
    decide_degradation,
)
from .context_signals import (
    needs_convergence_rule,
    correct_tools_rule,
)

logger = logging.getLogger("second_person.intent")

INTENT_TYPES = [
    "query_memory", "query_knowledge", "query_external", "compute", "file_op",
    "remember_intent", "remember_confirm", "soul_feedback",
    "output_preference_feedback", "meta", "chat", "unknown",
]


# 默认中文标签
INTENT_TYPE_LABELS = {
    "query_memory": "检索记忆", "query_knowledge": "查询知识库",
    "query_external": "查询外部信息", "compute": "计算任务",
    "file_op": "文件操作", "remember_intent": "记忆指令",
    "remember_confirm": "重要信息待确认",
    "soul_feedback": "风格反馈", "output_preference_feedback": "输出偏好反馈",
    "meta": "系统相关", "chat": "日常对话",
    "unknown": "意图不明",
}


@dataclass
class Intent:
    id: str
    intent_summary: str
    intent_type: str
    tools_needed: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    confidence: float = 1.0


# ---- 收敛式理解数据结构（§3.2） --------------------------------------------

@dataclass
class EmotionState:
    """情绪连续状态（替代旧的开关式）。"""
    valence: str       # 情绪类型标签（如 "curious"/"anxious"/"calm"）
    intensity: float   # 强度 0-1，始终 > 0，低强度接近中性基线


@dataclass
class FocusResult:
    """注意力聚焦结果。"""
    demand_points: list[dict]      # [{"point": "诉求描述", "weight": 0.6}, ...]
    primary_focus: str | None      # 权重最高的诉求
    is_competitive: bool = False   # 多个诉求权重接近均等
    competition_note: str = ""


@dataclass
class Understanding:
    """理解包：收敛环产出的统一对象，替代原单薄的 intent_result。"""
    rich_intent: Intent            # 丰满意图
    emotion_state: EmotionState    # 情绪状态
    focus: FocusResult | None = None  # 焦点结果


@dataclass
class QuickIntentResult:
    """快速预判结果。"""
    intent_hypothesis: str         # 初步意图假设
    needs_convergence: bool        # 是否需要深度收敛
    complexity_reason: str         # 复杂度判断理由
    complexity_hint: int = 3       # 复杂度先验 0-10（策略引擎强先验，v3 §四）
    consistency_corrected: bool = False  # 代码侧一致性校正标记（v3 §S2，进 Langfuse）


@dataclass
class GapResult:
    """缺口检测结果。"""
    gaps: list[dict]               # [{"type":"...", "description":"..."}]
    has_gaps: bool
    retarget_tasks: list[dict]     # [{"target":"...", "description":"..."}]
    unresolvable: bool = False     # 缺口无法在系统内消解
    material_slots: list[str] = field(default_factory=list)  # 材料缺口：下游材料充实环节消解


@dataclass
class RetargetTask:
    """定向重收集任务。"""
    target: str                    # "context_gather"|"attention_focus"|"intent_converge"
    description: str               # 任务描述


# ---- 注意聚焦器（§3.4） -----------------------------------------------------

class AttentionFocuser:
    """分析用户消息中的诉求点，分配焦点权重。"""

    def __init__(self, llm_client, provider_snapshot_fn):
        self.llm = llm_client
        self.snapshot_fn = provider_snapshot_fn

    async def focus(self, user_message: str, context_memories: str = "",
                    emotion: EmotionState | None = None,
                    session_id: str | None = None,
                    recent_history: str = "") -> FocusResult:
        """分析注意力焦点。记忆上下文、近期对话和情绪状态作为辅助输入。"""
        snap = self.snapshot_fn()
        if snap is None:
            return FocusResult(
                demand_points=[{"point": user_message[:60], "weight": 1.0}],
                primary_focus=user_message[:60],
            )
        ctx_parts = []
        if recent_history:
            ctx_parts.append(f"近期对话：\n{recent_history}")
        ctx_parts.append(f"用户消息：{user_message}")
        if context_memories:
            ctx_parts.append(f"相关记忆：{context_memories[:2000]}")
        if emotion:
            ctx_parts.append(
                f"当前用户情绪：{emotion.valence}（强度 {emotion.intensity:.2f}）")
        user_content = "\n\n".join(ctx_parts)

        system = PROMPTS.load_raw("agent/prompts/attention_focus")
        try:
            resp = await self.llm.chat(
                snap,
                [{"role": "system", "content": system},
                 {"role": "user", "content": user_content}],
                source="attention_focus",
                session_id=session_id,
                json_mode=True,
                extra_body={"thinking_enabled": False},
            )
            data = repair_json(resp["content"]) or {}
            return FocusResult(
                demand_points=data.get("demand_points", []),
                primary_focus=data.get("primary_focus"),
                is_competitive=data.get("is_competitive", False),
                competition_note=data.get("competition_note", ""),
            )
        except Exception:
            logger.warning("注意力聚焦失败，回退默认焦点", exc_info=True)
            return FocusResult(
                demand_points=[{"point": user_message[:60], "weight": 1.0}],
                primary_focus=user_message[:60],
            )


# ---- 缺口检测器（§4.1） -----------------------------------------------------

class GapDetector:
    """检测理解缺口：指代空洞 / 焦点竞争 / 情绪-意图矛盾。"""

    def __init__(self, llm_client, provider_snapshot_fn):
        self.llm = llm_client
        self.snapshot_fn = provider_snapshot_fn

    async def detect(self, understanding: Understanding,
                     user_message: str,
                     session_id: str | None = None,
                     recent_history: str = "",
                     profile_summary: str = "") -> GapResult:
        """检测理解缺口并产出定向重收集任务。

        profile_summary：用户画像全维度摘要（零 LLM 拼装）。缺口判定需先对照
        画像——画像中已存在的缺失信息不构成缺口，写入 material_slots 由下游
        材料充实环节回填。
        """
        snap = self.snapshot_fn()
        if snap is None:
            return GapResult(gaps=[], has_gaps=False, retarget_tasks=[])

        system = PROMPTS.load_raw("agent/prompts/gap_detect")
        ctx_parts = []
        if recent_history:
            ctx_parts.append(f"近期对话：\n{recent_history}")
        ctx_parts.append(f"用户消息：{user_message}")
        ctx_parts.append(f"丰满意图：{understanding.rich_intent.intent_summary}")
        ctx_parts.append(f"意图类型：{understanding.rich_intent.intent_type}")
        ctx_parts.append(
            f"情绪状态：{understanding.emotion_state.valence}"
            f"（强度 {understanding.emotion_state.intensity:.2f}）")
        if understanding.focus:
            ctx_parts.append(f"主要焦点：{understanding.focus.primary_focus}")
            ctx_parts.append(
                f"焦点竞争：{'是' if understanding.focus.is_competitive else '否'}")
        if profile_summary:
            ctx_parts.append(
                f"系统已知用户信息（画像）：\n{profile_summary}")
        ctx = "\n".join(ctx_parts)

        try:
            resp = await self.llm.chat(
                snap,
                [{"role": "system", "content": system},
                 {"role": "user", "content": ctx}],
                source="gap_detect",
                session_id=session_id,
                json_mode=True,
                extra_body={"thinking_enabled": False},
            )
            data = repair_json(resp["content"]) or {}
            return GapResult(
                gaps=data.get("gaps", []),
                has_gaps=data.get("has_gaps", False),
                retarget_tasks=data.get("retarget_tasks", []),
                unresolvable=data.get("unresolvable", False),
                material_slots=data.get("material_slots", []),
            )
        except Exception:
            logger.warning("缺口检测失败，回退无缺口", exc_info=True)
            return GapResult(gaps=[], has_gaps=False, retarget_tasks=[])


# ---- 复杂度先验护栏（v3 §S2） ----------------------------------------------

# complexity_hint ≥ 7 时必须进收敛环：高复杂度却跳过收敛，策略引擎只能基于
# 粗糙假设决策，属 prompt 违背——代码强制校正并记 WARNING
def enforce_hint_consistency(result: QuickIntentResult) -> QuickIntentResult:
    if result.complexity_hint >= 7 and not result.needs_convergence:
        logger.warning(
            "quick_intent 一致性校正：complexity_hint=%d 但 needs_convergence=False，"
            "强制进入收敛环", result.complexity_hint)
        result.needs_convergence = True
        result.consistency_corrected = True
    return result


def _clamp_hint(v) -> int:
    try:
        return max(0, min(10, int(v)))
    except (TypeError, ValueError):
        return 3


class DegradationError(Exception):
    """三态降级异常：携带 DegradationDecision，由调用方路由到对应态。"""

    def __init__(self, decision: DegradationDecision):
        super().__init__(decision.message or str(decision.decision_reason))
        self.decision = decision


class IntentParser:
    def __init__(self, llm_client, provider_snapshot_fn):
        self.llm = llm_client
        self.snapshot_fn = provider_snapshot_fn  # () -> ProviderSnapshot (chat)

    # ---- 快速预判（§3.1） --------------------------------------------------
    async def quick_intent(self, user_message: str,
                           session_id: str | None = None,
                           recent_history: str = "") -> QuickIntentResult:
        """快速预判：当前消息 + 近期对话消解指代 → 初步意图假设 + 是否需要深度收敛。"""
        snap = self.snapshot_fn()
        if snap is None:
            return QuickIntentResult(
                intent_hypothesis=user_message[:50],
                needs_convergence=False,
                complexity_reason="LLM 不可用，默认快速通道",
            )
        system = PROMPTS.load_raw("agent/prompts/quick_intent")
        # 拼接上下文：近期对话在前，帮助消解"你推荐的""上次说的"等悬空指代
        user_content = user_message
        if recent_history:
            user_content = f"近期对话：\n{recent_history}\n\n当前消息：{user_message}"
        try:
            resp = await self.llm.chat(
                snap,
                [{"role": "system", "content": system},
                 {"role": "user", "content": user_content}],
                source="quick_intent",
                session_id=session_id,
                json_mode=True,
                extra_body={"thinking_enabled": False},
            )
            data = repair_json(resp["content"]) or {}
            result = QuickIntentResult(
                intent_hypothesis=data.get(
                    "intent_hypothesis", user_message[:50]),
                needs_convergence=data.get("needs_convergence", False),
                complexity_reason=data.get("complexity_reason", ""),
                complexity_hint=_clamp_hint(data.get("complexity_hint")),
            )
            result = enforce_hint_consistency(result)
            # 规则校正：LLM 判为简单但含强指代/导出+指代信号 → 强制收敛
            if not result.needs_convergence:
                force, reason = needs_convergence_rule(user_message)
                if force:
                    logger.warning("quick_intent 规则强制收敛：%s", reason)
                    result.needs_convergence = True
                    result.consistency_corrected = True
                    result.complexity_reason = f"规则强制收敛：{reason}"
                    result.complexity_hint = max(result.complexity_hint, 5)
            return result
        except Exception:
            logger.error("快速预判 LLM 调用失败，降级走规则判断", exc_info=True)
            # LLM 失败时也走规则判断，不直接默认快速通道
            result = QuickIntentResult(
                intent_hypothesis=user_message[:50],
                needs_convergence=False,
                complexity_reason="快速预判 LLM 调用失败",
                complexity_hint=3,
            )
            force, _reason = needs_convergence_rule(user_message)
            if force:
                result.needs_convergence = True
                result.consistency_corrected = True
                result.complexity_reason = "LLM 失败 + 规则强制收敛"
                result.complexity_hint = max(result.complexity_hint, 5)
            return result

    # ---- 意图收敛（§3.3） --------------------------------------------------
    async def converge_intent(
        self,
        user_message: str,
        tool_names: list[str],
        quick_result: QuickIntentResult,
        memories_text: str = "",
        emotion_state: EmotionState | None = None,
        focus_result: FocusResult | None = None,
        session_id: str | None = None,
        recent_history: str = "",
    ) -> tuple[list[Intent], str]:
        """整合多方信息，修正预判，输出丰满意图。

        返回 (intents, correction_note)。
        """
        snap = self.snapshot_fn()
        if snap is None:
            # LLM 不可用 → 态三
            raise DegradationError(decide_degradation(
                failed_step="intent_converge",
                error="LLM 不可用",
                skip_causes_misleading=True,
                failure_type=FailureType.SYSTEM_FAULT,
            ))
        system = PROMPTS.render(
            "agent/prompts/converge_intent",
            intent_shared=PROMPTS.load_raw("agent/prompts/intent_shared"),
            tool_names=", ".join(tool_names))
        # 拼接 context：近期对话在前，消解悬空指代
        parts = []
        if recent_history:
            parts.append(f"近期对话：\n{recent_history}")
        parts.append(f"用户消息：{user_message}")
        parts.append(f"快速预判假设：{quick_result.intent_hypothesis}")
        if memories_text:
            parts.append(f"记忆检索结果：\n{memories_text[:4000]}")
        if emotion_state:
            parts.append(
                f"情绪状态：{emotion_state.valence}（强度 {emotion_state.intensity:.2f}）")
        if focus_result:
            parts.append(f"注意力焦点：{focus_result.primary_focus}")
        if focus_result and focus_result.is_competitive:
            parts.append(f"焦点竞争提示：{focus_result.competition_note}")
        user_content = "\n\n".join(parts)

        try:
            resp = await self.llm.chat(
                snap,
                [{"role": "system", "content": system},
                 {"role": "user", "content": user_content}],
                source="converge_intent",
                session_id=session_id,
                json_mode=True,
                extra_body={"thinking_enabled": False},
            )
            data = repair_json(resp["content"]) or {}
            intents = self._to_intents(data)
            correction_note = data.get("correction_note", "")
            return intents, correction_note
        except Exception as e:
            logger.warning("意图收敛失败：%s", e)
            raise DegradationError(decide_degradation(
                failed_step="intent_converge",
                error=str(e),
                skip_causes_misleading=True,
                failure_type=FailureType.SYSTEM_FAULT,
            ))

    # ---- 原有意图解析（保留兼容） ------------------------------------------
    async def parse(self, user_message: str, tool_names: list[str],
                    session_id: str | None = None,
                    recent_history: list[dict] | None = None) -> list[Intent]:
        snap = self.snapshot_fn()
        if snap is None:
            # LLM 不可用 → 态三明确中止，不再静默返回 chat 意图
            raise DegradationError(decide_degradation(
                failed_step="intent_parse",
                error="LLM 不可用（未配置或全部熔断）",
                skip_causes_misleading=True,
                failure_type=FailureType.SYSTEM_FAULT,
            ))

        history_block = self._format_history(recent_history)
        base_messages = [
            {"role": "system", "content": PROMPTS.render(
                "agent/prompts/intent_system",
                intent_shared=PROMPTS.load_raw("agent/prompts/intent_shared"),
                tool_names=", ".join(tool_names),
                recent_history=(
                    f"最近对话上下文（仅供理解用户意图，不作为回答材料）：\n{history_block}"
                    if history_block else ""
                ),
            )},
            {"role": "user", "content": user_message},
        ]

        last_err = None
        last_bad_output = ""
        resp = None

        for attempt in range(3):
            messages = list(base_messages)

            # 第 2 次起：把上次的错误输出告诉模型，让它自我纠正
            if attempt > 0 and last_bad_output:
                messages.append(
                    {"role": "assistant", "content": last_bad_output})
                messages.append({"role": "user", "content": (
                    f"上次输出解析失败：{last_err}。"
                    "请只输出合法 JSON，intent_type 必须取自枚举列表。")})

            try:
                resp = await self.llm.chat(snap, messages,
                                           source="intent_parse", session_id=session_id,
                                           json_mode=True)
                raw_content = resp["content"]
                data = repair_json(raw_content)
                result = self._to_intents(data)

                # 规则校正：chat 意图但含明确工具信号 → 修正意图类型和工具
                result = self._correct_tools_by_rule(result, user_message)

                # 软失败检测：全部降级为 chat 且用户消息有明显检索/工具意图时重试
                if (all(r.intent_type == "chat" for r in result)
                        and self._has_tool_intent(user_message)):
                    last_bad_output = raw_content
                    last_err = "所有意图均降级为 chat，可能枚举值识别失败"
                    continue

                return result

            except Exception as e:  # noqa: BLE001
                last_bad_output = resp["content"] if resp else ""
                last_err = e
                logger.warning("意图解析失败(第 %d 次)：%s", attempt + 1, e)

        # 最终兜底：不再静默返回 chat 意图，改为态三明确中止
        logger.error("意图解析最终失败，转为态三明确中止：%s", last_err)
        raise DegradationError(decide_degradation(
            failed_step="intent_parse",
            error=str(last_err or "重试耗尽"),
            skip_causes_misleading=True,
            failure_type=FailureType.SYSTEM_FAULT,
        ))

    @staticmethod
    def _format_history(recent_history: list[dict] | None) -> str:
        """格式化最近对话为意图理解的上下文块（最多 3 轮/6 条，单条截 300 字符）。

        role=system 为提议—确认闭环注入的【待执行提议】标记，原样保留。
        """
        if not recent_history:
            return ""
        lines = []
        for m in recent_history[-6:]:
            role = m.get("role")
            if role == "system":
                prefix = ""
            elif role == "user":
                prefix = "用户："
            else:
                prefix = "AI："
            content = str(m.get("content", "") or "")[:300]
            if content.strip():
                lines.append(f"{prefix}{content}")
        return "\n".join(lines)

    # 软失败判定用：用户消息含工具/检索意图信号时，全部降级为 chat 应触发重试
    _TOOL_INTENT_SIGNALS = [
        r"查", r"搜", r"计算", r"帮我", r"写(一|个|段)", r"生成",
        r"记住", r"存(到|入)", r"告诉我", r"分析", r"解释", r"是什么",
        r"怎么样", r"怎么(做|实现|配置)", r"为什么",
    ]

    @classmethod
    def _has_tool_intent(cls, message: str) -> bool:
        return any(re.search(p, message) for p in cls._TOOL_INTENT_SIGNALS)

    def _correct_tools_by_rule(self, intents: list[Intent],
                               message: str) -> list[Intent]:
        """规则校正：chat 意图但含明确工具信号时修正意图类型和 tools_needed。

        仅在 LLM 返回的意图全部为 chat 时执行校正，避免覆盖已正确识别的意图。
        """
        if not all(it.intent_type == "chat" for it in intents):
            return intents
        tool, itype = correct_tools_rule(message)
        if tool and itype:
            for it in intents:
                if it.intent_type == "chat":
                    it.intent_type = itype
                    if tool not in (it.tools_needed or []):
                        it.tools_needed = list(
                            set((it.tools_needed or []) + [tool]))
                    logger.warning(
                        "规则校正意图：chat→%s，追加工具 %s，消息片段=%s",
                        itype, tool, message[:60])
        return intents

    def _to_intents(self, data: dict) -> list[Intent]:
        raw = data.get("intents", []) if isinstance(data, dict) else []
        out = []
        for i, it in enumerate(raw):
            itype = it.get("intent_type", "chat")
            if itype not in INTENT_TYPES:
                # 态一安全跳过：非法类型默认 chat，但记录 WARNING
                logger.warning(
                    "意图解析返回未知 intent_type '%s'，安全降级为 chat（态一）", itype)
                itype = "chat"
            raw_conf = it.get("confidence")
            try:
                conf = max(0.0, min(1.0, float(raw_conf))
                           ) if raw_conf is not None else 1.0
            except (TypeError, ValueError):
                conf = 1.0
            out.append(Intent(
                id=it.get("id", f"i{i+1}"),
                intent_summary=it.get("intent_summary", ""),
                intent_type=itype,
                tools_needed=it.get("tools_needed", []) or [],
                depends_on=it.get("depends_on", []) or [],
                confidence=conf))
        return out or [Intent("i1", "意图识别失败", "unknown")]
