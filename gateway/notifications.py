"""
系统通知机制（产品文档 §通知落库规则 / 会话管理）。

- 系统通知作为 role=assistant、message_type=system_notification 的消息写入 conversations
- 目标会话：sessions 按 last_active 降序取第一条
- 无会话时暂存 pending_notifications 内存队列，创建首个会话时补发
- 同 notification_type 24 小时内去重，重复只更新时间戳
- 多端：Web + 当前接入的 IM 平台双端推送
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

logger = logging.getLogger("second_person.notify")


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
        """推送系统通知（同步入口，供各模块的 notifier 回调）。"""
        sid = self.sessions.latest_active_session()
        if not sid:
            self._pending.append((notification_type, message))
            return
        # 24h 去重
        cutoff = (datetime.now() - timedelta(hours=24)
                  ).isoformat(timespec="seconds")
        dup = self.db.query_one(
            "SELECT id FROM conversations WHERE notification_type=? AND create_time>=? "
            "ORDER BY id DESC LIMIT 1", (notification_type, cutoff))
        if dup:
            self.db.execute("UPDATE conversations SET create_time=? WHERE id=?",
                            (datetime.now().isoformat(timespec="seconds"), dup["id"]))
            return
        self.sessions.append_message(
            sid, "assistant", message, message_type="system_notification",
            notification_type=notification_type)
        self._send_im(message)

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
