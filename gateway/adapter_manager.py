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
from .platforms.weixin import WeixinAdapter
from infrastructure.timeutil import now_cst

logger = logging.getLogger("second_person.gateway")

ADAPTER_TYPES = {
    "telegram": TelegramAdapter, "feishu": FeishuAdapter, "dingtalk": DingtalkAdapter,
    "wecom": WecomAdapter, "weixin": WeixinAdapter,
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
        # 微信渠道注入凭证持久化句柄：游标/会话 token 由适配器写回凭证（重启免扫码）
        if row["platform_type"] == "weixin":
            cfg["_credential_id"] = row["credential_id"]
            cfg["_creds"] = self.creds
        adapter = cls(
            row["id"], cfg, core=self.core, sessions=self.sessions, db=self.db,
            notifier=self.notifications.push,
            im_max_chars=self.config.get("im_message_max_chars", 4000),
            data_dir=self.data_dir, ingest=self.ingest)
        try:
            await adapter.connect()
            self._active = adapter

            def _im_send(text, _ad=adapter):
                target = self.resolve_push_target()
                if not target:
                    logger.warning("IM 主动推送跳过：%s 无可用推送目标（whitelist 为空且"
                                   "无入站消息记录）", _ad.platform_type)
                    return
                return _ad.send_message(target, text)
            # 微信渠道不支持独立主动推送（iLink 官方限制：sendmessage 仅在即时回复场景送达），
            # 跳过 _im_sender 注入，系统通知只推 Web 端+其他 IM 渠道
            if row["platform_type"] != "weixin":
                self.notifications.set_im_sender(_im_send)
            else:
                self.notifications.set_im_sender(None)
                logger.info("微信渠道已启用（不支持主动推送，系统通知仅推 Web 端）")
            logger.info("已加载 IM 适配器：%s", row["platform_type"])
        except Exception as e:  # noqa: BLE001
            logger.warning("IM 适配器 %s 连接失败", row["platform_type"])
            # 真实状态落库：避免 UI 显示“已启用/健康”但实际无连接
            self.db.execute(
                "UPDATE platforms SET status='paused', last_failure_time=?, "
                "last_failure_reason=? WHERE id=?",
                (now_cst().isoformat(timespec="seconds"),
                 f"连接失败：{e}", row["id"]))

    def resolve_push_target(self) -> str:
        """解析当前 IM 主动推送目标（每次调用时实时求值，配置变更即生效）：
        优先用户在设置页录入的 whitelist_user_id；为空时回退到最近一次入站
        消息的发送者 open_id（platform_sessions 已持久化），避免空目标静默失败。"""
        row = self.db.query_one(
            "SELECT platform_type, whitelist_user_id FROM platforms "
            "WHERE enabled=1 AND platform_type!='web' LIMIT 1")
        if not row:
            return ""
        if row["whitelist_user_id"]:
            return row["whitelist_user_id"]
        r = self.db.query_one(
            "SELECT platform_user_id FROM platform_sessions WHERE platform=? "
            "ORDER BY created_at DESC LIMIT 1", (row["platform_type"],))
        return (r["platform_user_id"] if r else "") or ""

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
