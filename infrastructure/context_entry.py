"""
CONTEXT_ENTRY.md 管理器（产品文档 §上下文入口文件 / 开发文档 §6.19）。

固定四段（缺段视为空）：
  ## 系统状态      —— 动态计数（待处理矛盾数、pending、上次回顾、SOUL 版本）
  ## 阅读顺序      —— 1-6 步，前 3 步必读
  ## 近期变化摘要   —— 保留 7 天
  ## 待处理        —— 含 ### pending_soul_update 子段
- 记忆统计数字从 _index.md frontmatter 引用，不维护第二份副本
- pending 去重：同 type 相似度 > 0.85 视为重复，只更新 original_text/created_at
"""
from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from memory.naming import pending_id as make_pending_id
from infrastructure.timeutil import now_cst

SECTIONS = ["系统状态", "阅读顺序", "近期变化摘要", "待处理"]

READING_ORDER_DEFAULT = """1. SOUL_CORE → 核心人格（必读）
2. SOUL_STYLE → 当前风格（必读）
3. 意识提示 → 重要记忆关键词（必读）
4. 当前会话摘要 → 继续已有对话时加载（按需）
5. 记忆检索 → 根据用户输入触发三级联动（按需）
6. 工具描述 → 根据意图判断加载哪些（按需）"""


