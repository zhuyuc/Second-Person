"""检测画像重建前后的方向冲突，符合条件的维度入 review queue。

三轨支持：
- user_profile：重建前后对比，方向反转 → 入队
- output_style：累积路径 vs 手编锁定 → 入队（框架，preview_build 待 OutputStyleBuilder 追加）

使用 convergence 模型（廉价模型）做对比，不消耗主对话模型的 token 预算。
"""
from __future__ import annotations

import hashlib
import logging
from datetime import timedelta

from infrastructure.prompt_loader import PROMPTS
from infrastructure.timeutil import now_cst

logger = logging.getLogger("second_person.profile_conflict")


class ProfileConflictScanner:
    """画像冲突扫描器。在 ProfileBuilder.rebuild 完成后调用，将方向反转的维度入队。"""

    def __init__(self, db, llm, providers, config):
        self.db = db
        self.llm = llm
        self.providers = providers
        self.config = config

    # ------------------------------------------------------------------
    # user_profile 轨道
    # ------------------------------------------------------------------
    async def scan_profile_rebuild(self, old_content: str, new_content: str) -> int:
        """对比新旧用户画像，识别方向反转的维度入队列。返回入队数。"""
        if not old_content or not new_content:
            return 0

        snap = self.providers.snapshot_for("agent") or self.providers.snapshot_for("chat")
        if snap is None:
            return 0

        try:
            from infrastructure.json_repair import repair_json

            prompt = PROMPTS.render(
                "agent/prompts/profile_conflict_scan",
                old_profile=old_content[:2000],
                new_profile=new_content[:2000],
            )
            resp = await self.llm.chat(
                snap,
                [{"role": "system", "content": prompt}],
                source="profile_conflict",
                session_id=None,
            )
            data = repair_json(resp["content"]) or {}
            conflicts = data.get("conflicts", [])
        except Exception:
            logger.warning("画像冲突检测 LLM 调用失败", exc_info=True)
            return 0

        added = 0
        now_str = now_cst().isoformat(timespec="seconds")
        for c in conflicts:
            dimension = c.get("dimension", "")
            old_val = c.get("old", "")
            new_val = c.get("new", "")
            reason = c.get("reason", "")
            if not dimension or not new_val:
                continue

            raw_key = f"profile:{dimension}:{new_val[:50]}"
            change_key = hashlib.md5(raw_key.encode()).hexdigest()[:16]

            # 检查拒绝保护
            protected = self.db.query_one(
                "SELECT 1 FROM profile_review_rejections "
                "WHERE change_key=? AND protected_until > ?",
                (change_key, now_str),
            )
            if protected:
                continue

            # 去重：同一 change_key + pending 已有则跳过
            dup = self.db.query_one(
                "SELECT 1 FROM profile_review_queue "
                "WHERE change_key=? AND status='pending'",
                (change_key,),
            )
            if dup:
                continue

            self.db.execute(
                "INSERT INTO profile_review_queue"
                "(review_type,change_key,title,proposed_content,current_content,"
                "evidence,conflict_reason,priority,status,created_at) "
                "VALUES('user_profile',?,?,?,?,?,?,?,'pending',?)",
                (
                    change_key,
                    f"用户画像调整：{dimension}",
                    new_val,
                    old_val,
                    "来自本轮画像重建结果",
                    reason,
                    1,  # 高优先级：明确冲突
                    now_str,
                ),
            )
            added += 1
        return added

    # ------------------------------------------------------------------
    # output_style 轨道
    # ------------------------------------------------------------------
    async def scan_output_style_conflict(self) -> int:
        """检测输出喜好累积路径想更新但被手编锁定的情况入队列。

        触发条件：output_style_state.locked_by_user=1 且
        OutputStyleBuilder 累积统计产出的画像与手编内容有显著差异。

        当前为框架实现：依赖 OutputStyleBuilder.preview_build() 方法，
        该方法尚未实现时返回 0（不阻断流程）。
        """
        state = self.db.query_one(
            "SELECT last_build_at, last_user_edit_at, locked_by_user "
            "FROM output_style_state WHERE id=1"
        )
        if not state or not state["locked_by_user"]:
            return 0

        # 手编锁定后无新累积 → 无需冲突检测
        last_build = state["last_build_at"]
        last_edit = state["last_user_edit_at"]
        if not last_build or not last_edit:
            return 0

        # 累积在锁定之后发生 → 可能有冲突
        if last_build <= last_edit:
            return 0

        # 框架预留：调用 OutputStyleBuilder.preview_build() 生成候选
        # preview = await self.output_style_builder.preview_build()
        # if preview and preview != current:
        #     _enqueue_output_style_conflict(...)
        return 0

    # ------------------------------------------------------------------
    # 频次门 + 入队辅助（供 container.soul_feedback_fn 调用）
    # ------------------------------------------------------------------
    def check_rejection_protection(self, change_key: str, now_str: str) -> bool:
        """返回 True 表示该 change_key 处于拒绝保护期内。"""
        row = self.db.query_one(
            "SELECT 1 FROM profile_review_rejections "
            "WHERE change_key=? AND protected_until > ?",
            (change_key, now_str),
        )
        return row is not None

    def accumulate_feedback(
        self, change_key: str, proposed: str, summary: str, ptype: str = "behavior"
    ) -> tuple[int, bool]:
        """累积一次 SOUL 反馈频次，返回 (occurrences, newly_enqueued)。

        若达阈值且尚未入队，调用方应调用 enqueue_persona_review 入队。
        """
        now_str = now_cst().isoformat(timespec="seconds")
        threshold = self.config.get("persona_promote_threshold", 2)

        row = self.db.query_one(
            "SELECT occurrences, enqueued FROM soul_feedback_log WHERE direction_key=?",
            (change_key,),
        )
        if row:
            new_occ = (row["occurrences"] or 0) + 1
            self.db.execute(
                "UPDATE soul_feedback_log SET occurrences=?, last_seen=? "
                "WHERE direction_key=?",
                (new_occ, now_str, change_key),
            )
            newly_enqueued = (new_occ >= threshold) and not row["enqueued"]
            if newly_enqueued:
                self.db.execute(
                    "UPDATE soul_feedback_log SET enqueued=1 WHERE direction_key=?",
                    (change_key,),
                )
            return new_occ, newly_enqueued
        else:
            self.db.execute(
                "INSERT INTO soul_feedback_log"
                "(direction_key,ptype,proposed_change,summary,occurrences,"
                "first_seen,last_seen,enqueued) "
                "VALUES(?,?,?,?,1,?,?,0)",
                (change_key, ptype, proposed, summary, now_str, now_str),
            )
            newly_enqueued = (1 >= threshold)
            if newly_enqueued:
                self.db.execute(
                    "UPDATE soul_feedback_log SET enqueued=1 WHERE direction_key=?",
                    (change_key,),
                )
            return 1, newly_enqueued

    def enqueue_persona_review(
        self, change_key: str, proposed: str, summary: str, occurrences: int,
        current_dialog: str,
    ) -> None:
        """将 persona 反馈正式入队列。"""
        now_str = now_cst().isoformat(timespec="seconds")

        # 去重检查
        dup = self.db.query_one(
            "SELECT 1 FROM profile_review_queue "
            "WHERE change_key=? AND status='pending'",
            (change_key,),
        )
        if dup:
            return

        self.db.execute(
            "INSERT INTO profile_review_queue"
            "(review_type,change_key,title,proposed_content,current_content,"
            "evidence,priority,status,created_at) "
            "VALUES('persona',?,?,?,?,?,?,'pending',?)",
            (
                change_key,
                f"AI 人格调整：{summary[:40]}" if summary else "AI 人格调整",
                proposed,
                current_dialog[:500] if current_dialog else "",
                f"用户在最近对话中 {occurrences} 次表达此类反馈",
                2,  # 中优先级
                now_str,
            ),
        )

    def enqueue_tone_review(self, message_id: int, session_id: str,
                            context_snippet: str = "") -> None:
        """将用户点踩（tone_wrong）入队列。"""
        import hashlib

        change_key = hashlib.md5(
            f"tone_downvote:{message_id}".encode()
        ).hexdigest()[:16]
        now_str = now_cst().isoformat(timespec="seconds")

        # 去重
        dup = self.db.query_one(
            "SELECT 1 FROM profile_review_queue "
            "WHERE change_key=? AND status='pending'",
            (change_key,),
        )
        if dup:
            return

        evidence = f"用户在消息 {message_id} 点踩，理由：语气不对"
        if context_snippet:
            evidence += f"；上下文：{context_snippet[:200]}"

        proposed = "根据用户点踩反馈调整对话语气方向"
        if context_snippet:
            proposed += f"（踩的消息内容：{context_snippet[:300]}）"

        self.db.execute(
            "INSERT INTO profile_review_queue"
            "(review_type,change_key,title,proposed_content,evidence,priority,"
            "status,created_at) "
            "VALUES('persona',?,'AI 语气调整（用户点踩）',?,?,?,'pending',?)",
            (
                change_key,
                proposed,
                evidence,
                2,  # 中优先级
                now_str,
            ),
        )

    def reject_and_protect(self, review_type: str, change_key: str,
                           proposed_summary: str) -> None:
        """拒绝一项建议并设置 60 天保护期，同时清零对应 soul_feedback_log。"""
        now = now_cst()
        protect_days = self.config.get("profile_rejection_protect_days", 60)
        protected_until = (now + timedelta(days=protect_days)
                           ).isoformat(timespec="seconds")

        self.db.execute(
            "INSERT OR REPLACE INTO profile_review_rejections"
            "(review_type,change_key,proposed_content_summary,rejected_at,"
            "protected_until) "
            "VALUES(?,?,?,?,?)",
            (
                review_type,
                change_key,
                (proposed_summary or "")[:200],
                now.isoformat(timespec="seconds"),
                protected_until,
            ),
        )

        # 若为 persona 类，清零累积频次
        if review_type == "persona":
            self.db.execute(
                "UPDATE soul_feedback_log SET occurrences=0, enqueued=0 "
                "WHERE direction_key=?",
                (change_key,),
            )

    def clean_expired(self) -> tuple[int, int]:
        """清理过期 pending（30 天）和过期拒绝保护。返回 (expired_count, cleaned_rejections)。"""
        now = now_cst()
        expire_days = self.config.get("review_queue_expire_days", 30)
        cutoff = (now - timedelta(days=expire_days)
                  ).isoformat(timespec="seconds")

        expired = self.db.execute(
            "UPDATE profile_review_queue SET status='expired', reviewed_at=?, "
            "reviewed_by='system_expire' WHERE status='pending' AND created_at < ?",
            (now.isoformat(timespec="seconds"), cutoff),
        )
        expired_count = expired.rowcount if expired else 0

        cleaned = self.db.execute(
            "DELETE FROM profile_review_rejections WHERE protected_until < ?",
            (now.isoformat(timespec="seconds"),),
        )
        cleaned_count = cleaned.rowcount if cleaned else 0

        return expired_count, cleaned_count

    def pending_count(self, review_type: str | None = None) -> dict[str, int]:
        """返回各轨道 pending 计数（含策略偏好轨道，v3 §画像扩展）。"""
        counts: dict[str, int] = {}
        types = [review_type] if review_type else [
            "persona", "user_profile", "output_style", "strategy_preference"]
        for t in types:
            r = self.db.query_one(
                "SELECT count(*) c FROM profile_review_queue "
                "WHERE status='pending' AND review_type=?",
                (t,),
            )
            counts[t] = r["c"] if r else 0
        if not review_type:
            counts["total"] = sum(counts.values())
        return counts
