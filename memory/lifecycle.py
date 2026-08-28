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

import json
import uuid
from datetime import timedelta
from pathlib import Path

from infrastructure.timeutil import now_cst

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
        }, wait=True)

    # ---- active → stable（第 8 步 access_count 更新时检查） ---------------
    async def check_stable_upgrade(self, mid: str) -> bool:
        row, doc = self._load_doc(mid)
        if not row or not doc:
            return False
        from . import _constants
        if (row["confidence"] == "strong" and row["lifecycle"] == "active"
                and row["access_count"] >= _constants.IMPORTANT_UPGRADE_COUNT):
            doc.frontmatter["lifecycle"] = "stable"
            # 守卫：用户手动移出过重要目录的记忆，升 stable 不再自动置回
            if not _row_flag(row, "user_cleared_important"):
                doc.frontmatter["is_important"] = True
            doc.change_history.insert(
                0, f"[{now_cst():%Y-%m-%d}] access_count 达 {row['access_count']}，"
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
            doc.frontmatter["freshness_state"] = "current"
            doc.change_history.insert(
                0, f"[{now_cst():%Y-%m-%d}] 检索命中，stale → active")
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
        fm["freshness_state"] = "expired"
        fm["confidence"] = _downgrade_confidence(row["confidence"])
        fm["is_important"] = False
        fm["user_marked_stale"] = True
        doc.change_history.insert(
            0, f"[{now_cst():%Y-%m-%d}] 用户点踩'记忆过时'：stale + 降置信 + 移出重要")
        await self._submit_update(doc, "点踩过时复合操作")
        return True

    # ---- 点赞升级：引用该记忆的回复被点赞 → medium → strong -----------
    async def upvote_upgrade(self, mid: str) -> bool:
        row, doc = self._load_doc(mid)
        if not row or not doc:
            return False
        score = min(10, float(row["usefulness_score"] or 0) + 1)
        doc.frontmatter["usefulness_score"] = score
        doc.change_history.insert(
            0, f"[{now_cst():%Y-%m-%d}] 用户点赞引用回复，使用价值 +1")
        await self._submit_update(doc, "点赞反馈")
        return True

    # ---- low 待确认：用户在对话中明确认可 → low → medium ----------------
    async def confirm_low(self, mid: str, confirmed: bool) -> bool:
        row, doc = self._load_doc(mid)
        if not row or not doc:
            return False
        now = now_cst()
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
        created_cutoff = (now_cst() - timedelta(days=30)
                          ).isoformat(timespec="seconds")
        asked_cutoff = (now_cst() - timedelta(days=7)
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
            (now_cst().isoformat(timespec="seconds"), mid))

    # ---- active → stale（Lint 过期检测批量） ------------------------------
    def detect_stale_candidates(self) -> list[str]:
        from . import _constants
        days = _constants.stale_days(self.config)
        cutoff = (now_cst() - timedelta(days=days)
                  ).isoformat(timespec="seconds")
        rows = self.db.query_all(
            "SELECT id FROM memories WHERE lifecycle='active' "
            "AND last_accessed IS NOT NULL AND last_accessed < ?", (cutoff,))
        return [r["id"] for r in rows]

    def detect_review_due(self) -> list[str]:
        """按记忆自身 review_after 到期，不把“被加载”误算为复核。"""
        today = now_cst().strftime("%Y-%m-%d")
        rows = self.db.query_all(
            "SELECT id FROM memories WHERE lifecycle IN ('active','stable','stale') "
            "AND COALESCE(freshness_state,'current')='current' "
            "AND review_after IS NOT NULL AND review_after <= ?", (today,))
        return [r["id"] for r in rows]

    async def mark_review_due(self, mid: str) -> bool:
        row, doc = self._load_doc(mid)
        if not row or not doc:
            return False
        if (row.get("freshness_state") or "current") != "current":
            return False
        doc.frontmatter["freshness_state"] = "review_due"
        doc.change_history.insert(
            0, f"[{now_cst():%Y-%m-%d}] 到达 review_after，标记待复核")
        await self._submit_update(doc, "记忆到期待复核")
        now = now_cst().isoformat(timespec="seconds")
        self.db.execute(
            "INSERT INTO memory_governance_items(item_id,item_type,primary_memory_id,"
            "priority,status,reason,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (f"gov_{uuid.uuid4().hex[:12]}", "freshness_review", mid,
             (row.get("access_count", 0) or 0) + 1, "open", "记忆到达复核时间",
             json.dumps({"review_after": row.get("review_after")}, ensure_ascii=False), now))
        return True

    async def mark_stale(self, mid: str) -> None:
        row, doc = self._load_doc(mid)
        if not row or not doc or row["lifecycle"] != "active":
            return
        doc.frontmatter["lifecycle"] = "stale"
        doc.frontmatter["freshness_state"] = "review_due"
        doc.change_history.insert(
            0, f"[{now_cst():%Y-%m-%d}] 过期检测：active → stale")
        await self._submit_update(doc, "过期降级")

    # ---- 频次更新（第 8 步）：last_accessed / access_count / implicit -----
    def update_access_stats(self, loaded_ids: list[str], cited_ids: list[str],
                            *, session_id: str | None = None) -> None:
        """批量更新频次；走索引表直接更新（frontmatter 由后续 update 同步）。
        全部改 executemany 批处理，避免逐条 execute 的 N+1。

        T2-A 跨会话去重：同一 (memory_id, session_id) 组合每天只计 1 次
        access_count，避免一条记忆在同一会话内被反复引用后 access_count 迅速
        爆表 → 升级 stable + is_important=1 → 检索加权，形成正反馈环。
        session_id 缺失（历史调用/后台链路）时退化为旧行为，仅去重 last_accessed。
        """
        if not cited_ids:
            return
        now = now_cst().isoformat(timespec="seconds")
        # last_accessed 无论如何都刷新（用于过期检测）
        self.db.executemany(
            "UPDATE memories SET last_accessed=? WHERE id=?",
            [(now, mid) for mid in cited_ids])

        counted_ids = list(cited_ids)
        if session_id:
            today = now[:10]
            # 幂等表：同 (memory_id, session_id, day) 计一次；不新建表，
            # 复用 citation_events 判定（该 memory 今日在该 session 已计过 → 跳过）
            filtered: list[str] = []
            for mid in cited_ids:
                already = self.db.query_one(
                    "SELECT 1 FROM citation_events "
                    "WHERE memory_id=? AND session_id=? AND SUBSTR(cited_at,1,10)=? "
                    "LIMIT 1", (mid, session_id, today))
                if not already:
                    filtered.append(mid)
            counted_ids = filtered
        if counted_ids:
            self.db.executemany(
                "UPDATE memories SET access_count=access_count+1 WHERE id=?",
                [(mid,) for mid in counted_ids])

    # ---- is_important 衰减：连续 N 天未被引用 → 清除重要标记 ------------
    def decay_is_important(self, days: int | None = None) -> int:
        """自动清除长期未被引用的 is_important 标记，避免早期误命中被永久放大。

        触发场景：夜间维护链调用一次。返回被清除数量。
        user_cleared_important 已置位的记忆维持原样。

        实现：只批量更新 SQLite 索引位（is_important=0），md frontmatter 在下次
        任何 memory update 时由 fix_index_drift/normal write 同步。避免此处
        触发 FileWriter 队列在同步/异步双路径下的 event loop 复杂度。
        """
        from . import _constants
        days = int(days if days is not None
                   else _constants.important_decay_days(self.config))
        cutoff = (now_cst() - timedelta(days=days)
                  ).isoformat(timespec="seconds")
        # user_cleared_important 已置位的不再改；同时用 last_accessed 做过滤
        cur = self.db.execute(
            "UPDATE memories SET is_important=0 "
            "WHERE is_important=1 "
            "AND (last_accessed IS NULL OR last_accessed < ?) "
            "AND COALESCE(user_cleared_important, 0) = 0",
            (cutoff,))
        return int(getattr(cur, "rowcount", 0) or 0)

    # ---- 引用明细（第 8 步）：记忆/知识库统一引用溯源 -------------------
    def record_citations(self, cited_ids: list[str], message_id: int,
                         session_id: str) -> None:
        """引用事件落表：记录被哪条消息/会话引用；源自知识库文档的记忆
        同步回溯 doc_id，使文档侧也能展示被引用记录。"""
        if not cited_ids:
            return
        now = now_cst().isoformat(timespec="seconds")
        # 一次性扫 raw_docs 构建 memory_id -> doc_id 映射，替代逐条 LIKE 查询（N+1）
        doc_map: dict[str, int] = {}
        for r in self.db.query_all(
                "SELECT id, extracted_memory_ids FROM raw_docs "
                "WHERE extracted_memory_ids IS NOT NULL "
                "AND extracted_memory_ids != ''"):
            try:
                ids = json.loads(r["extracted_memory_ids"] or "[]")
            except (json.JSONDecodeError, TypeError):
                continue
            for mid in ids:
                doc_map.setdefault(mid, r["id"])
        self.db.executemany(
            "INSERT INTO citation_events(memory_id,doc_id,message_id,"
            "session_id,cited_at) VALUES(?,?,?,?,?)",
            [(mid, doc_map.get(mid), message_id, session_id, now)
             for mid in cited_ids])

    def record_feedback(self, memory_id: str, feedback_type: str,
                        message_id: int | None = None,
                        query_text: str | None = None) -> None:
        """记录记忆级反馈；幂等：同一 (memory_id, message_id, feedback_type)
        重复调用只落一条，避免前端重复触发/网络重试污染治理队列与降权计数。"""
        # 幂等去重：同一 (memory_id, message_id, feedback_type) 已记则跳过
        if message_id is None:
            dup = self.db.query_one(
                "SELECT 1 FROM memory_feedback WHERE memory_id=? "
                "AND message_id IS NULL AND feedback_type=? LIMIT 1",
                (memory_id, feedback_type))
        else:
            dup = self.db.query_one(
                "SELECT 1 FROM memory_feedback WHERE memory_id=? "
                "AND message_id=? AND feedback_type=? LIMIT 1",
                (memory_id, message_id, feedback_type))
        if dup:
            return
        now = now_cst().isoformat(timespec="seconds")
        self.db.execute(
            "INSERT INTO memory_feedback(memory_id,message_id,feedback_type,"
            "query_text,created_at) VALUES(?,?,?,?,?)",
            (memory_id, message_id, feedback_type, query_text, now))
        if feedback_type == "irrelevant":
            self.db.execute(
                "UPDATE memories SET retrieval_negative_count="
                "COALESCE(retrieval_negative_count, 0) + 1 WHERE id=?",
                (memory_id,))
            # 治理队列同样幂等：同一 memory 已有 open 事项则不再新建
            already_open = self.db.query_one(
                "SELECT 1 FROM memory_governance_items WHERE primary_memory_id=? "
                "AND item_type='retrieval_irrelevant' AND status='open' LIMIT 1",
                (memory_id,))
            if not already_open:
                row = self.palace.get(memory_id)
                if row:
                    priority = (row["access_count"] or 0) + 1
                    self.db.execute(
                        "INSERT INTO memory_governance_items(item_id,item_type,"
                        "primary_memory_id,priority,status,reason,detail_json,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (f"gov_{uuid.uuid4().hex[:12]}", "retrieval_irrelevant",
                         memory_id, priority, "open", "用户标记本轮检索无关",
                         json.dumps({"query": query_text}, ensure_ascii=False), now))

    def record_retrieval_event(self, session_id: str | None, message_id: int | None,
                               query: str, diagnostics: dict) -> None:
        """保存轻量检索轨迹，供误命中回放和阈值校准使用。"""
        now = now_cst().isoformat(timespec="seconds")
        self.db.execute(
            "INSERT INTO retrieval_events(session_id,message_id,query_text,query_type,"
            "gate,candidates_json,selected_json,rejected_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (session_id, message_id, query, diagnostics.get("query_type"),
             diagnostics.get("gate"),
             json.dumps(diagnostics.get("candidate_ids", []), ensure_ascii=False),
             json.dumps(diagnostics.get("selected_ids", []), ensure_ascii=False),
             json.dumps(diagnostics.get("rejected", []), ensure_ascii=False), now))
