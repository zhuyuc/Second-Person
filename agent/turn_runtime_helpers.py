"""Small pure helpers for TurnRuntime timeline and citation projection."""
from __future__ import annotations

import json
from typing import Any

from infrastructure.timeutil import now_cst


def summarize_tool_result(result: Any) -> str:
    """Compress an arbitrary tool result to a one-line preview for the timeline card.

    Returns "" when nothing sensible can be projected. Never raises — the
    timeline card is telemetry, and a preview failure should not affect the
    turn.
    """
    if result is None:
        return ""
    try:
        if isinstance(result, str):
            text = result
        elif isinstance(result, dict):
            for key in ("summary", "preview", "content", "path", "matches",
                         "total_lines", "action"):
                if key in result:
                    v = result[key]
                    if isinstance(v, (list, tuple)):
                        text = f"{key}={len(v)}"
                    else:
                        text = f"{key}={v}"
                    break
            else:
                text = str({k: result[k] for k in list(result)[:3]})
        elif isinstance(result, (list, tuple)):
            text = f"{len(result)} items"
        else:
            text = str(result)
    except Exception:  # noqa: BLE001
        return ""
    text = " ".join((text or "").split())
    return text[:200]


def extract_web_citations(tool_name: str, result: Any,
                          arguments: Any = None) -> list[dict[str, str]]:
    """从 web_search / web_fetch 结果提取可展示的引用链接。"""
    name = (tool_name or "").lower()
    if name == "web_fetch":
        args: dict = {}
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments)
                if isinstance(parsed, dict):
                    args = parsed
            except (TypeError, ValueError):
                pass
        elif isinstance(arguments, dict):
            args = arguments
        url = (args.get("url") or "").strip()
        if url:
            return [{"title": url, "url": url}]
        return []
    if name != "web_search":
        return []
    items = result
    if isinstance(result, str):
        try:
            items = json.loads(result)
        except (TypeError, ValueError):
            return []
    if not isinstance(items, list):
        return []
    cites: list[dict[str, str]] = []
    for it in items[:5]:
        if not isinstance(it, dict):
            continue
        url = (it.get("url") or "").strip()
        if not url:
            continue
        title = (it.get("title") or "").strip() or url
        cites.append({"title": title, "url": url})
    return cites


def format_turn_time() -> str | None:
    """本轮时间元信息文本，追加到 messages 末尾而不进 system prompt。

    进 system prompt 的分钟级时间戳每分钟第一条消息就会击穿整个前缀 cache；
    改到 messages 尾部，只影响尾部一小段 tokens，system + tools + history
    保持字节稳定，DeepSeek 官方 prefix cache 命中率显著提升。

    文本极简化 + 精度降为"天"（T1+T2）：去掉冗余说明，只保留 [北京时间] +
    YYYY-MM-DD。同一天内所有 turn 的 context.time 字节完全相同，直接可命中
    prefix cache；模型仍能通过日期计算星期几，聊天场景下"精确到天"足够，
    真需要精确时刻可由 datetime_now 工具按需返回。
    """
    try:
        now = now_cst()
        return f"[北京时间] {now:%Y-%m-%d}"
    except Exception:  # noqa: BLE001
        return None
