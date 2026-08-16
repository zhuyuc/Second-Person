"""
响应策略引擎（意图理解与响应质量优化方案 v3 §四）。

职责边界：统合意图/情绪/焦点/策略先验，产出唯一的 ResponseStrategy。
所有关于"回答形态、角度、深度、语气"的决策集中在此；
memories 永不入内（v3 §二：策略与记忆内容正交，记忆是生成层材料）。

降级（对话零阻塞铁律）：
- LLM 决策 5s 超时 / JSON 修复失败 / 熔断 → fallback 规则策略（态一），
  span metadata 记录 fallback_used/failure_reason/fallback_strategy_snapshot
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

from infrastructure.json_repair import repair_json
from infrastructure.prompt_loader import PROMPTS

logger = logging.getLogger("second_person.strategy")

# ---- 枚举与触发集 -----------------------------------------------------------

FORM_ENUM = ["结论型", "分析型", "确认型", "对话型", "共情型"]
TONE_ENUM = ["严肃", "轻松", "共情", "克制", "激励", "中性"]

# 元认知排除集（v3 §四.3）：工具执行/记忆指令/系统类/用户纠正 AI 类不做深度骨架
META_EXCLUDED_INTENTS = {
    "compute", "file_op",
    "remember_intent", "remember_confirm",
    "meta",
    "soul_feedback", "output_preference_feedback",
}

# 决策超时（秒）：超时走 fallback 规则策略，不阻塞主链路。
# 初始 5s 实测复杂场景 100% 超时 → 放宽 10s 后仍有双峰波动（模型延迟不稳定）；
# 双通道均已并行化（策略耗时被检索/收敛环完全掩盖），再放宽到 20s 零净增，
# 只提升真实决策成功率；后续按 Langfuse fallback 比例校准
DECIDE_TIMEOUT_SEC = 20.0

FALLBACK_NARRATIVE = "这个问题我用常规分析的方式回答。"


# ---- 数据结构（v3 §三） ------------------------------------------------------

@dataclass
class ResponseStrategy:
    # 存 DB 瘦身 5 字段（反馈闭环归因输入）：
    angle: str                          # 回答立场
    depth: int                          # 思考深度 0-3
    form: str                           # FORM_ENUM
    tone: str                           # TONE_ENUM
    complexity_score: int               # 0-10
    # 仅存 Langfuse span，不落 DB：
    should_run_meta_cognitive: bool = False
    insight_hooks: list[str] = field(default_factory=list)
    trace_reason: str = ""
    strategy_narrative: str = FALLBACK_NARRATIVE
    matched_scene: str = "none"
    # 审计字段：
    fallback_used: bool = False
    failure_reason: str = ""

    def db_snapshot(self) -> dict:
        """落库瘦身快照：只含归因所需的 5 个决策字段（v3 R5）。"""
        return {"angle": self.angle, "depth": self.depth, "form": self.form,
                "tone": self.tone, "complexity_score": self.complexity_score}

    def span_snapshot(self) -> dict:
        """Langfuse 全量快照（含可解释性字段）。"""
        return {**self.db_snapshot(),
                "should_run_meta_cognitive": self.should_run_meta_cognitive,
                "insight_hooks": self.insight_hooks,
                "trace_reason": self.trace_reason,
                "strategy_narrative": self.strategy_narrative,
                "matched_scene": self.matched_scene,
                "fallback_used": self.fallback_used,
                "failure_reason": self.failure_reason}


@dataclass
class StrategyInputs:
    """策略引擎输入（v3 §二）：memories 永不入内。"""
    message: str
    quick_result: object              # QuickIntentResult（含 complexity_hint）
    emotion: object                   # EmotionState | None
    priors: str                       # 先验全文（用户偏好 + 默认模板合并）
    # 收敛通道专属（快速通道为 None）：
    rich_intent: object = None        # Intent（含 intent_type/intent_summary）
    deep_intent: str = ""
    focus: object = None              # FocusResult | None


# ---- 引擎 -------------------------------------------------------------------

class StrategyEngine:
    def __init__(self, llm_client, provider_snapshot_fn, config, data_dir):
        self.llm = llm_client
        self.snapshot_fn = provider_snapshot_fn
        self.config = config
        self.data_dir = Path(data_dir)

    # ---- 先验加载（v3 S1：全文注入，LLM 自行匹配场景） ----------------------

    def load_priors(self) -> str:
        """用户已确认偏好（RESPONSE_STRATEGY.md）+ 默认模板合并，全文注入。

        文件不存在/为空（阶段 4 前常态）→ 仅默认模板，策略引擎冷启动稳定。
        """
        default_text = PROMPTS.load_raw(
            "agent/prompts/default_strategy_priors")
        user_path = self.data_dir / "profile" / "RESPONSE_STRATEGY.md"
        try:
            user_text = user_path.read_text(encoding="utf-8").strip()
        except OSError:
            user_text = ""
        if not user_text:
            return f"## 默认启发\n{default_text}"
        return (f"## 用户已确认偏好（遵循强度高于默认启发）\n{user_text}\n\n"
                f"## 默认启发（用户偏好未覆盖的场景用此兜底）\n{default_text}")

    # ---- 决策入口 ------------------------------------------------------------

    async def decide(self, inputs: StrategyInputs,
                     session_id: str | None = None) -> ResponseStrategy:
        """策略决策：规则短路 → LLM 决策（5s 超时）→ fallback 规则策略。"""
        qr = inputs.quick_result
        # 规则短路（v3 §四.2 确定性条件）：简单消息不调 LLM
        if qr is not None and not qr.needs_convergence and qr.complexity_hint < 3:
            return self._rule_shortcut(inputs)
        snap = self.snapshot_fn()
        if snap is None:
            return self._fallback("llm_unavailable")
        try:
            return await asyncio.wait_for(
                self._llm_decide(snap, inputs, session_id), DECIDE_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            logger.warning("策略决策超时（%.0fs），走 fallback 规则策略",
                           DECIDE_TIMEOUT_SEC)
            return self._fallback("timeout")
        except Exception as e:  # noqa: BLE001 - 零阻塞铁律：静默降级
            logger.warning("策略决策失败：%s，走 fallback 规则策略", e)
            return self._fallback("llm_error")

    async def _llm_decide(self, snap, inputs: StrategyInputs,
                          session_id: str | None) -> ResponseStrategy:
        system = PROMPTS.load_raw("agent/prompts/strategy_decide")
        parts = [f"用户消息：{inputs.message}"]
        qr = inputs.quick_result
        if qr is not None:
            parts.append(f"预判假设：{qr.intent_hypothesis}")
            parts.append(f"复杂度先验 complexity_hint：{qr.complexity_hint}")
        if inputs.emotion is not None:
            parts.append(f"用户情绪：{inputs.emotion.valence}"
                         f"（强度 {inputs.emotion.intensity:.2f}）")
        parts.append(f"策略先验：\n{inputs.priors}")
        if inputs.rich_intent is not None:
            parts.append(f"丰满意图：{inputs.rich_intent.intent_summary}"
                         f"（类型 {inputs.rich_intent.intent_type}）")
        if inputs.deep_intent:
            parts.append(f"深层诉求：{inputs.deep_intent}")
        if inputs.focus is not None and inputs.focus.primary_focus:
            parts.append(f"注意力焦点：{inputs.focus.primary_focus}")
        user_content = "\n\n".join(parts)

        resp = await self.llm.chat(
            snap,
            [{"role": "system", "content": system},
             {"role": "user", "content": user_content}],
            source="strategy_decide",
            session_id=session_id,
            json_mode=True,
        )
        data = repair_json(resp["content"])
        if not isinstance(data, dict):
            raise ValueError("策略决策输出非 JSON 对象")
        hint = getattr(inputs.quick_result, "complexity_hint", 3) \
            if inputs.quick_result is not None else 3
        return self._validated(data, hint)

    # ---- 校验与构造 ----------------------------------------------------------

    def _validated(self, data: dict, hint: int) -> ResponseStrategy:
        """枚举/值域校验：非法值落默认而非整体失败（局部容错）。"""
        # complexity_score 只允许在 hint 基础上 ±3（v3 §四.1 单一决策源）
        raw_score = data.get("complexity_score", hint)
        try:
            score = max(0, min(10, int(raw_score)))
        except (TypeError, ValueError):
            score = hint
        score = max(hint - 3, min(hint + 3, score))
        form = data.get("form") if data.get("form") in FORM_ENUM else "分析型"
        tone = data.get("tone") if data.get("tone") in TONE_ENUM else "中性"
        try:
            depth = max(0, min(3, int(data.get("depth", 2))))
        except (TypeError, ValueError):
            depth = 2
        narrative = str(data.get("strategy_narrative") or "").strip()
        return ResponseStrategy(
            angle=str(data.get("angle") or "全面评估")[:100],
            depth=depth, form=form, tone=tone, complexity_score=score,
            should_run_meta_cognitive=bool(
                data.get("should_run_meta_cognitive", False)),
            insight_hooks=[str(h)[:80]
                           for h in (data.get("insight_hooks") or [])][:3],
            trace_reason=str(data.get("trace_reason") or "")[:500],
            strategy_narrative=narrative[:120] or FALLBACK_NARRATIVE,
            matched_scene=str(data.get("matched_scene") or "none")[:20],
        )

    def _rule_shortcut(self, inputs: StrategyInputs) -> ResponseStrategy:
        """规则短路（needs_convergence=False 且 hint<3）：零 LLM。"""
        return ResponseStrategy(
            angle="直接回应", depth=1, form="对话型", tone="轻松",
            complexity_score=getattr(
                inputs.quick_result, "complexity_hint", 1),
            trace_reason="规则短路：简单消息（needs_convergence=False 且 complexity_hint<3）",
            strategy_narrative="简单直接的问题，我直接回答。",
            matched_scene="chat",
        )

    def _fallback(self, reason: str) -> ResponseStrategy:
        """fallback 规则策略（v3 §九）：depth=1/分析型/克制。"""
        return ResponseStrategy(
            angle="全面评估", depth=1, form="分析型", tone="克制",
            complexity_score=4,
            trace_reason=f"fallback 规则策略（{reason}）",
            strategy_narrative=FALLBACK_NARRATIVE,
            fallback_used=True, failure_reason=reason,
        )

    # ---- 元认知触发判定（阶段 2 接线消费，v3 §四.3） --------------------------

    @staticmethod
    def should_trigger_meta(strategy: ResponseStrategy, intent_type: str,
                            enabled: bool) -> bool:
        return (enabled and strategy.complexity_score >= 4
                and not strategy.fallback_used
                and intent_type not in META_EXCLUDED_INTENTS)

    # ---- 追问式补充信息：clarification_router（elicitation §05 触发与门槛） ---

    async def clarification_router(self, session_id: str, message: str,
                                   gap_description: str, config: dict,
                                   profile_summary: str = "") -> dict | None:
        """判定缺失信息可枚举/发散 → 可枚举时返回 ask_user seed。

        profile_summary：用户画像全维度摘要。缺失信息若已存在于画像中，
        由 prompt 约束判定为无需追问（系统直接回填），返回 None 走正常合成。
        返回 None 表示不可枚举（走文字澄清），返回 dict 表示可枚举（走追问）。
        """
        from infrastructure.prompt_loader import PROMPTS
        from infrastructure.json_repair import repair_json

        snap = self.snapshot_fn()
        if not snap:
            return None  # 模型不可用 → 走文字澄清

        system_prompt = PROMPTS.load_raw(
            "agent/prompts/elicitation_decision")
        parts = [
            f"用户消息：{message}",
            f"缺失信息：{gap_description}",
        ]
        if profile_summary:
            parts.append(f"系统已知用户信息（画像）：\n{profile_summary}")
        parts.append(
            "请判定上述缺失信息是否可枚举为 2-4 个选项。"
            "已知信息中已存在的事实禁止进入追问。")
        user_prompt = "\n".join(parts)

        try:
            resp = await self.llm.chat(snap, [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ], source="elicitation_decision", session_id=session_id,
                json_mode=True)
            raw = resp.get("content", "")
            data = repair_json(raw)

            if data.get("enumerable") and data.get("questions"):
                return {
                    "questions": data["questions"],
                    "reason": data.get("reason", ""),
                    "trigger_source": "intent_low_conf",
                }
        except Exception:  # noqa: BLE001
            logger.warning("clarification_router LLM 调用失败", exc_info=True)

        return None
