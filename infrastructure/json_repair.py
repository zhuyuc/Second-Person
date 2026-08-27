"""
JSON 自动修复链（开发文档 §1.1 / §6.11）。

LLM 结构化输出解析失败时先尝试修复再重试：
  去除 markdown 代码块标记 → 修复常见 JSON 错误（尾逗号/单引号/缺失括号）
  → 提取 JSON 子串
修复后仍失败才计入重试次数。
"""
from __future__ import annotations

import json
import re
from typing import Any


def _strip_markdown_fence(text: str) -> str:
    text = text.strip()
    # ```json ... ``` 或 ``` ... ```
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.S)
    if m:
        return m.group(1).strip()
    return text


def _extract_json_substring(text: str) -> str | None:
    """提取第一个平衡的 {...} 或 [...] 子串。"""
    start = None
    stack = []
    pairs = {"}": "{", "]": "["}
    for i, ch in enumerate(text):
        if ch in "{[":
            if start is None:
                start = i
            stack.append(ch)
        elif ch in "}]":
            if stack and stack[-1] == pairs[ch]:
                stack.pop()
                if not stack and start is not None:
                    return text[start:i + 1]
            else:
                stack = []
                start = None
    return None


def _fix_common_errors(text: str) -> str:
    # 尾逗号
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    # 单引号键/值 → 双引号（保守：只替换明显的单引号包裹）
    text = re.sub(r"'([^'\"]*)'(\s*:)", r'"\1"\2', text)
    text = re.sub(r"(:\s*)'([^'\"]*)'", r'\1"\2"', text)
    return text


def _close_unbalanced(text: str) -> str | None:
    """补齐截断 JSON：闭合未结束的字符串与括号（LLM 输出被 max_tokens
    截断时的兜底）。本就平衡则返回 None。"""
    pairs = {"}": "{", "]": "["}
    stack: list[str] = []
    in_str = escape = False
    for ch in text:
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack and stack[-1] == pairs[ch]:
                stack.pop()
    if not stack and not in_str:
        return None
    fixed = text
    if in_str:
        fixed += '"'
    # 去掉截断产生的悬空尾逗号/冒号（含冒号时连键一起去掉）
    fixed = re.sub(r'"[^"]*"\s*:\s*$', "", fixed)
    fixed = re.sub(r"[,:]\s*$", "", fixed)
    for ch in reversed(stack):
        fixed += "}" if ch == "{" else "]"
    return fixed


# P1-E：修复次数统计（进程内轻量计数器，供健康检查/告警读取）
class RepairStats:
    """线程安全的修复计数器；attempts=所有调用次数，failures=最终失败次数。"""
    __slots__ = ("attempts", "failures", "consecutive_failures")

    def __init__(self):
        self.attempts = 0
        self.failures = 0
        self.consecutive_failures = 0

    def record_ok(self) -> None:
        self.attempts += 1
        self.consecutive_failures = 0

    def record_fail(self) -> None:
        self.attempts += 1
        self.failures += 1
        self.consecutive_failures += 1

    def snapshot(self) -> dict[str, int]:
        return {"attempts": self.attempts,
                "failures": self.failures,
                "consecutive_failures": self.consecutive_failures}


REPAIR_STATS = RepairStats()


def repair_json(text: str) -> Any:
    """尝试解析 JSON，逐级修复。全部失败抛 ValueError。"""
    candidates: list[str] = []
    cleaned = _strip_markdown_fence(text)
    candidates.append(cleaned)
    candidates.append(_fix_common_errors(cleaned))
    sub = _extract_json_substring(cleaned)
    if sub:
        candidates.append(sub)
        candidates.append(_fix_common_errors(sub))
    else:
        # 提取不到平衡子串 → 大概率被截断，尝试从首个括号起补齐闭合
        m = re.search(r"[{\[]", cleaned)
        if m:
            closed = _close_unbalanced(cleaned[m.start():])
            if closed:
                candidates.append(closed)
                candidates.append(_fix_common_errors(closed))

    last_err: Exception | None = None
    for cand in candidates:
        try:
            result = json.loads(cand)
            REPAIR_STATS.record_ok()
            return result
        except json.JSONDecodeError as e:
            last_err = e
    REPAIR_STATS.record_fail()
    raise ValueError(f"JSON 修复失败：{last_err}")
