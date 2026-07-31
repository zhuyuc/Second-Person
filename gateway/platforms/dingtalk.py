"""钉钉 / 企业微信 Bot 适配器（webhook 接入）。

webhook 被动接入无应用凭据，无法拉取媒体：非文本消息明确提示引导，
不再静默丢弃。
"""
from __future__ import annotations

import logging

import httpx

from .base import BasePlatformAdapter

logger = logging.getLogger("second_person.adapter")

MEDIA_HINT = "当前渠道仅支持文字消息，图片/文件请通过飞书或 Web 端发送"


class DingtalkAdapter(BasePlatformAdapter):
    platform_type = "dingtalk"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.webhook = self.config.get("callback_url", "")

    async def connect(self) -> None:
        pass  # webhook 被动接入

    async def send_message(self, chat_id: str, text: str) -> None:
        # 提取 MEDIA: 附件标记（钉钉 webhook 不支持文件上传，改为分段发送兑底）
        media_path = None
        if "MEDIA:" in text:
            lines = text.splitlines()
            text = "\n".join(l for l in lines if not l.startswith("MEDIA:"))
            media_path = next((l[6:]
                              for l in lines if l.startswith("MEDIA:")), None)
        if media_path:
            text += ("\n\n（回复内容较长，部分以附件形式生成，但因平台限制无法直接发送文件，"
                     "请通过 Web 端查看完整回复）")
        # 优先使用钉钉会话级动态回复地址（sessionWebhook 随入站消息下发），
        # 回退到配置的固定群机器人 webhook
        target = chat_id if (chat_id or "").startswith(
            "http") else self.webhook
        if not target:
            return
        # 超长消息分段发送（每段 3500 字符，留 Markdown 标签空间）
        if len(text) > 3500:
            for i in range(0, len(text), 3500):
                seg = text[i:i + 3500]
                if i > 0:
                    seg = f"（续前）\n{seg}"
                async with httpx.AsyncClient(timeout=30) as c:
                    await c.post(target,
                                 json={"msgtype": "markdown",
                                       "markdown": {"title": "Second Person", "text": seg}})
            return
        async with httpx.AsyncClient(timeout=30) as c:
            await c.post(target,
                         json={"msgtype": "markdown",
                               "markdown": {"title": "Second Person", "text": text}})

    async def handle_webhook(self, payload: dict) -> None:
        sender = payload.get("senderStaffId", payload.get("senderId", ""))
        is_group = payload.get("conversationType") == "2"
        msg_type = payload.get("msgtype", "text")
        msg_id = payload.get("msgId", "")
        logger.info("钉钉入站消息 type=%s message_id=%s", msg_type, msg_id)
        chat_id = payload.get("sessionWebhook", sender)
        # webhook 模式无应用凭据无法拉取媒体 → 友好降级提示
        if msg_type != "text":
            await self.handle_unsupported(sender, chat_id, msg_id, msg_type,
                                          is_group, hint=MEDIA_HINT)
            return
        await self.on_message(
            platform_user_id=sender, chat_id=chat_id,
            message_id=msg_id,
            text=payload.get("text", {}).get("content", "").strip(), is_group=is_group)
