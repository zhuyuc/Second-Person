"""
元认知协议（意图理解与响应质量优化方案 v3 §六）。

职责边界：对复杂问题产出"思考骨架"（CognitiveSkeleton），不产出回答内容。
用五个通用动作（Reframe/Decompose/Surface Assumptions/Expert Lens/Answer Shape）
应对任意问题类型，不做分类路由。

降级（对话零阻塞铁律）：8s 超时 / LLM 不可用 / JSON 修复失败 → 返回 None，
调用方跳过骨架直接生成（态一安全跳过），不阻塞主链路。
骨架环节允许消费记忆片段（expert_lens 领域知识补齐）——骨架是内容生产，
与"策略决策不消费 memories"的正交性约束不冲突。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from infrastructure.json_repair import repair_json
from infrastructure.prompt_loader import PROMPTS

logger = logging.getLogger("second_person.metacog")

# 骨架提取超时（秒）：超时跳过骨架直接生成（态一）。
# 初始 8s 上线实测被慢速模型吞掉 100% 失败；骨架仅高复杂度低频触发，
# 该场景用户等待容忍度高（本就经收敛环），质量优先放宽到 20s；
# 后续按 Langfuse skeleton_extraction 失败率校准
SKELETON_TIMEOUT_SEC = 20.0


@dataclass
class CognitiveSkeleton:
    """五步元协议产出的思考骨架。"""
    reframe: dict = field(default_factory=dict)
    decompose: dict = field(default_factory=dict)
    hidden_assumptions: list = field(default_factory=list)
    expert_lens: dict = field(default_factory=dict)
    answer_shape: dict = field(default_factory=dict)
    reasoning: str = ""

    def to_dict(self) -> dict:
        return {"reframe": self.reframe, "decompose": self.decompose,
                "hidden_assumptions": self.hidden_assumptions,
                "expert_lens": self.expert_lens,
                "answer_shape": self.answer_shape, "reasoning": self.reasoning}

    def to_prompt_text(self) -> str:
        """骨架文本化：注入生成 prompt 的"思考骨架"段。"""
        parts = []
        if self.reframe.get("needed") and self.reframe.get("real_question"):
            parts.append(f"真正要解决的问题：{self.reframe['real_question']}")
        if self.decompose.get("needed") and self.decompose.get("parts"):
            parts.append("问题分解（" + str(self.decompose.get("logic", "")) + "）：\n"
                         + "\n".join(f"- {p}" for p in self.decompose["parts"]))
        bad = [a for a in self.hidden_assumptions if not a.get("holds", True)]
        if bad:
            parts.append("不成立的隐藏假设：\n" + "\n".join(
                f"- {a.get('assumption', '')}：{a.get('issue', '')}" for a in bad))
        el = self.expert_lens
        if el.get("non_obvious_insight"):
            parts.append(f"专家视角（{el.get('domain', '')}）："
                         f"{el.get('essence', '')}\n"
                         f"关键洞察（必须在回答中呈现，不可稀释）：{el['non_obvious_insight']}")
        ash = self.answer_shape
        if ash.get("opening_move") or ash.get("closing_move"):
            parts.append(f"答案形态：{ash.get('form', '')}；"
                         f"开头：{ash.get('opening_move', '')}；"
                         f"收尾：{ash.get('closing_move', '')}")
        return "\n\n".join(parts)


class MetaCognitiveProtocol:
    def __init__(self, llm_client, provider_snapshot_fn):
        self.llm = llm_client
        self.snapshot_fn = provider_snapshot_fn

    async def extract(self, message: str, strategy,
                      memories_text: str = "", deep_intent: str = "",
                      session_id: str | None = None) -> CognitiveSkeleton | None:
        """提取思考骨架。任何失败返回 None（态一跳过），不抛异常。"""
        snap = self.snapshot_fn()
        if snap is None:
            return None
        system = PROMPTS.render(
            "agent/prompts/meta_cognitive",
            examples=PROMPTS.load_raw("agent/prompts/meta_cognitive_examples"))
        parts = [f"用户消息：{message}"]
        if deep_intent:
            parts.append(f"深层诉求：{deep_intent}")
        if strategy is not None:
            parts.append(f"策略洞察触发点：{'、'.join(strategy.insight_hooks) or '无'}")
        if memories_text:
            parts.append(
                f"相关记忆片段（供 expert_lens 领域知识补齐）：\n{memories_text[:3000]}")
        user_content = "\n\n".join(parts)
        try:
            return await asyncio.wait_for(
                self._extract(snap, system, user_content, session_id),
                SKELETON_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            logger.warning("元认知骨架提取超时（%.0fs），跳过骨架直接生成",
                           SKELETON_TIMEOUT_SEC)
            return None
        except Exception as e:  # noqa: BLE001 - 零阻塞铁律
            logger.warning("元认知骨架提取失败：%s，跳过骨架直接生成", e)
            return None

    async def _extract(self, snap, system: str, user_content: str,
                       session_id: str | None) -> CognitiveSkeleton | None:
        resp = await self.llm.chat(
            snap,
            [{"role": "system", "content": system},
             {"role": "user", "content": user_content}],
            source="meta_cognitive",
            session_id=session_id,
        )
        data = repair_json(resp["content"])
        if not isinstance(data, dict):
            logger.warning("元认知输出非 JSON 对象，跳过骨架")
            return None
        return CognitiveSkeleton(
            reframe=data.get("reframe") or {},
            decompose=data.get("decompose") or {},
            hidden_assumptions=[a for a in (data.get("hidden_assumptions") or [])
                                if isinstance(a, dict)][:5],
            expert_lens=data.get("expert_lens") or {},
            answer_shape=data.get("answer_shape") or {},
            reasoning=str(data.get("reasoning") or "")[:500],
        )
