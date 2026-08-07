"""每日综合扫描任务：队列清理 + 通知 + 冲突检测触发。

调度约定：
- 04:30 执行（晚于记忆维护链 03:00-04:00 的 profile_rebuild），
  避免与 rebuild 链冲突。对应 scheduler 循环新增 04:30 窗口。
- 不独立触发 ProfileBuilder.rebuild()，冲突检测由 rebuild 内部自动完成。
- 职责：清理过期记录、到期拒绝保护、pending 堆积通知。
"""
from __future__ import annotations

import logging
from datetime import timedelta

from infrastructure.timeutil import now_cst

logger = logging.getLogger("second_person.profile_review_scan")


class ProfileReviewScanner:
    """画像审核队列维护任务。每日凌晨执行清理 + 通知。"""

    def __init__(self, db, conflict_scanner, config, notifier=None):
        self.db = db
        self.scanner = conflict_scanner
        self.config = config
        self.notify = notifier or (lambda t, m: None)

    async def daily_scan(self) -> dict:
        """每日扫描：清理 + 通知。返回结果摘要。"""
        result = {"expired": 0, "cleaned_rejections": 0, "pending_count": 0}

        # 1. 清理过期 pending 和到期拒绝保护
        try:
            expired, cleaned = self.scanner.clean_expired()
            result["expired"] = expired
            result["cleaned_rejections"] = cleaned
        except Exception:
            logger.warning("过期清理失败", exc_info=True)

        # 2. pending 数量通知
        try:
            counts = self.scanner.pending_count()
            result["pending_count"] = counts.get("total", 0)
            threshold = self.config.get("review_queue_notify_threshold", 3)
            if result["pending_count"] >= threshold:
                self._notify_user(result["pending_count"])
        except Exception:
            logger.warning("pending 计数失败", exc_info=True)

        return result

    def _notify_user(self, count: int) -> None:
        """通过 conversations 表插入 system_notification，24h 内同 notification_type 去重。"""
        now = now_cst()
        cutoff = (now - timedelta(hours=24)).isoformat(timespec="seconds")

        # 24h 去重：避免每天重复提醒相同的 pending
        existing = self.db.query_one(
            "SELECT 1 FROM conversations "
            "WHERE session_id='_system' AND notification_type='profile_review_pending' "
            "AND create_time > ? LIMIT 1",
            (cutoff,),
        )
        if existing:
            return

        self.db.execute(
            "INSERT INTO conversations"
            "(session_id,role,message_type,notification_type,content,create_time) "
            "VALUES('_system','system','system_notification',"
            "'profile_review_pending',?,?)",
            (
                f"你的画像有 {count} 项待确认更新，前往画像管理查看",
                now.isoformat(timespec="seconds"),
            ),
        )
        logger.info("画像审核通知：%d 项 pending", count)