class ContextEntryManager:
    def __init__(self, data_dir: str | Path):
        self.path = Path(data_dir) / "CONTEXT_ENTRY.md"
        self._lock = threading.RLock()

    # ---- 分段解析 ---------------------------------------------------------
    def _read_sections(self) -> dict[str, str]:
        if not self.path.exists():
            return {s: "" for s in SECTIONS}
        text = self.path.read_text(encoding="utf-8")
        out = {s: "" for s in SECTIONS}
        cur = None
        buf: list[str] = []
        for ln in text.splitlines():
            m = re.match(r"^## (.+)$", ln.strip())
            if m and m.group(1).strip() in SECTIONS:
                if cur:
                    out[cur] = "\n".join(buf).strip()
                cur = m.group(1).strip()
                buf = []
            elif cur:
                buf.append(ln)
        if cur:
            out[cur] = "\n".join(buf).strip()
        return out

    def _write_sections(self, sections: dict[str, str]) -> None:
        parts = ["# Second Person Context Entry", ""]
        for s in SECTIONS:
            parts.append(f"## {s}")
            parts.append(sections.get(s, "").strip())
            parts.append("")
        self.path.write_text("\n".join(parts), encoding="utf-8")

    # ---- 应用 patch（FileWriter context_entry 处理器调用） ---------------
    def apply_patch(self, patch: dict[str, Any]) -> None:
        with self._lock:
            sections = self._read_sections()
            if "system_status" in patch:
                sections["系统状态"] = self._render_status(patch["system_status"])
            if "reading_order" in patch:
                sections["阅读顺序"] = patch["reading_order"]
            elif not sections["阅读顺序"]:
                sections["阅读顺序"] = READING_ORDER_DEFAULT
            if "recent_changes" in patch:
                sections["近期变化摘要"] = self._merge_recent(
                    sections["近期变化摘要"], patch["recent_changes"])
            self._write_sections(sections)

    @staticmethod
    def _render_status(status: dict) -> str:
        lines = []
        for k, v in status.items():
            lines.append(f"- {k}：{v}")
        return "\n".join(lines)

    def _merge_recent(self, existing: str, new_items: list[str]) -> str:
        """合并近期变化，清理超 7 天条目（条目形如 - [7/15] ...）。"""
        all_lines = [l for l in existing.splitlines() if l.strip()]
        for item in new_items:
            all_lines.append(item if item.startswith("- ") else f"- {item}")
        return "\n".join(all_lines[-50:])  # 保留最近 50 条，日期清理由定时任务负责

    # ---- 意识提示（第 0 层） ----------------------------------------------
    def set_consciousness_hint(self, keywords: list[str]) -> None:
        with self._lock:
            sections = self._read_sections()
            status = sections["系统状态"]
            hint = "意识提示：" + "、".join(keywords[:30])
            # 覆盖已有意识提示行
            lines = [l for l in status.splitlines() if "意识提示" not in l]
            lines.append(f"- {hint}")
            sections["系统状态"] = "\n".join(lines)
            self._write_sections(sections)

    def set_consciousness_hint_raw(self, raw_text: str) -> None:
        """按整串写入意识提示（约束句用；分隔，不做关键词切分）。
        与 set_consciousness_hint 区别：后者按列表拼接会切断含顿号的约束句。"""
        with self._lock:
            sections = self._read_sections()
            status = sections["系统状态"]
            hint = "意识提示：" + raw_text
            lines = [l for l in status.splitlines() if "意识提示" not in l]
            lines.append(f"- {hint}")
            sections["系统状态"] = "\n".join(lines)
            self._write_sections(sections)

    def read_consciousness_hint(self) -> str:
        sections = self._read_sections()
        for ln in sections["系统状态"].splitlines():
            if "意识提示" in ln:
                return ln.split("意识提示：", 1)[-1].strip()
        return ""

    # ---- pending_soul_update 管理 ----------------------------------------
    def list_pending(self) -> list[dict[str, Any]]:
        sections = self._read_sections()
        return self._parse_pending(sections["待处理"])

    @staticmethod
    def _parse_pending(pending_section: str) -> list[dict[str, Any]]:
        # 提取 ### pending_soul_update 下的 YAML 列表
        m = re.search(r"### pending_soul_update\s*\n(.*)$",
                      pending_section, flags=re.S)
        if not m:
            return []
        body = m.group(1).strip()
        if not body:
            return []
        try:
            data = yaml.safe_load(body)
            return data if isinstance(data, list) else []
        except yaml.YAMLError:
            return []

    def _write_pending(self, items: list[dict[str, Any]]) -> None:
        sections = self._read_sections()
        if items:
            body = yaml.safe_dump(items, allow_unicode=True, sort_keys=False)
            sections["待处理"] = f"### pending_soul_update\n{body}"
        else:
            sections["待处理"] = ""
        self._write_sections(sections)

    def add_pending(self, ptype: str, original_text: str, proposed_change: str,
                    similarity_fn=None) -> str:
        """新增 pending；同 type 相似度 > 0.85 视为重复只更新。type 枚举：tone/behavior。"""
        if ptype not in ("tone", "behavior"):
            ptype = "tone"  # 非法枚举归入 tone
        with self._lock:
            items = self.list_pending()
            for it in items:
                if it.get("type") == ptype and similarity_fn and \
                        similarity_fn(it.get("original_text", ""), original_text) > 0.85:
                    it["original_text"] = original_text
                    it["created_at"] = now_cst().isoformat(timespec="seconds")
                    self._write_pending(items)
                    return it["id"]
            pid = make_pending_id()
            items.append({
                "id": pid, "type": ptype, "original_text": original_text,
                "proposed_change": proposed_change,
                "created_at": now_cst().isoformat(timespec="seconds"),
            })
            self._write_pending(items)
            return pid

    def remove_pending(self, pending_id: str) -> dict | None:
        with self._lock:
            items = self.list_pending()
            removed = None
            kept = []
            for it in items:
                if it.get("id") == pending_id:
                    removed = it
                else:
                    kept.append(it)
            self._write_pending(kept)
            return removed

    def purge_old_recent_changes(self, days: int = 7) -> None:
        """清理近期变化摘要中超过 N 天的条目（定时任务调用）。"""
        with self._lock:
            sections = self._read_sections()
            cutoff = now_cst() - timedelta(days=days)
            kept = []
            for ln in sections["近期变化摘要"].splitlines():
                m = re.search(r"\[(\d{1,2})/(\d{1,2})\]", ln)
                if m:
                    month, day = int(m.group(1)), int(m.group(2))
                    year = now_cst().year
                    try:
                        d = datetime(year, month, day)
                        if d < cutoff:
                            continue
                    except ValueError:
                        pass
                kept.append(ln)
            sections["近期变化摘要"] = "\n".join(kept)
            self._write_sections(sections)
