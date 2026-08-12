"""
降级 metadata 统一 schema —— 全系统降级动作的标准记录格式。

所有降级点统一使用 DegradationMeta 记录，确保 Langfuse/Loki 中可聚合查询。
"""

from dataclasses import dataclass
from enum import Enum


class DegradationLevel(str, Enum):
    SILENT = "silent"              # 仅日志
    USER_VISIBLE = "user_visible"  # thinking_delta 外露给用户
    BLOCKING = "blocking"          # 阻断主流程


class DegradationComponent(str, Enum):
    QUICK_INTENT = "quick_intent"
    INTENT_PARSE = "intent_parse"
    CONVERSATION_SEARCH = "conversation_search"
    MEMORY_RETRIEVAL = "memory_retrieval"
    TOOL_CLAIM_VALIDATE = "tool_claim_validate"
    COMPRESSION = "compression"
    CONVERGENCE_LOOP = "convergence_loop"
    FORMAT_INFER = "format_infer"
    TOOL_EXECUTOR = "tool_executor"
    ELICITATION = "elicitation"


@dataclass
class DegradationMeta:
    degraded: bool = True
    level: DegradationLevel = DegradationLevel.SILENT
    component: DegradationComponent = DegradationComponent.QUICK_INTENT
    reason: str = ""
    fallback_taken: str = ""
    rule_corrected: bool = False

    def to_dict(self) -> dict:
        return {
            "degraded": self.degraded,
            "level": self.level.value,
            "component": self.component.value,
            "reason": self.reason,
            "fallback_taken": self.fallback_taken,
            "rule_corrected": self.rule_corrected,
        }

    def __repr__(self) -> str:
        return (
            f"DegradationMeta(level={self.level.value}, "
            f"component={self.component.value}, "
            f"reason={self.reason[:60]}, "
            f"rule_corrected={self.rule_corrected})"
        )
