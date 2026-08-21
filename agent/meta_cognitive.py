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
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from infrastructure.json_repair import repair_json
from infrastructure.prompt_loader import PROMPTS

from .intent_parser import extract_explicit_requirements, infer_deep_delivery_form

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


DELIVERY_FORMS = {"direct", "structured", "long_document"}


def _clean_problem_text(value: Any, limit: int | None = None) -> str:
    text = str(value or "").strip()
    return text[:limit] if limit is not None else text


def _unique_problem_texts(values: list[Any], limit: int | None = None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = _clean_problem_text(value)
        key = re.sub(r"\s+", "", text).lower()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
        if limit is not None and len(result) >= limit:
            break
    return result


@dataclass
class RequirementItem:
    id: str
    raw_request: str
    expected_outcome: str = ""
    dependencies: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    solution_required: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DeliveryContract:
    deliverable_type: str = "answer"
    audience: str = ""
    delivery_form: str = "direct"
    requested_artifacts: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    explicit_requirements: list[RequirementItem] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["explicit_requirements"] = [item.to_dict() for item in self.explicit_requirements]
        return data

    @classmethod
    def from_dict(cls, data: dict | None) -> "DeliveryContract":
        data = data or {}
        requirements = []
        for index, item in enumerate(data.get("explicit_requirements") or [], 1):
            if isinstance(item, dict):
                requirements.append(RequirementItem(
                    id=_clean_problem_text(item.get("id"), 30) or f"R{index}",
                    raw_request=_clean_problem_text(item.get("raw_request") or item.get("request")),
                    expected_outcome=_clean_problem_text(
                        item.get("expected_outcome") or item.get("outcome")),
                    dependencies=_unique_problem_texts(item.get("dependencies") or []),
                    acceptance_criteria=_unique_problem_texts(
                        item.get("acceptance_criteria") or []),
                    solution_required=bool(item.get("solution_required", True)),
                ))
        delivery_form = _clean_problem_text(data.get("delivery_form"), 30)
        return cls(
            deliverable_type=_clean_problem_text(data.get("deliverable_type"), 80) or "answer",
            audience=_clean_problem_text(data.get("audience")),
            delivery_form=delivery_form if delivery_form in DELIVERY_FORMS else "direct",
            requested_artifacts=_unique_problem_texts(data.get("requested_artifacts") or []),
            acceptance_criteria=_unique_problem_texts(data.get("acceptance_criteria") or []),
            explicit_requirements=requirements,
        )


@dataclass
class ProblemModel:
    user_goal: str
    contract: DeliveryContract
    facts: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    relationships: list[str] = field(default_factory=list)
    analysis_actions: list[str] = field(default_factory=list)
    evidence_needs: list[str] = field(default_factory=list)
    outline: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "user_goal": self.user_goal,
            "contract": self.contract.to_dict(),
            "facts": self.facts,
            "assumptions": self.assumptions,
            "constraints": self.constraints,
            "unknowns": self.unknowns,
            "relationships": self.relationships,
            "analysis_actions": self.analysis_actions,
            "evidence_needs": self.evidence_needs,
            "outline": self.outline,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "ProblemModel":
        data = data or {}
        return cls(
            user_goal=_clean_problem_text(data.get("user_goal")) or "完整回应用户当前问题",
            contract=DeliveryContract.from_dict(data.get("contract")),
            facts=_unique_problem_texts(data.get("facts") or []),
            assumptions=_unique_problem_texts(data.get("assumptions") or []),
            constraints=_unique_problem_texts(data.get("constraints") or []),
            unknowns=_unique_problem_texts(data.get("unknowns") or []),
            relationships=_unique_problem_texts(data.get("relationships") or []),
            analysis_actions=_unique_problem_texts(data.get("analysis_actions") or []),
            evidence_needs=_unique_problem_texts(data.get("evidence_needs") or []),
            outline=[item for item in (data.get("outline") or []) if isinstance(item, dict)],
        )

    def safe_summary(self) -> dict:
        return {
            "goal": self.user_goal[:120],
            "requirement_count": len(self.contract.explicit_requirements),
            "delivery_form": self.contract.delivery_form,
            "evidence_need_count": len(self.evidence_needs),
            "analysis_actions": self.analysis_actions[:4],
        }

    def prompt_text(self) -> str:
        requirements = "\n".join(
            f"- [{item.id}] 需求：{item.raw_request}\n  期望结果："
            f"{item.expected_outcome or '给出可执行解法'}\n  依赖："
            f"{'、'.join(item.dependencies) or '按问题判断'}\n  验收："
            f"{'、'.join(item.acceptance_criteria) or '给出机制、边界与验证方式'}"
            for item in self.contract.explicit_requirements)
        blocks = [
            "## 深度问题模型（本轮回答必须遵循）",
            f"用户真正目标：{self.user_goal}",
            "### 必须逐项覆盖的明确需求\n"
            + (requirements or "- [R1] 完整回应用户当前请求"),
        ]
        for label, values in (
                ("已知事实", self.facts), ("约束", self.constraints),
                ("待验证假设", self.assumptions), ("关键未知项", self.unknowns),
                ("关系与依赖", self.relationships), ("应执行的分析动作", self.analysis_actions),
                ("证据需求", self.evidence_needs)):
            if values:
                blocks.append(f"### {label}\n" + "\n".join(f"- {value}" for value in values))
        blocks.append(
            "### 交付硬约束\n"
            "先给出每项明确需求的实质解法，再说明整体方案、依赖、风险和实施顺序。"
            "事实、假设、未知项和建议必须区分；禁止用排期或后续建议替代当前可给出的解法。"
            "不要暴露内部推理过程。")
        return "\n\n".join(blocks)


class ProblemModelBuilder:
    """构建交付合同与问题模型，属于深度理解，不承担生成和任务调度。"""

    def __init__(self, llm_client, provider_snapshot_fn: Callable[[], Any]):
        self.llm = llm_client
        self.snapshot_fn = provider_snapshot_fn

    async def build(self, message: str, *, session_id: str | None = None,
                    understanding=None, skeleton=None, strategy=None,
                    recent_history: str = "") -> ProblemModel:
        fallback = self._fallback(message)
        snap = self.snapshot_fn()
        if snap is None:
            return fallback
        context = [f"用户消息：{message}"]
        if understanding is not None:
            intent = getattr(getattr(understanding, "rich_intent", None), "intent_summary", "")
            focus = getattr(getattr(understanding, "focus", None), "primary_focus", "")
            if intent:
                context.append(f"已收敛意图：{intent}")
            if focus:
                context.append(f"关注焦点：{focus}")
        if skeleton is not None:
            reframe = getattr(skeleton, "reframe", {}) or {}
            if reframe.get("real_question"):
                context.append(f"问题重构：{reframe['real_question']}")
        if strategy is not None:
            context.append(f"既有策略：{getattr(strategy, 'angle', '')}")
        if recent_history:
            context.append(f"近期上下文：\n{recent_history[-4000:]}")
        try:
            response = await self.llm.chat(
                snap,
                [{"role": "system", "content": PROMPTS.load_raw("agent/prompts/problem_model")},
                 {"role": "user", "content": "\n\n".join(context)}],
                source="problem_model", session_id=session_id, json_mode=True,
            )
            data = repair_json(response.get("content", ""))
            return self._from_llm(data, message, fallback) if isinstance(data, dict) else fallback
        except Exception:  # noqa: BLE001 - 深度增强失败保留可解释兜底
            logger.warning("问题模型构建失败，使用本地任务合同", exc_info=True)
            return fallback

    def _fallback(self, message: str) -> ProblemModel:
        requirements = [RequirementItem(
            id=f"R{index}", raw_request=text,
            expected_outcome="给出对应的完整解法、依赖、风险边界与验收方式",
            acceptance_criteria=["有具体解法", "说明依赖与验证方式"],
        ) for index, text in enumerate(extract_explicit_requirements(message), 1)]
        delivery_form = infer_deep_delivery_form(message, len(requirements))
        contract = DeliveryContract(
            deliverable_type="document" if delivery_form == "long_document" else "answer",
            delivery_form=delivery_form,
            explicit_requirements=requirements,
            acceptance_criteria=["所有明确需求都有对应解法", "事实和假设清晰区分"],
        )
        return ProblemModel(
            user_goal="完整解决用户当前提出的问题，而不是只回答表面措辞",
            contract=contract,
            constraints=["不遗漏用户明确提出的事项", "质量优先，不以缩短内容替代交付"],
            analysis_actions=["需求覆盖", "依赖与权衡分析", "质量验证"],
            outline=self._default_outline(requirements),
        )

    def _from_llm(self, data: dict, message: str, fallback: ProblemModel) -> ProblemModel:
        contract_data = data.get("contract") if isinstance(data.get("contract"), dict) else {}
        raw_requirements = data.get("requirements") or contract_data.get("explicit_requirements") or []
        requirements: list[RequirementItem] = []
        for index, raw in enumerate(raw_requirements, 1):
            if not isinstance(raw, dict):
                continue
            request = _clean_problem_text(raw.get("raw_request") or raw.get("request"))
            if request:
                requirements.append(RequirementItem(
                    id=_clean_problem_text(raw.get("id"), 30) or f"R{index}",
                    raw_request=request,
                    expected_outcome=_clean_problem_text(
                        raw.get("expected_outcome") or raw.get("outcome")),
                    dependencies=_unique_problem_texts(raw.get("dependencies") or []),
                    acceptance_criteria=_unique_problem_texts(
                        raw.get("acceptance_criteria") or []),
                    solution_required=bool(raw.get("solution_required", True)),
                ))
        known = " ".join(item.raw_request for item in requirements)
        for fallback_item in fallback.contract.explicit_requirements:
            words = _problem_keywords(fallback_item.raw_request)
            if not words or len(words & _problem_keywords(known)) / max(len(words), 1) < 0.35:
                requirements.append(RequirementItem(
                    id=f"R{len(requirements) + 1}", raw_request=fallback_item.raw_request,
                    expected_outcome=fallback_item.expected_outcome,
                    acceptance_criteria=fallback_item.acceptance_criteria,
                ))
        requirements = requirements or fallback.contract.explicit_requirements
        inferred = infer_deep_delivery_form(message, len(requirements))
        delivery_form = _clean_problem_text(
            contract_data.get("delivery_form") or data.get("delivery_form"), 30)
        if delivery_form not in DELIVERY_FORMS:
            delivery_form = inferred
        elif inferred == "long_document":
            delivery_form = "long_document"
        return ProblemModel(
            user_goal=_clean_problem_text(data.get("user_goal")) or fallback.user_goal,
            contract=DeliveryContract(
                deliverable_type=_clean_problem_text(contract_data.get("deliverable_type"), 80)
                or fallback.contract.deliverable_type,
                audience=_clean_problem_text(contract_data.get("audience")),
                delivery_form=delivery_form,
                requested_artifacts=_unique_problem_texts(
                    contract_data.get("requested_artifacts") or []),
                acceptance_criteria=_unique_problem_texts(
                    contract_data.get("acceptance_criteria")
                    or fallback.contract.acceptance_criteria),
                explicit_requirements=requirements,
            ),
            facts=_unique_problem_texts(data.get("facts") or []),
            assumptions=_unique_problem_texts(data.get("assumptions") or []),
            constraints=_unique_problem_texts(data.get("constraints") or fallback.constraints),
            unknowns=_unique_problem_texts(data.get("unknowns") or []),
            relationships=_unique_problem_texts(data.get("relationships") or []),
            analysis_actions=_unique_problem_texts(
                data.get("analysis_actions") or fallback.analysis_actions),
            evidence_needs=_unique_problem_texts(data.get("evidence_needs") or []),
            outline=[item for item in (data.get("outline") or []) if isinstance(item, dict)]
            or self._default_outline(requirements),
        )

    @staticmethod
    def _default_outline(requirements: list[RequirementItem]) -> list[dict]:
        sections = [{"id": "S0", "title": "问题目标与整体方案",
                     "requirement_ids": [item.id for item in requirements]}]
        sections.extend({"id": f"S{index}", "title": item.raw_request[:60],
                         "requirement_ids": [item.id]}
                        for index, item in enumerate(requirements, 1))
        sections.append({"id": f"S{len(sections)}", "title": "依赖、风险与验收",
                         "requirement_ids": [item.id for item in requirements]})
        return sections


def _problem_keywords(text: str) -> set[str]:
    chunks = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9_-]{2,}", text or "")
    result: set[str] = set()
    for chunk in chunks:
        if re.fullmatch(r"[\u4e00-\u9fff]+", chunk):
            result.update(chunk[index:index + 2] for index in range(max(0, len(chunk) - 1)))
        else:
            result.add(chunk.lower())
    return result


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
            json_mode=True,
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
