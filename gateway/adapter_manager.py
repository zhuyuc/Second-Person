"""
Gateway 适配器管理器 —— 加载并管理已启用的 IM Platform Adapter。

- 启动时加载 enabled=1 且非 web 的 IM 平台适配器并 connect
- 同时只启用一个 IM 平台
- 为通知管理器注入 IM 发送器（系统通知双端推送）
- reload()：平台启停后重新加载
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from .platforms.base import BasePlatformAdapter
from .platforms.dingtalk import DingtalkAdapter
from .platforms.feishu import FeishuAdapter
from .platforms.telegram import TelegramAdapter
from .platforms.wecom import WecomAdapter
from infrastructure.timeutil import now_cst

logger = logging.getLogger("second_person.gateway")

ADAPTER_TYPES = {
    "telegram": TelegramAdapter, "feishu": FeishuAdapter, "dingtalk": DingtalkAdapter,
    "wecom": WecomAdapter,
}


class AdapterManager:
    def __init__(self, db, creds, core, sessions, notifications, config, ingest=None):
        self.db = db
        self.creds = creds
        self.core = core
        self.sessions = sessions
        self.notifications = notifications
        self.config = config
        self.ingest = ingest
        self.data_dir = getattr(sessions, "data_dir", None)
        self._active: BasePlatformAdapter | None = None

    async def load_enabled(self) -> None:
        row = self.db.query_one(
            "SELECT * FROM platforms WHERE enabled=1 AND platform_type!='web' LIMIT 1")
        if not row:
            self.notifications.set_im_sender(None)
            return
        cls = ADAPTER_TYPES.get(row["platform_type"])
        if not cls:
            return
        cfg = {"whitelist_user_id": row["whitelist_user_id"],
               "callback_url": row["callback_url"]}
        if row["credential_id"]:
            sec = self.creds.get(row["credential_id"])
            if sec:
                try:
                    cfg.update(json.loads(sec))
                except json.JSONDecodeError:
                    pass
        adapter = cls(
            row["id"], cfg, core=self.core, sessions=self.sessions, db=self.db,
            notifier=self.notifications.push,
            im_max_chars=self.config.get("im_message_max_chars", 4000),
            data_dir=self.data_dir, ingest=self.ingest)
        try:
            await adapter.connect()
            self._active = adapter
            self.notifications.set_im_sender(
                lambda text: adapter.send_message(row["whitelist_user_id"] or "", text))
            logger.info("已加载 IM 适配器：%s", row["platform_type"])
        except Exception as e:  # noqa: BLE001
            logger.warning("IM 适配器 %s 连接失败", row["platform_type"])
            # 真实状态落库：避免 UI 显示“已启用/健康”但实际无连接
            self.db.execute(
                "UPDATE platforms SET status='paused', last_failure_time=?, "
                "last_failure_reason=? WHERE id=?",
                (now_cst().isoformat(timespec="seconds"),
                 f"连接失败：{e}", row["id"]))

    async def reload(self) -> None:
        if self._active:
            try:
                await self._active.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._active = None
        await self.load_enabled()

    async def stop(self) -> None:
        if self._active:
            await self._active.disconnect()
            self._active = None

    @property
    def active(self) -> BasePlatformAdapter | None:
        return self._active
