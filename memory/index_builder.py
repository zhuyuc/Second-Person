"""
IndexBuilder —— _index.md 全局记忆目录的生成（产品文档 §记忆存储 / 开发文档 §6.16）。

- 记忆总数 ≤ 2000：全量重建（毫秒级）
- 超过 2000：增量更新——只重写发生变化（dirty）的 domain 分组段落与 frontmatter 统计，
  其余段落原样保留，用 `## {domain} (N)` 标题行做定位锚点；domain 被清空则删除该段
- 频率上限：最多每 10 秒重建一次（此处节流 + FileWriter 的 index 请求合并共同保证）
- 重要记忆 section 是 WHERE is_important=1 的渲染快照
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from .md_file import dump_frontmatter_doc, split_frontmatter

FULL_REBUILD_LIMIT = 2000
THROTTLE_SECONDS = 10.0
IMPORTANT_HEADING = "重要记忆"


class IndexBuilder:
    def __init__(self, db, palace, data_dir):
        self.db = db
        self.palace = palace
        self.data_dir = data_dir
        self._last_build = 0.0
        self._dirty: set[str] = set()          # 变化的 domain 集合（增量用）
        self._all_dirty = True                 # 首次或结构性变化时全量

    # FileWriter memory 处理器在写入后调用，标记受影响 domain
    def mark_dirty(self, domain: str | None) -> None:
        if domain:
            self._dirty.add(domain)

    def _index_path(self) -> Path:
        return Path(self.data_dir) / "memories" / "_index.md"

    def rebuild(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_build < THROTTLE_SECONDS:
            return
        self._last_build = now
        stats = self.palace.stats()
        if stats["total"] <= FULL_REBUILD_LIMIT or self._all_dirty \
                or not self._index_path().exists():
            self._full_rebuild(stats)
            self._all_dirty = False
        else:
            self._incremental_rebuild(stats)
        self._dirty.clear()

    # ---- 渲染片段（全量/增量共用） ---------------------------------------
    def _rows_by_domain(self) -> dict[str, list]:
        rows = self.db.query_all(
            "SELECT id,title,domain,confidence,lifecycle,is_important FROM memories "
            "WHERE lifecycle IN ('active','stable','stale') ORDER BY domain, id")
        by_domain: dict[str, list] = {}
        for r in rows:
            by_domain.setdefault(r["domain"], []).append(r)
        return by_domain

    def _render_important(self) -> list[str]:
        rows = self.db.query_all(
            "SELECT id,title FROM memories WHERE is_important=1 "
            "AND lifecycle IN ('active','stable','stale') ORDER BY id")
        if not rows:
            return []
        lines = [f"## {IMPORTANT_HEADING}"]
        lines += [f"- [[{r['id']}]] | {r['title']}" for r in rows]
        lines.append("")
        return lines

    @staticmethod
    def _render_domain_segment(domain: str, items: list) -> list[str]:
        lines = [f"## {domain} ({len(items)})"]
        for r in items:
            lines.append(
                f"- [[{r['id']}]] | {r['title']} | {r['confidence']} | {r['lifecycle']}")
        lines.append("")
        return lines

    def _frontmatter(self, stats: dict) -> dict:
        return {
            "total": stats["total"], "active": stats["total_active"],
            "stable": stats["total_stable"], "stale": stats["total_stale"],
            "archived": stats["total_archived"], "important": stats["important_count"],
            "link_count": stats["link_count"], "md_schema_version": 1,
        }

    def _full_rebuild(self, stats: dict) -> None:
        by_domain = self._rows_by_domain()
        lines = self._render_important()
        for domain in sorted(by_domain):
            lines += self._render_domain_segment(domain, by_domain[domain])
        out = dump_frontmatter_doc(self._frontmatter(stats), "\n".join(lines))
        self._index_path().write_text(out, encoding="utf-8")

    # ---- 增量：只重写 dirty domain 段 + frontmatter -----------------------
    def _incremental_rebuild(self, stats: dict) -> None:
        fm_old, body = split_frontmatter(
            self._index_path().read_text(encoding="utf-8"))
        # OrderedDict: heading_key -> segment_text
        segments = self._parse_segments(body)

        by_domain = self._rows_by_domain()
        current_domains = set(by_domain)

        # 1) 重要记忆段始终重渲染（体量小）
        important_lines = self._render_important()
        if important_lines:
            segments[IMPORTANT_HEADING] = "\n".join(important_lines)
        else:
            segments.pop(IMPORTANT_HEADING, None)

        # 2) 只重写 dirty domain 段
        for domain in self._dirty:
            if domain in current_domains:
                segments[domain] = "\n".join(
                    self._render_domain_segment(domain, by_domain[domain]))
            else:
                segments.pop(domain, None)   # domain 被清空 → 删除该段

        # 3) 删除已不存在的 domain 段（防止残留）
        for key in list(segments.keys()):
            if key != IMPORTANT_HEADING and key not in current_domains:
                segments.pop(key, None)

        # 4) 组装：重要记忆在前，domain 按字典序
        ordered = []
        if IMPORTANT_HEADING in segments:
            ordered.append(segments[IMPORTANT_HEADING])
        for domain in sorted(k for k in segments if k != IMPORTANT_HEADING):
            ordered.append(segments[domain])
        out = dump_frontmatter_doc(
            self._frontmatter(stats), "\n".join(ordered))
        self._index_path().write_text(out, encoding="utf-8")

    @staticmethod
    def _parse_segments(body: str) -> "dict[str, str]":
        """把正文按 `## {heading}` 切成段。heading 为 domain 名或 '重要记忆'。"""
        from collections import OrderedDict
        segments: "OrderedDict[str, str]" = OrderedDict()
        cur_key = None
        buf: list[str] = []
        for ln in body.splitlines():
            m = re.match(r"^## (.+)$", ln)
            if m:
                if cur_key is not None:
                    segments[cur_key] = "\n".join(buf).rstrip() + "\n"
                raw = m.group(1).strip()
                # `## {domain} (N)` → 取 domain；`## 重要记忆` → 原样
                dm = re.match(r"^(.*?)\s*\(\d+\)$", raw)
                cur_key = dm.group(1).strip() if dm else raw
                buf = [ln]
            elif cur_key is not None:
                buf.append(ln)
        if cur_key is not None:
            segments[cur_key] = "\n".join(buf).rstrip() + "\n"
        return segments

    def important_keywords(self, limit: int = 30) -> list[str]:
        """第 0 层意识提示的关键词列表（从重要记忆目录视图提炼）。"""
        rows = self.db.query_all(
            "SELECT title FROM memories WHERE is_important=1 "
            "ORDER BY access_count DESC LIMIT ?", (limit,))
        return [r["title"] for r in rows]
