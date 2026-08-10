"""
ConflictDetector —— 矛盾检测与裁决（产品文档 §矛盾陈述 / §矛盾裁决 / 开发文档 §2.11）。

- 矛盾陈述（主题相同但内容冲突且非明确演变）：不合并，保留两条独立记忆，
  之间建 contradicts 引用，两条 confidence 均降 disputed（降级前把原值写入
  frontmatter.confidence_before_dispute），生成 _conflicts/conflict_XXX.md，
  下次对话由 AI 主动告知用户裁决
- 裁决 resolve：keep_a / keep_b / keep_both / delete_both
  keep_both：confidence 从 disputed 恢复为 confidence_before_dispute（无则 medium），
             contradicts 替换为 related
  删除类：按记忆删除引用清理规则（走 FileWriter memory delete）
- conflict 文件 status→resolved 追加处理结果，30 天后自动删除
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from infrastructure.timeutil import now_cst
from pathlib import Path


from .md_file import dump_frontmatter_doc, parse_memory_md, split_frontmatter


class ConflictDetector:
    def __init__(self, db, palace, file_writer, linker, data_dir):
        self.db = db
        self.palace = palace
        self.fw = file_writer
        self.linker = linker
        self.data_dir = Path(data_dir)
        self.conflicts_dir = self.data_dir / "memories" / "_conflicts"

    def _load_doc(self, mid: str):
        row = self.palace.get(mid)
        if not row:
            return None, None
        f = self.data_dir / row["md_path"]
        if not f.exists():
            return row, None
        return row, parse_memory_md(f.read_text(encoding="utf-8"))

    # ---- 检测并标记矛盾 ---------------------------------------------------
    async def mark_conflict(self, mid_a: str, mid_b: str, title: str) -> str:
        """把两条记忆标为 disputed，建 contradicts，生成 conflict 文件。"""
        conflict_id = f"conflict_{uuid.uuid4().hex[:8]}"
        for mid in (mid_a, mid_b):
            row, doc = self._load_doc(mid)
            if not doc:
                continue
            if row["confidence"] != "disputed":
                doc.frontmatter["confidence_before_dispute"] = row["confidence"]
            doc.frontmatter["confidence"] = "disputed"
            await self.fw.submit("memory", {
                "op": "update", "memory_id": mid, "frontmatter": doc.frontmatter,
                "summary": doc.summary, "detail": doc.detail,
                "change_history": doc.change_history, "links": doc.links,
                "entities": doc.entities, "reason": "矛盾检测：置 disputed"})
        await self.linker.add_link(mid_a, mid_b, "contradicts")

        self.conflicts_dir.mkdir(parents=True, exist_ok=True)
        row_a, doc_a = self._load_doc(mid_a)
        row_b, doc_b = self._load_doc(mid_b)
        fm = {"conflict_id": conflict_id, "status": "pending",
              "detected_at": now_cst().strftime("%Y-%m-%d"),
              "detected_by": "conflict_detector"}
        body = (f"## 来源 A\n- 记忆：[[{mid_a}]]\n- 内容：{doc_a.summary if doc_a else ''}\n\n"
                f"## 来源 B\n- 记忆：[[{mid_b}]]\n- 内容：{doc_b.summary if doc_b else ''}\n\n"
                f"## 处理结果\n")
        (self.conflicts_dir / f"{conflict_id}.md").write_text(
            dump_frontmatter_doc(fm, body), encoding="utf-8")
        return conflict_id

    # ---- 待处理矛盾列表 ---------------------------------------------------
    def list_pending(self) -> list[dict]:
        out = []
        if not self.conflicts_dir.exists():
            return out
        for f in self.conflicts_dir.glob("conflict_*.md"):
            fm, body = split_frontmatter(f.read_text(encoding="utf-8"))
            if fm.get("status") != "pending":
                continue
            out.append(self._parse_conflict(fm, body))
        return out

    def _parse_conflict(self, fm: dict, body: str) -> dict:
        import re
        sources = {}
        for label in ("A", "B"):
            m = re.search(
                rf"## 来源 {label}\n(.*?)(?=\n## |\Z)", body, flags=re.S)
            if m:
                seg = m.group(1)
                mid_m = re.search(r"\[\[(mem_\w+)\]\]", seg)
                content_m = re.search(r"内容：(.*)", seg)
                mid = mid_m.group(1) if mid_m else None
                sources[label] = {
                    "memory_id": mid,
                    "content": content_m.group(1).strip() if content_m else "",
                }
        # 用记忆标题生成有意义的中文标题
        title = fm.get("conflict_id")
        mid_a = sources.get("A", {}).get("memory_id")
        mid_b = sources.get("B", {}).get("memory_id")
        if mid_a or mid_b:
            titles = []
            for mid in (mid_a, mid_b):
                if mid:
                    row = self.palace.get(mid)
                    if row and row["title"]:
                        titles.append(row["title"])
            if titles:
                title = " vs ".join(titles) if len(titles) == 2 else titles[0]
        return {"conflict_id": fm.get("conflict_id"), "title": title,
                "detected_at": fm.get("detected_at"),
                "source_a": sources.get("A", {}), "source_b": sources.get("B", {})}

    # ---- 裁决 -------------------------------------------------------------
    async def resolve(self, conflict_id: str, resolution: str) -> None:
        f = self.conflicts_dir / f"{conflict_id}.md"
        if not f.exists():
            raise KeyError(conflict_id)
        fm, body = split_frontmatter(f.read_text(encoding="utf-8"))
        info = self._parse_conflict(fm, body)
        mid_a = info["source_a"].get("memory_id")
        mid_b = info["source_b"].get("memory_id")

        if resolution == "keep_a":
            await self._delete_memory(mid_b)
            await self._restore_confidence(mid_a)
        elif resolution == "keep_b":
            await self._delete_memory(mid_a)
            await self._restore_confidence(mid_b)
        elif resolution == "keep_both":
            await self._restore_confidence(mid_a)
            await self._restore_confidence(mid_b)
            # 替换 contradicts
            await self.linker.add_link(mid_a, mid_b, "related")
        elif resolution == "delete_both":
            await self._delete_memory(mid_a)
            await self._delete_memory(mid_b)
        else:
            raise ValueError(f"未知裁决：{resolution}")

        fm["status"] = "resolved"
        fm["resolved_at"] = now_cst().strftime("%Y-%m-%d")
        body += f"\n- 裁决：{resolution} @ {fm['resolved_at']}\n"
        f.write_text(dump_frontmatter_doc(fm, body), encoding="utf-8")

    async def _restore_confidence(self, mid: str | None) -> None:
        if not mid:
            return
        row, doc = self._load_doc(mid)
        if not doc:
            return
        before = doc.frontmatter.pop("confidence_before_dispute", "medium")
        doc.frontmatter["confidence"] = before or "medium"
        # 解除 contradicts 引用
        doc.frontmatter["links"] = [
            l for l in doc.links if l.get("type") != "contradicts"]
        await self.fw.submit("memory", {
            "op": "update", "memory_id": mid, "frontmatter": doc.frontmatter,
            "summary": doc.summary, "detail": doc.detail,
            "change_history": doc.change_history, "links": doc.frontmatter["links"],
            "entities": doc.entities, "reason": "矛盾裁决：恢复 confidence"})

    async def _delete_memory(self, mid: str | None) -> None:
        if not mid:
            return
        await self.fw.submit("memory", {"op": "delete", "memory_id": mid})

    def purge_resolved(self, days: int = 30) -> int:
        """清理 status=resolved 且超 30 天的矛盾文件（定时任务调用）。"""
        if not self.conflicts_dir.exists():
            return 0
        cutoff = (now_cst() - timedelta(days=days)).timestamp()
        removed = 0
        for f in self.conflicts_dir.glob("conflict_*.md"):
            fm, _ = split_frontmatter(f.read_text(encoding="utf-8"))
            if fm.get("status") == "resolved":
                ra = fm.get("resolved_at")
                try:
                    ts = datetime.strptime(ra, "%Y-%m-%d").timestamp()
                except (TypeError, ValueError):
                    continue
                if ts < cutoff:
                    f.unlink()
                    removed += 1
        return removed
