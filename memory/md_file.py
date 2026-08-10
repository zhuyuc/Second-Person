"""
md 文件的机读结构解析与序列化（开发文档 §6.19 + §记忆存储）。

记忆 md 文件结构：
  ---
  frontmatter (YAML): id/title/domain/confidence/lifecycle/source_type/
                      access_count/last_accessed/created_at/updated_at/
                      source_conversation/links/entities/is_important/
                      confidence_before_dispute/user_marked_stale
  ---
  ## 摘要
  <summary>
  ## 详情
  <detail>（观点演变时分层：当前观点 [日期起] / 历史观点 [日期]）
  ## 变更历史
  - [2026-07-10] ...
  - [2026-06-20] 首次创建

同一套解析同时用于 conflict_XXX.md / user_profile.md 的通用 frontmatter 分离。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml

_DELIM = "---"


@dataclass
class MemoryDoc:
    frontmatter: dict[str, Any]
    summary: str = ""
    detail: str = ""
    change_history: list[str] = field(default_factory=list)

    # ---- 便捷访问 ----
    @property
    def id(self) -> str:
        return self.frontmatter.get("id", "")

    @property
    def title(self) -> str:
        return self.frontmatter.get("title", "")

    @property
    def domain(self) -> str:
        return self.frontmatter.get("domain", "general")

    @property
    def links(self) -> list[dict[str, str]]:
        raw = self.frontmatter.get("links", []) or []
        # 防御脏数据：历史残留/LLM 产出可能含非 dict 元素（纯字符串），过滤而非崩溃
        return [l for l in raw if isinstance(l, dict)]

    @property
    def entities(self) -> list[str]:
        return self.frontmatter.get("entities", []) or []


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """分离 YAML frontmatter 与正文。无 frontmatter 时返回 ({}, text)。"""
    text = text.lstrip("\ufeff")
    if not text.startswith(_DELIM):
        return {}, text
    parts = text.split(_DELIM, 2)
    if len(parts) < 3:
        return {}, text
    fm = yaml.safe_load(parts[1]) or {}
    if not isinstance(fm, dict):
        fm = {}
    return fm, parts[2].lstrip("\n")


# 结构段标题：只有这三个二级标题是文档分段边界；正文内的其他 ## 行
# （如观点演变的“当前观点/历史观点”历史数据、导入知识自带的 markdown
# 标题）属于段内容，不得截断——否则详情会被解析为空并在后续回写中丢失。
_SECTION_HEADINGS = {"摘要", "详情", "变更历史"}


def _extract_section(body: str, heading: str) -> str:
    """提取 ## heading 到下一个结构段标题之间的内容。"""
    lines = body.splitlines()
    out: list[str] = []
    capturing = False
    for ln in lines:
        stripped = ln.strip()
        if stripped.startswith("## ") and stripped[3:].strip() in _SECTION_HEADINGS:
            if capturing:
                break
            capturing = stripped[3:].strip() == heading
            continue
        if capturing:
            out.append(ln)
    return "\n".join(out).strip()


def parse_memory_md(text: str) -> MemoryDoc:
    fm, body = split_frontmatter(text)
    summary = _extract_section(body, "摘要")
    detail = _extract_section(body, "详情")
    hist_raw = _extract_section(body, "变更历史")
    history = [ln.strip()[2:].strip() if ln.strip().startswith("- ") else ln.strip()
               for ln in hist_raw.splitlines() if ln.strip()]
    return MemoryDoc(frontmatter=fm, summary=summary, detail=detail,
                     change_history=history)


def serialize_memory_md(doc: MemoryDoc) -> str:
    fm_yaml = yaml.safe_dump(
        doc.frontmatter, allow_unicode=True, sort_keys=False).strip()
    parts = [f"{_DELIM}\n{fm_yaml}\n{_DELIM}", ""]
    parts.append(f"## 摘要\n{doc.summary}".rstrip())
    parts.append(f"\n## 详情\n{doc.detail}".rstrip())
    if doc.change_history:
        hist = "\n".join(f"- {h}" for h in doc.change_history)
        parts.append(f"\n## 变更历史\n{hist}")
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# 通用 frontmatter 文档（user_profile.md / _index.md 等）
# ---------------------------------------------------------------------------
def dump_frontmatter_doc(frontmatter: dict[str, Any], body: str) -> str:
    fm_yaml = yaml.safe_dump(
        frontmatter, allow_unicode=True, sort_keys=False).strip()
    return f"{_DELIM}\n{fm_yaml}\n{_DELIM}\n{body}"
