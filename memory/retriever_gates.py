"""Retriever gate / intent helpers — short-circuit rules before hybrid search."""
from __future__ import annotations

import re

# 唯一保留的意图识别：明确回忆语（"你还记得/我之前说过"），仅用于兜底路径
# 调低阈值再跑一次。不用于任何"关门"（不再有 personal / knowledge 分流）。
RECALL_INTENT_PATTERNS = [
    r"你还记得", r"我之前(说过|提过|讲过)", r"我上次", r"还记不记得", r"之前(聊|谈)过",
]

# 确认/致谢类：明确不需要查记忆库（仍走 retrieve 入口，内部短路）
ACK_ONLY_PATTERNS = [
    r"^(好|好的|嗯+|谢谢|感谢|多谢|OK|ok|继续|收到|明白|知道了|没问题)[。!！?？…~]*$",
]

# 第一人称历史指代：永不短路
HISTORY_REF_PATTERNS = [
    r"我的", r"上次", r"之前", r"老样子", r"照旧", r"还是那样",
]


def has_recall_intent(query: str) -> bool:
    return any(re.search(p, query) for p in RECALL_INTENT_PATTERNS)


def has_history_reference(query: str) -> bool:
    q = (query or "").strip()
    return any(re.search(p, q) for p in HISTORY_REF_PATTERNS)


def is_ack_only(query: str) -> bool:
    q = (query or "").strip()
    if not q:
        return False
    return any(re.match(p, q, re.IGNORECASE) for p in ACK_ONLY_PATTERNS)


def short_circuit_gate(query: str, context_text: str | None, min_chars: int) -> str | None:
    """Return gate code if retrieval should skip, else None."""
    q = (query or "").strip()
    if not q:
        return "empty_query"
    if has_recall_intent(q) or has_history_reference(q):
        return None
    if is_ack_only(q):
        return "ack_shortcut"
    if context_text and len(q) <= max(0, int(min_chars)):
        return "short_query_shortcircuit"
    return None


def should_short_circuit(query: str, context_text: str | None,
                         min_chars: int) -> bool:
    return short_circuit_gate(query, context_text, min_chars) is not None
