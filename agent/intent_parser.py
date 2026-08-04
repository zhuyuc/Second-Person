"""
意图识别（开发文档 §1.1 第 4 步 / §6.17）。

LLM 结构化输出拆解所有独立意图；每个意图含 id/intent_summary/intent_type/
tools_needed/depends_on。intent_type 必须取自固定枚举。
JSON 修复链失败重试最多 3 次。
特殊意图检测：remember_intent / soul_feedback / output_preference_feedback。
"""
from __future__ import annotations

import logging
import re
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
                    session_id: str | None = None,
                    recent_history: list[dict] | None = None) -> list[Intent]:
        snap = self.snapshot_fn()
        if snap is None:
            # LLM 不可用：降级为单 chat 意图
            return [Intent("i1", user_message[:50], "chat")]

        history_block = self._format_history(recent_history)
        base_messages = [
            {"role": "system", "content": PROMPTS.render(
                "agent/prompts/intent_system",
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
                                           source="intent_parse", session_id=session_id)
                raw_content = resp["content"]
                data = repair_json(raw_content)
                result = self._to_intents(data)

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

        # 最终兜底：返回 chat 意图，对话继续（不再抛出 500）
        logger.error("意图解析最终失败，降级为 chat：%s", last_err)
        return [Intent("i1", user_message[:80], "chat")]

    @staticmethod
    def _format_history(recent_history: list[dict] | None) -> str:
        """格式化最近对话为意图理解的上下文块（最多 3 轮/6 条，单条截 300 字符）。"""
        if not recent_history:
            return ""
        lines = []
        for m in recent_history[-6:]:
            role = "用户" if m.get("role") == "user" else "AI"
            content = str(m.get("content", "") or "")[:300]
            if content.strip():
                lines.append(f"{role}：{content}")
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
