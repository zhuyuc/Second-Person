"""工具过滤（开发文档 §MCP 工具刷新）：include 白名单 / exclude 黑名单。"""
from __future__ import annotations


def apply_tool_filter(tools: list[dict], tools_filter: dict | None) -> list[dict]:
    if not tools_filter:
        return tools
    include = set(tools_filter.get("include") or [])
    exclude = set(tools_filter.get("exclude") or [])
    out = []
    for t in tools:
        name = t.get("name", "")
        if include and name not in include:
            continue
        if name in exclude:
            continue
        out.append(t)
    return out
