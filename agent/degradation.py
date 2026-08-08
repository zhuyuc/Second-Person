"""
三态降级模型（收敛式理解优化方案 §5）。

用"正常 / 诚实澄清 / 明确中止"三态替代旧的"成功 / 带病降级"二态。
判据不是"能不能继续"，而是"继续了会不会给出似是而非的答案"。

分流逻辑（两步判定）：
  第一道：跳过该环节会不会导致错误或误导的答案？
    → 不会 → 态一（安全跳过）
    → 会   → 进入第二道判断
  第二道：是能力边界失败，还是系统故障失败？
    → 能力边界（系统正常但答不好）→ 态二（诚实澄清）
    → 系统故障（技术性挂了）→ 态三（明确中止）

核心红线：绝不用降级后的低质量理解，去生成一个看似正常的高置信度回答。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DegradationState(str, Enum):
    """三态降级的目标状态。"""
    STATE_1 = "state_1"  # 正常：安全跳过该环节，照常回答
    STATE_2 = "state_2"  # 诚实澄清：能力边界，反问或坦诚不知道
    STATE_3 = "state_3"  # 明确中止：系统故障，告知服务异常


class FailureType(str, Enum):
    """失败性质分类。"""
    CAPABILITY_BOUNDARY = "capability_boundary"  # 系统正常，但信息不足/答不好
    SYSTEM_FAULT = "system_fault"                # 技术性故障（超时/崩溃/熔断）


@dataclass
class DegradationDecision:
    """三态降级判定结果。"""
    state: DegradationState
    decision_reason: str            # 触发原因（如 "intent_parse_retry_exhausted"）
    failed_step: str                # 失败的环节名
    failure_type: FailureType       # 失败性质
    skip_causes_misleading: bool    # 第一道判断：跳过是否致错/误导
    message: str = ""               # 面向用户的提示文本（态二/态三用）


def decide_degradation(
    failed_step: str,
    error: str,
    skip_causes_misleading: bool,
    failure_type: FailureType | None = None,
) -> DegradationDecision:
    """两步判定：跳过是否致错 → 能力边界还是系统故障 → 路由到对应的态。

    调用方需先自行判断 skip_causes_misleading（第一道判断），
    然后传入本函数做第二道判断。若调用方已能判定 failure_type 则直接传入。
    """
    # 第一道判断已在调用方完成（skip_causes_misleading 参数）

    if not skip_causes_misleading:
        # 跳过该环节不会导致错误或误导 → 态一：安全跳过
        return DegradationDecision(
            state=DegradationState.STATE_1,
            decision_reason=f"{failed_step}_safe_skip",
            failed_step=failed_step,
            failure_type=failure_type or FailureType.CAPABILITY_BOUNDARY,
            skip_causes_misleading=False,
        )

    # 第二道判断：能力边界还是系统故障
    if failure_type is None:
        # 调用方未明确分类时，基于 error 关键词推断
        failure_type = _infer_failure_type(error)

    if failure_type == FailureType.SYSTEM_FAULT:
        return DegradationDecision(
            state=DegradationState.STATE_3,
            decision_reason=f"{failed_step}_system_fault",
            failed_step=failed_step,
            failure_type=FailureType.SYSTEM_FAULT,
            skip_causes_misleading=True,
            message=f"服务异常（{failed_step}），请稍后重试",
        )
    else:
        return DegradationDecision(
            state=DegradationState.STATE_2,
            decision_reason=f"{failed_step}_capability_boundary",
            failed_step=failed_step,
            failure_type=FailureType.CAPABILITY_BOUNDARY,
            skip_causes_misleading=True,
            message="关于这个问题，我需要更多信息才能准确回答",
        )


def _infer_failure_type(error: str) -> FailureType:
    """基于错误信息关键词推断失败性质。"""
    error_lower = error.lower()
    system_keywords = (
        "timeout", "circuit", "熔断", "超时", "connection",
        "unavailable", "500", "502", "503", "crash",
    )
    if any(kw in error_lower for kw in system_keywords):
        return FailureType.SYSTEM_FAULT
    return FailureType.CAPABILITY_BOUNDARY
