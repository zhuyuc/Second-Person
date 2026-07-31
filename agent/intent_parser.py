"""
意图识别（开发文档 §1.1 第 4 步 / §6.17）。

LLM 结构化输出拆解所有独立意图；每个意图含 id/intent_summary/intent_type/
tools_needed/depends_on。intent_type 必须取自固定枚举。
JSON 修复链失败重试最多 3 次。
特殊意图检测：remember_intent / soul_feedback / output_preference_feedback。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from infrastructure.json_repair import repair_json
from infrastructure.prompt_loader import PROMPTS

logger = logging.getLogger("second_person.intent")

INTENT_TYPES = [
    "query_memory", "query_knowledge", "query_external", "compute", "file_op",
    "remember_intent", "remember_confirm", "soul_feedback",
    "output_preference_feedback", "meta", "chat",
]


@dataclass
class Intent:
    id: str
    intent_summary: str
    intent_type: str
    tools_needed: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)


class IntentParser:
    def __init__(self, llm_client, provider_snapshot_fn):
        self.llm = llm_client
        self.snapshot_fn = provider_snapshot_fn  # () -> ProviderSnapshot (chat)

    async def parse(self, user_message: str, tool_names: list[str],
                    session_id: str | None = None) -> list[Intent]:
        snap = self.snapshot_fn()
        if snap is None:
            # LLM 不可用：降级为单 chat 意图
            return [Intent("i1", user_message[:50], "chat")]
        messages = [
            {"role": "system", "content": PROMPTS.render(
                "agent/prompts/intent_system", tool_names=", ".join(tool_names))},
            {"role": "user", "content": user_message},
        ]
        last_err = None
        for attempt in range(3):
            try:
                resp = await self.llm.chat(snap, messages, source="intent_parse",
                                           session_id=session_id)
                data = repair_json(resp["content"])
                return self._to_intents(data)
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning("意图解析失败(第 %d 次)：%s", attempt + 1, e)
        # 结构化输出重试 3 次仍失败：终止本轮，返回具体原因 + trace_id
        from infrastructure.observability import get_trace_id
        logger.error("意图解析最终失败：%s", last_err)
        raise RuntimeError(
            f"意图解析失败（已重试 3 次）：{last_err}（trace_id: {get_trace_id() or '-'}）")

    def _to_intents(self, data: dict) -> list[Intent]:
        raw = data.get("intents", []) if isinstance(data, dict) else []
        out = []
        for i, it in enumerate(raw):
            itype = it.get("intent_type", "chat")
            if itype not in INTENT_TYPES:
                itype = "chat"
            out.append(Intent(
                id=it.get("id", f"i{i+1}"),
                intent_summary=it.get("intent_summary", ""),
                intent_type=itype,
                tools_needed=it.get("tools_needed", []) or [],
                depends_on=it.get("depends_on", []) or []))
        return out or [Intent("i1", "", "chat")]
