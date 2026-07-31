"""
SOUL 注入防护扫描（开发文档 §6.3 SOUL 文件的注入防护）。

正则匹配越权指令模式（大小写不敏感，中英文各一套）：
  ignore (all )?previous instructions / disregard .* (rules|instructions) /
  you are now / system: 前缀 / 声明新角色或解除限制的句式
命中则拒绝加载该版本。
"""
from __future__ import annotations

import re

_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.I),
    re.compile(r"disregard\s+.*(rules|instructions|prompt)", re.I),
    re.compile(r"you\s+are\s+now\b", re.I),
    re.compile(r"^\s*system\s*:", re.I | re.M),
    re.compile(r"(forget|override)\s+.*(rules|instructions|persona|soul)", re.I),
    re.compile(r"忽略(之前|以上|前面).*(指令|规则|设定)"),
    re.compile(r"(现在|从现在起)你(是|将是|扮演)"),
    re.compile(r"解除.*(限制|约束|规则)"),
]


def scan_injection(text: str) -> str | None:
    """返回命中的模式说明，或 None 表示通过。"""
    for pat in _INJECTION_PATTERNS:
        m = pat.search(text)
        if m:
            return pat.pattern
    return None
