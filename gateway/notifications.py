"""
系统通知机制（产品文档 §通知落库规则 / 会话管理）。

- 系统通知作为 role=assistant、message_type=system_notification 的消息写入 conversations
- 目标会话：sessions 按 last_active 降序取第一条
- 无会话时暂存 pending_notifications 内存队列，创建首个会话时补发
- 多端：Web + 当前接入的 IM 平台双端推送
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from infrastructure.timeutil import now_cst

logger = logging.getLogger("second_person.notify")

# ---- 推送去重（统一兑底，防同一条消息重复落库/重复推送） ----
# 设计原则：去重只合并“同一次事件的多重触发”（事件簇/连发），
# 绝不吞掉跨时间的持续失败信号——重复出错说明问题严重且未解决，
# 必须让用户感知（否则会误以为已处理）。
# 仅“阈值告警型”（提醒一次即可的状态告知）做长窗口去重：
# 预算超限/容量超限/渠道熔断告知，24h 内同内容不重复推送
STATE_NOTIFY_TYPES = {
    "platform_paused", "raw_docs_capacity",
    "budget_alert_daily", "budget_exceeded_daily",
    "budget_alert_monthly", "budget_exceeded_monthly",
    "db_write_degraded",
}
STATE_DEDUP_SECONDS = 24 * 3600
# 事件/错误型通知：仅合并短窗内的事件簇/连发（60s），
# 跨时间的再次失败（如 soul_reset 每轮对话检测失败）仍会推送
EVENT_DEDUP_SECONDS = 60


class NotificationManager:
    def __init__(self, db, session_store):
        self.db = db
        self.sessions = session_store
        self._pending: list[tuple[str, str]] = []   # (type, message) 无会话时暂存
        # 由 Gateway 注入 async(text)->None
        self._im_sender = None
        self._main_loop: asyncio.AbstractEventLoop | None = None

    def set_im_sender(self, sender) -> None:
        self._im_sender = sender
        # 捕获主事件循环引用：后台线程（调度器等）调用 push 时可跨线程投递 IM 推送
        try:
            self._main_loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

    def push(self, notification_type: str, message: str) -> None:
        """推送系统通知（同步入口，供各模块的 notifier 回调）。
        去重只防同一次事件的多重触发（60s 事件簇）；阈值告警型额外 24h。
        持续失败（如注入回退每轮对话再检测）不做跨时间去重，
        每次失败都推送，让用户感知问题仍未解决。"""
        if self._is_duplicated(notification_type, message):
            return
        sid = self.sessions.latest_active_session()
        if not sid:
            # 无会话暂存同样去重，避免 pending 内堆积同内容
            if any(t == notification_type and m == message
                   for t, m in self._pending):
                return
            self._pending.append((notification_type, message))
            return
        self.sessions.append_message(
            sid, "assistant", message, message_type="system_notification",
            notification_type=notification_type)
        self._send_im(message)

    def _is_duplicated(self, notification_type: str, message: str) -> bool:
        """窗口内是否已推送过同类型+同内容（查 conversations 落库记录）。"""
        window = (STATE_DEDUP_SECONDS if notification_type in STATE_NOTIFY_TYPES
                  else EVENT_DEDUP_SECONDS)
        cutoff = (now_cst() - timedelta(seconds=window)
                  ).isoformat(timespec="seconds")
        return bool(self.db.query_one(
            "SELECT 1 FROM conversations WHERE notification_type=? AND content=? "
            "AND create_time>=? LIMIT 1",
            (notification_type, message, cutoff)))

    def _send_im(self, message: str) -> None:
        """IM 双端推送：优先当前循环，无运行循环（后台线程）时桥接到主循环。"""
        if not self._im_sender:
            return
        try:
            asyncio.get_running_loop().create_task(self._im_sender(message))
        except RuntimeError:
            if self._main_loop and not self._main_loop.is_closed():
                asyncio.run_coroutine_threadsafe(
                    self._im_sender(message), self._main_loop)
            else:
                logger.debug("IM 通知推送跳过：无可用事件循环")
        except Exception:  # noqa: BLE001
            logger.warning("IM 通知推送失败", exc_info=True)

    def resolve(self, notification_type: str, resolved_message: str) -> int:
        """失败通知的恢复闭环：原失败横幅保持原文不动（避免用户对改写后的
        历史消息困惑），仅把 notification_type 加 _resolved 后缀做幂等标记，
        并在失败通知所在的每个原会话尾部追加一条已恢复新通知（而非投递
        到最新会话，保证用户在原会话内能看到恢复提示）。返回标记条数。"""
        rows = self.db.query_all(
            "SELECT DISTINCT session_id FROM conversations WHERE notification_type=?",
            (notification_type,))
        if not rows:
            return 0
        res = self.db.execute(
            "UPDATE conversations SET notification_type=? WHERE notification_type=?",
            (notification_type + "_resolved", notification_type))
        for r in rows:
            self.sessions.append_message(
                r["session_id"], "assistant", resolved_message,
                message_type="system_notification",
                notification_type=notification_type + "_recovered")
        logger.info("失败通知已标记解决：type=%s count=%d，已在 %d 个原会话追加恢复通知",
                    notification_type, res.rowcount, len(rows))
        self._send_im(resolved_message)
        return res.rowcount

    def flush_pending(self) -> None:
        """创建首个会话后补发暂存通知。"""
        if not self._pending:
            return
        pending, self._pending = self._pending, []
        for ntype, msg in pending:
            self.push(ntype, msg)
