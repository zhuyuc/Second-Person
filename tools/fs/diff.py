"""Unified diff 生成（v5 §六 B10）。"""
from __future__ import annotations

import difflib

# 单卡片最大字符数：超过后返回摘要（首尾各 40 行）
DIFF_MAX_CHARS = 20_000


def unified_diff(before: str, after: str, path: str = "file") -> str:
    """标准 unified diff（`--- a/path\\n+++ b/path\\n@@ ...`）。"""
    before_lines = before.splitlines(keepends=True) if before else []
    after_lines = after.splitlines(keepends=True) if after else []
    diff = difflib.unified_diff(
        before_lines, after_lines,
        fromfile=f"a/{path}", tofile=f"b/{path}",
        n=3)
    text = "".join(diff)
    if len(text) > DIFF_MAX_CHARS:
        # 摘要模式：截首尾各 40 行，中间省略
        lines = text.splitlines()
        head = "\n".join(lines[:40])
        tail = "\n".join(lines[-40:])
        text = f"{head}\n... （diff 过长已截断，中间 {len(lines) - 80} 行省略）\n{tail}"
    return text


def summary_stats(before: str, after: str) -> dict:
    """粗略统计增删行数（供 diff 卡角标显示）。"""
    b_lines = before.splitlines()
    a_lines = after.splitlines()
    matcher = difflib.SequenceMatcher(a=b_lines, b=a_lines, autojunk=False)
    added = 0
    removed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "replace":
            removed += (i2 - i1)
            added += (j2 - j1)
        elif tag == "delete":
            removed += (i2 - i1)
        elif tag == "insert":
            added += (j2 - j1)
    return {"added": added, "removed": removed}
