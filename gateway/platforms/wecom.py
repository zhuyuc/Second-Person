"""企业微信 Bot 适配器（webhook 接入）。

webhook 被动接入无应用凭据，无法拉取媒体：非文本消息明确提示引导，
不再静默丢弃。
"""
from __future__ import annotations

import logging

import httpx

from .base import BasePlatformAdapter
from .media_parser import WEBHOOK_MEDIA_FALLBACK_SUFFIX, split_media_marker

logger = logging.getLogger("second_person.adapter")

MEDIA_HINT = "当前渠道仅支持文字消息，图片/文件请通过飞书或 Web 端发送"

# 企业微信 markdown 消息 content 上限 4096 字节（UTF-8），
# 中文按 3 字节计，取 1300 字符/段留出余量
_SEG_LEN = 1300


class WecomAdapter(BasePlatformAdapter):
    platform_type = "wecom"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.webhook = self.config.get("callback_url", "")

    async def connect(self) -> None:
        pass  # webhook 被动接入

    async def send_message(self, chat_id: str, text: str) -> None:
        text, media_path = split_media_marker(text)
        if media_path:
            text += WEBHOOK_MEDIA_FALLBACK_SUFFIX
        if not self.webhook:
            return
        # 超长消息分段发送（企微 markdown.content 上限 4096 字节）
        if len(text) > _SEG_LEN:
            for i in range(0, len(text), _SEG_LEN):
                seg = text[i:i + _SEG_LEN]
                if i > 0:
                    seg = f"（续前）\n{seg}"
                async with httpx.AsyncClient(timeout=30) as c:
                    await c.post(self.webhook,
                                 json={"msgtype": "markdown",
                                       "markdown": {"content": seg}})
            return
        async with httpx.AsyncClient(timeout=30) as c:
            await c.post(self.webhook,
                         json={"msgtype": "markdown",
                               "markdown": {"content": text}})

    async def handle_webhook(self, payload: dict) -> None:
        sender = (payload.get("from") or {}).get(
            "userid", payload.get("from_user_id", ""))
        is_group = payload.get("chattype") == "group"
        msg_type = payload.get("msgtype", "text")
        msg_id = payload.get("msgid", "")
        logger.info("企微入站消息 type=%s message_id=%s", msg_type, msg_id)
        chat_id = payload.get("chatid", sender)
        # webhook 模式无应用凭据无法拉取媒体 → 友好降级提示
        if msg_type != "text":
            await self.handle_unsupported(sender, chat_id, msg_id, msg_type,
                                          is_group, hint=MEDIA_HINT)
            return
        await self.on_message(
            platform_user_id=sender,
            chat_id=chat_id,
            message_id=msg_id,
            text=(payload.get("text") or {}).get("content", "").strip(),
            is_group=is_group)
