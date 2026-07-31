"""
Lifecycle —— 生命周期五态流转（产品文档 §生命周期流转规则）。

五态：active / stable / stale / archived / missing；lifecycle 与 confidence 独立。
- active → stable：confidence=strong 且 access_count >= important_upgrade_count，
  第 8 步 access_count 更新时检查，同时 is_important=1
- active → stale：last_accessed 超 stale_detection_days（Lint 过期检测）
- stable 不因命中变回 active；stable → active 仅用户手动/点踩(→stale)
- stale → active：被检索命中即恢复（user_marked_stale=1 除外）；或手动恢复
- 点踩"过时"复合操作（4 动作原子）：lifecycle→stale + confidence 降一级 +
  is_important=0 + user_marked_stale=1；若已 stale/archived 则跳过全部动作
所有 frontmatter 变更走 FileWriter（memory update），保持单写者。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from .md_file import parse_memory_md

CONFIDENCE_ORDER = ["low", "medium", "strong"]  # disputed 不在降级链


def _downgrade_confidence(c: str) -> str:
    if c not in CONFIDENCE_ORDER:
        return c  # disputed 不降
    idx = CONFIDENCE_ORDER.index(c)
    return CONFIDENCE_ORDER[max(0, idx - 1)]


def _row_flag(row, key: str) -> bool:
    """sqlite3.Row 安全取布尔字段（列不存在/NULL 时返 False）。"""
    try:
        return bool(row[key])
    except (IndexError, KeyError):
        return False


class LifecycleManager:
    def __init__(self, db, palace, file_writer, data_dir, config):
        self.db = db
        self.palace = palace
        self.fw = file_writer
        self.data_dir = Path(data_dir)
        self.config = config

    def _load_doc(self, mid: str):
        row = self.palace.get(mid)
        if not row:
            return None, None
        f = self.data_dir / row["md_path"]
        if not f.exists():
            return row, None
        return row, parse_memory_md(f.read_text(encoding="utf-8"))

    async def _submit_update(self, doc, reason: str = "") -> None:
        await self.fw.submit("memory", {
            "op": "update", "memory_id": doc.frontmatter["id"],
            "frontmatter": doc.frontmatter, "summary": doc.summary,
            "detail": doc.detail, "change_history": doc.change_history,
            "links": doc.links, "entities": doc.entities, "reason": reason,
        })

    # ---- active → stable（第 8 步 access_count 更新时检查） ---------------
    async def check_stable_upgrade(self, mid: str) -> bool:
        row, doc = self._load_doc(mid)
        if not row or not doc:
            return False
        threshold = self.config.get("important_upgrade_count", 3)
        if (row["confidence"] == "strong" and row["lifecycle"] == "active"
                and row["access_count"] >= threshold):
            doc.frontmatter["lifecycle"] = "stable"
            # 守卫：用户手动移出过重要目录的记忆，升 stable 不再自动置回
            if not _row_flag(row, "user_cleared_important"):
                doc.frontmatter["is_important"] = True
            doc.change_history.insert(
                0, f"[{datetime.now():%Y-%m-%d}] access_count 达 {row['access_count']}，"
                f"lifecycle: active → stable")
            await self._submit_update(doc, "stable 升级")
            return True
        return False

    # ---- stale → active（检索命中恢复） -----------------------------------
    async def recover_on_hit(self, mid: str) -> bool:
        row, doc = self._load_doc(mid)
        if not row or not doc:
            return False
        if row["lifecycle"] == "stale" and not row["user_marked_stale"]:
            doc.frontmatter["lifecycle"] = "active"
            doc.change_history.insert(
                0, f"[{datetime.now():%Y-%m-%d}] 检索命中，stale → active")
            # 自动归档周期计数清零（已恢复活跃）
            self.db.execute(
                "UPDATE memories SET stale_lint_runs=0 WHERE id=?", (mid,))
            await self._submit_update(doc, "stale 恢复")
            return True
        return False

    # ---- 点踩"过时"复合操作（4 动作原子） ---------------------------------
    async def downvote_stale(self, mid: str) -> bool:
        row, doc = self._load_doc(mid)
        if not row or not doc:
            return False
        if row["lifecycle"] in ("stale", "archived"):
            # 跳过全部四个动作，仅记录反馈进入被动回顾输入源（由调用方处理）
            return False
        fm = doc.frontmatter
        fm["lifecycle"] = "stale"
        fm["confidence"] = _downgrade_confidence(row["confidence"])
        fm["is_important"] = False
        fm["user_marked_stale"] = True
        doc.change_history.insert(
            0, f"[{datetime.now():%Y-%m-%d}] 用户点踩'记忆过时'：stale + 降置信 + 移出重要")
        await self._submit_update(doc, "点踩过时复合操作")
        return True

    # ---- 点赞升级：引用该记忆的回复被点赞 → medium → strong -----------
    async def upvote_upgrade(self, mid: str) -> bool:
        row, doc = self._load_doc(mid)
        if not row or not doc or row["confidence"] != "medium":
            return False
        doc.frontmatter["confidence"] = "strong"
        doc.change_history.insert(
            0, f"[{datetime.now():%Y-%m-%d}] 用户点赞引用回复，confidence: medium → strong")
        await self._submit_update(doc, "点赞升级")
        return True

    # ---- low 待确认：用户在对话中明确认可 → low → medium ----------------
    async def confirm_low(self, mid: str, confirmed: bool) -> bool:
        row, doc = self._load_doc(mid)
        if not row or not doc:
            return False
        now = datetime.now()
        if confirmed and row["confidence"] == "low":
            doc.frontmatter["confidence"] = "medium"
            doc.change_history.insert(
                0, f"[{now:%Y-%m-%d}] 用户在对话中确认推断属实，confidence: low → medium")
            await self._submit_update(doc, "low 确认升级")
            return True
        if not confirmed:
            doc.change_history.insert(
                0, f"[{now:%Y-%m-%d}] 用户否认该推断，保持 low 待后续处理")
            await self._submit_update(doc, "low 确认被否认")
        return False

    def next_low_confirm_candidate(self) -> dict | None:
        """取一条待对话确认的 low 记忆（超 30 天未确认且 7 天内未问过）。"""
        created_cutoff = (datetime.now() - timedelta(days=30)
                          ).isoformat(timespec="seconds")
        asked_cutoff = (datetime.now() - timedelta(days=7)
                        ).isoformat(timespec="seconds")
        row = self.db.query_one(
            "SELECT id, title, summary FROM memories WHERE confidence='low' "
            "AND lifecycle IN ('active','stable','stale') "
            "AND (created_at < ? OR created_at IS NULL) "
            "AND (low_confirm_asked_at IS NULL OR low_confirm_asked_at < ?) "
            "ORDER BY created_at LIMIT 1", (created_cutoff, asked_cutoff))
        return dict(row) if row else None

    def mark_low_confirm_asked(self, mid: str) -> None:
        self.db.execute(
            "UPDATE memories SET low_confirm_asked_at=? WHERE id=?",
            (datetime.now().isoformat(timespec="seconds"), mid))

    # ---- active → stale（Lint 过期检测批量） ------------------------------
    def detect_stale_candidates(self) -> list[str]:
        days = self.config.get("stale_detection_days", 90)
        cutoff = (datetime.now() - timedelta(days=days)
                  ).isoformat(timespec="seconds")
        rows = self.db.query_all(
            "SELECT id FROM memories WHERE lifecycle='active' "
            "AND last_accessed IS NOT NULL AND last_accessed < ?", (cutoff,))
        return [r["id"] for r in rows]

    async def mark_stale(self, mid: str) -> None:
        row, doc = self._load_doc(mid)
        if not row or not doc or row["lifecycle"] != "active":
            return
        doc.frontmatter["lifecycle"] = "stale"
        doc.change_history.insert(
            0, f"[{datetime.now():%Y-%m-%d}] 过期检测：active → stale")
        await self._submit_update(doc, "过期降级")

    # ---- 频次更新（第 8 步）：last_accessed / access_count / implicit -----
    def update_access_stats(self, loaded_ids: list[str], cited_ids: list[str]) -> None:
        """批量更新频次；走索引表直接更新（frontmatter 由后续 update 同步）。"""
        now = datetime.now().isoformat(timespec="seconds")
        for mid in loaded_ids:
            self.db.execute(
                "UPDATE memories SET last_accessed=? WHERE id=?", (now, mid))
        for mid in cited_ids:
            self.db.execute(
                "UPDATE memories SET access_count=access_count+1 WHERE id=?", (mid,))
        # implicit：加载了但未引用的，累计 3 转 1 次 access_count
        implicit = [m for m in loaded_ids if m not in cited_ids]
        for mid in implicit:
            row = self.db.query_one(
                "SELECT implicit_use_count FROM memories WHERE id=?", (mid,))
            if not row:
                continue
            new_cnt = (row["implicit_use_count"] or 0) + 1
            if new_cnt >= 3:
                self.db.execute(
                    "UPDATE memories SET implicit_use_count=0, "
                    "access_count=access_count+1 WHERE id=?", (mid,))
            else:
                self.db.execute(
                    "UPDATE memories SET implicit_use_count=? WHERE id=?", (new_cnt, mid))

    # ---- 引用明细（第 8 步）：记忆/知识库统一引用溯源 -------------------
    def record_citations(self, cited_ids: list[str], message_id: int,
                         session_id: str) -> None:
        """引用事件落表：记录被哪条消息/会话引用；源自知识库文档的记忆
        同步回溯 doc_id，使文档侧也能展示被引用记录。"""
        if not cited_ids:
            return
        now = datetime.now().isoformat(timespec="seconds")
        for mid in cited_ids:
            doc = self.db.query_one(
                "SELECT id FROM raw_docs WHERE extracted_memory_ids LIKE ?",
                (f'%"{mid}"%',))
            self.db.execute(
                "INSERT INTO citation_events(memory_id,doc_id,message_id,"
                "session_id,cited_at) VALUES(?,?,?,?,?)",
                (mid, doc["id"] if doc else None, message_id, session_id, now))
