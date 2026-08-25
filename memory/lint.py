"""
Lint 健康检查（产品文档 §记忆维护 / §健康度 / 开发文档 §2.10 健康分契约）。

六项检查 + 第七项技能提炼与归档：
  过期检测 / 孤立检测 / 矛盾检测 / 待确认检测 / 目录漂移修复 / 重复检测 + 技能提炼归档
健康分（0-100，从 100 扣减，扣完为止，基数不含 archived）：
  disputed 3 / low_unconfirmed 1 / stale 0.5 / orphan 0.5 / duplicate 1 /
  missing 2 / failed_writes 一次性 10
健康分由后端唯一计算，前端只展示不重算。
"""
from __future__ import annotations

import logging
from datetime import timedelta

from .naming import suggestion_id as make_sug_id
from infrastructure.timeutil import now_cst

logger = logging.getLogger("second_person.lint")

DEDUCT = {"disputed": 3, "low_unconfirmed": 1, "stale": 0.5,
          "orphan": 0.5, "duplicate": 1, "missing": 2}


class LintEngine:
    def __init__(self, db, palace, vector_store, config):
        self.db = db
        self.palace = palace
        self.vs = vector_store
        self.config = config

    # ---- 统计各扣分项 -----------------------------------------------------
    def counts(self) -> dict[str, int]:
        db = self.db
        disputed = db.query_one(
            "SELECT count(*) c FROM memories WHERE confidence='disputed'")["c"]
        stale = db.query_one(
            "SELECT count(*) c FROM memories WHERE lifecycle='stale'")["c"]
        missing = db.query_one(
            "SELECT count(*) c FROM memories WHERE lifecycle='missing'")["c"]
        # 健康扣分口径：零连接 且 未被用户处理。adopted = 用户确认保留；
        # dismissed = 用户主动忽略。两者均不再视为质量问题，与建议列表口径一致；
        # palace.orphans()（建议生成/批量补链用）仍为全量零连接口径，不受此影响。
        orphan = db.query_one(
            "SELECT count(*) c FROM memories m "
            "WHERE m.lifecycle IN ('active','stable','stale') "
            "AND NOT EXISTS (SELECT 1 FROM memory_links l "
            "WHERE l.target_id=m.id OR l.source_id=m.id) "
            "AND NOT EXISTS (SELECT 1 FROM lint_suggestions s "
            "WHERE s.primary_memory_id=m.id AND s.suggestion_type='orphan' "
            "AND s.status IN ('adopted','dismissed'))")["c"]
        duplicate = db.query_one(
            "SELECT count(*) c FROM lint_suggestions "
            "WHERE status='open' AND suggestion_type='duplicate'")["c"]
        cutoff = (now_cst() - timedelta(days=30)
                  ).isoformat(timespec="seconds")
        low_unconfirmed = db.query_one(
            "SELECT count(*) c FROM memories WHERE confidence='low' "
            "AND (created_at < ? OR created_at IS NULL)", (cutoff,))["c"]
        failed_writes = db.query_one(
            "SELECT count(*) c FROM pending_writes WHERE status='failed'")["c"]
        return {"disputed": disputed, "stale": stale, "missing": missing,
                "orphan": orphan, "duplicate": duplicate,
                "low_unconfirmed": low_unconfirmed, "failed_writes": failed_writes}

    def health_score(self, counts: dict | None = None) -> tuple[int, list[dict]]:
        c = counts or self.counts()
        breakdown = []
        total_deduct = 0.0
        for key in ("disputed", "low_unconfirmed", "stale", "orphan", "duplicate", "missing"):
            deduct = c[key] * DEDUCT[key]
            breakdown.append(
                {"reason": key, "count": c[key], "deduct": deduct})
            total_deduct += deduct
        fw = 10 if c["failed_writes"] > 0 else 0
        breakdown.append({"reason": "failed_writes",
                         "count": c["failed_writes"], "deduct": fw})
        total_deduct += fw
        score = max(0, round(100 - total_deduct))
        return score, breakdown

    # ---- 孤立检测：生成建议 -----------------------------------------------
    def detect_orphans(self, lint_run_id: str) -> list[str]:
        orphan_set = set(self.palace.orphans())
        # 口径变更/建链后已不再孤立的残留 open 建议自动关闭，避免列表幽灵条目
        stale_rows = self.db.query_all(
            "SELECT suggestion_id, primary_memory_id FROM lint_suggestions "
            "WHERE suggestion_type='orphan' AND status='open'")
        for r in stale_rows:
            if r["primary_memory_id"] not in orphan_set:
                self.db.execute(
                    "UPDATE lint_suggestions SET status='dismissed', "
                    "dismiss_reason='no_longer_orphan', resolved_at=? "
                    "WHERE suggestion_id=?",
                    (now_cst().isoformat(timespec="seconds"),
                     r["suggestion_id"]))
        sug_ids = []
        for mid in orphan_set:
            # 去重：已有 open 建议的跳过；已被用户采纳（确认保留）但因无相似记忆
            # 仍孤立的也跳过，不重复打扰；后续建链由提炼/补链路径自动完成。
            exists = self.db.query_one(
                "SELECT 1 FROM lint_suggestions WHERE primary_memory_id=? "
                "AND suggestion_type='orphan' AND status IN ('open','adopted','dismissed')", (mid,))
            if exists:
                continue
            sid = make_sug_id()
            self.db.execute(
                "INSERT INTO lint_suggestions(suggestion_id,lint_run_id,suggestion_type,"
                "primary_memory_id,detail,status,created_at) "
                "VALUES(?,?,'orphan',?,?,'open',?)",
                (sid, lint_run_id, mid, "零连接，建议按语义相似度建立 related 引用",
                 now_cst().isoformat(timespec="seconds")))
            sug_ids.append(sid)
        return sug_ids

    # ---- 重复检测：相似度 > lint_duplicate_threshold -----------------------
    def detect_duplicates(self, lint_run_id: str) -> list[str]:
        threshold = self.config.get("lint_duplicate_threshold", 0.9)
        sug_ids = []
        checked: set[tuple] = set()
        rows = self.db.query_all(
            "SELECT v.memory_id, v.embedding FROM vectors v "
            "JOIN memories m ON v.memory_id=m.id "
            "WHERE v.vector_status='ready' AND m.lifecycle IN ('active','stable','stale')")
        from .vector_store import deserialize_vector
        for r in rows:
            vec = deserialize_vector(r["embedding"])
            for mid, score in self.vs.top_similar(vec, n=3):
                if mid == r["memory_id"] or score < threshold:
                    continue
                pair = tuple(sorted([r["memory_id"], mid]))
                if pair in checked:
                    continue
                checked.add(pair)
                # 已标记 contradicts 的矛盾对不进重复候选：否则“采纳合并”会
                # 直接删掉矛盾一侧，与矛盾裁决抢同一对记忆
                if self._has_contradicts_link(pair[0], pair[1]):
                    continue
                # not_duplicate 标记过则跳过
                dismissed = self.db.query_one(
                    "SELECT 1 FROM lint_suggestions WHERE suggestion_type='duplicate' "
                    "AND dismiss_reason='not_duplicate' AND "
                    "((primary_memory_id=? AND related_memory_id=?) OR "
                    "(primary_memory_id=? AND related_memory_id=?))",
                    (pair[0], pair[1], pair[1], pair[0]))
                if dismissed:
                    continue
                sid = make_sug_id()
                self.db.execute(
                    "INSERT INTO lint_suggestions(suggestion_id,lint_run_id,suggestion_type,"
                    "primary_memory_id,related_memory_id,detail,status,created_at) "
                    "VALUES(?,?,'duplicate',?,?,?,'open',?)",
                    (sid, lint_run_id, pair[0], pair[1],
                     f"相似度 {score:.2f} 疑似重复",
                     now_cst().isoformat(timespec="seconds")))
                sug_ids.append(sid)
        return sug_ids

    def _has_contradicts_link(self, a: str, b: str) -> bool:
        return bool(self.db.query_one(
            "SELECT 1 FROM memory_links WHERE link_type='contradicts' AND "
            "((source_id=? AND target_id=?) OR (source_id=? AND target_id=?))",
            (a, b, b, a)))

    def _has_judged_link(self, a: str, b: str) -> bool:
        """两条记忆间已有矛盾/相关/演变引用 = 关系已判定过，
        不再重复送 LLM（避免夜间扫描每次都重判同一对）。"""
        return bool(self.db.query_one(
            "SELECT 1 FROM memory_links WHERE link_type IN "
            "('contradicts','related','evolved_from') AND "
            "((source_id=? AND target_id=?) OR (source_id=? AND target_id=?))",
            (a, b, b, a)))

    # ---- 矛盾检测候选：高相似且未标记矛盾的记忆对（供 LintAgent 送 LLM 判定）
    def conflict_candidate_pairs(self, floor: float = 0.8,
                                 limit: int = 10) -> list[tuple[str, str, float]]:
        pairs: list[tuple[str, str, float]] = []
        checked: set[tuple] = set()
        rows = self.db.query_all(
            "SELECT v.memory_id, v.embedding FROM vectors v "
            "JOIN memories m ON v.memory_id=m.id "
            "WHERE v.vector_status='ready' AND m.lifecycle IN ('active','stable','stale') "
            "AND m.confidence != 'disputed'")
        from .vector_store import deserialize_vector
        for r in rows:
            vec = deserialize_vector(r["embedding"])
            for mid, score in self.vs.top_similar(vec, n=3):
                if mid == r["memory_id"] or score < floor:
                    continue
                pair = tuple(sorted([r["memory_id"], mid]))
                if pair in checked:
                    continue
                checked.add(pair)
                other = self.db.query_one(
                    "SELECT confidence FROM memories WHERE id=?", (mid,))
                if not other or other["confidence"] == "disputed":
                    continue
                if self._has_judged_link(pair[0], pair[1]):
                    continue
                pairs.append((pair[0], pair[1], score))
                if len(pairs) >= limit:
                    return pairs
        return pairs

    # ---- 目录漂移修复：md 主副本与索引表 title/summary 不一致时回写索引 ----
    def fix_index_drift(self, palace, data_dir) -> int:
        """逐条比对 md frontmatter/summary 与 memories 索引，不一致则以 md
        为准同步索引与 FTS（只写派生索引不回写 md，与 watcher 同模式）。"""
        from pathlib import Path
        from .md_file import parse_memory_md
        data_dir = Path(data_dir)
        fixed = 0
        rows = self.db.query_all(
            "SELECT id, title, summary, md_path, domain FROM memories "
            "WHERE lifecycle IN ('active','stable','stale')")
        for r in rows:
            f = data_dir / r["md_path"]
            if not f.exists():
                continue
            try:
                doc = parse_memory_md(f.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if doc.title == (r["title"] or "") and doc.summary == (r["summary"] or ""):
                continue
            with self.db.transaction() as conn:
                palace.upsert_index(conn, doc.frontmatter,
                                    doc.summary, r["md_path"])
                palace.sync_fts(conn, r["id"], doc.title, doc.summary,
                                doc.detail, doc.domain or r["domain"])
            fixed += 1
        if fixed:
            logger.info("目录漂移修复：同步 %d 条索引", fixed)
        return fixed

    def lint_details(self, counts: dict) -> list[dict]:
        # 孤立建议：查 primary_memory_id → 取 title/summary
        orphans = self.db.query_all(
            "SELECT s.suggestion_id, s.primary_memory_id, m.title, m.summary "
            "FROM lint_suggestions s LEFT JOIN memories m ON s.primary_memory_id=m.id "
            "WHERE s.status='open' AND s.suggestion_type='orphan'")
        open_orphan = [{"id": r["suggestion_id"], "memory_id": r["primary_memory_id"],
                        "title": r["title"] or r["primary_memory_id"],
                        "summary": r["summary"] or ""} for r in orphans]
        # 重复建议：查 primary_memory_id + related_memory_id
        dups = self.db.query_all(
            "SELECT s.suggestion_id, s.primary_memory_id, s.related_memory_id,"
            " m1.title t1, m2.title t2, m1.summary s1, m2.summary s2 "
            "FROM lint_suggestions s LEFT JOIN memories m1 ON s.primary_memory_id=m1.id "
            "LEFT JOIN memories m2 ON s.related_memory_id=m2.id "
            "WHERE s.status='open' AND s.suggestion_type='duplicate'")
        open_dup = [{"id": r["suggestion_id"],
                     "memory_a": {"id": r["primary_memory_id"],
                                  "title": r["t1"] or r["primary_memory_id"],
                                  "summary": r["s1"] or ""},
                     "memory_b": {"id": r["related_memory_id"],
                                  "title": r["t2"] or r["related_memory_id"],
                                  "summary": r["s2"] or ""}} for r in dups]
        # 低置信未确认：暴露具体条目，前端可直接查看/确认
        cutoff = (now_cst() - timedelta(days=30)
                  ).isoformat(timespec="seconds")
        low_rows = self.db.query_all(
            "SELECT id, title, summary, created_at FROM memories "
            "WHERE confidence='low' AND (created_at < ? OR created_at IS NULL) "
            "ORDER BY created_at ASC LIMIT 50", (cutoff,))
        open_low = [{"id": r["id"], "memory_id": r["id"],
                     "title": r["title"] or r["id"],
                     "summary": r["summary"] or "",
                     "created_at": r["created_at"] or ""} for r in low_rows]
        return [
            {"check": "过期检测", "desc": "90 天未访问", "count": counts["stale"],
             "status": "ok" if counts["stale"] == 0 else "warning"},
            {"check": "矛盾检测", "desc": "事实冲突", "count": counts["disputed"],
             "status": "ok" if counts["disputed"] == 0 else "warning"},
            {"check": "孤立检测", "desc": "零连接记忆（无任何引用边），可一键建立 related 引用",
             "count": counts["orphan"], "status": "info", "actionable": True,
             "suggestion_ids": open_orphan},
            {"check": "重复检测", "desc": "相似度超阈值疑似重复",
             "count": counts["duplicate"], "status": "info", "actionable": True,
             "suggestion_ids": open_dup},
            {"check": "低置信度未确认", "desc": "confidence=low 且超 30 天",
             "count": counts["low_unconfirmed"],
             "status": "ok" if counts["low_unconfirmed"] == 0 else "info",
             "actionable": counts["low_unconfirmed"] > 0,
             "suggestion_ids": open_low},
            {"check": "missing 记忆", "desc": "md 文件缺失", "count": counts["missing"],
             "status": "ok" if counts["missing"] == 0 else "warning"},
            {"check": "failed 写入", "desc": "pending_writes 状态 failed",
             "count": counts["failed_writes"],
             "status": "ok" if counts["failed_writes"] == 0 else "warning"},
        ]
