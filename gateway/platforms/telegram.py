"""Telegram Bot 适配器（long-poll 参考实现）。

入站：text → 对话；photo/图片文件 → 多模态对话（caption 作文字）；
文档 → 知识库提炼入库；其余类型明确提示不支持。
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from .base import BasePlatformAdapter, MAX_MEDIA_MB
from .media_parser import split_media_marker

logger = logging.getLogger("second_person.telegram")


class TelegramAdapter(BasePlatformAdapter):
    platform_type = "telegram"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.token = self.config.get("bot_token", "")
        self._offset = 0
        self._task: asyncio.Task | None = None
        self._running = False
        self._http: httpx.AsyncClient | None = None

    @property
    def _api(self) -> str:
        return f"https://api.telegram.org/bot{self.token}"

    async def connect(self) -> None:
        self._running = True
        self._http = httpx.AsyncClient(timeout=60)
        self._task = asyncio.create_task(self._poll_loop())

    async def disconnect(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
        if self._http:
            await self._http.aclose()
            self._http = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=60)
        return self._http

    async def send_message(self, chat_id: str, text: str) -> None:
        text, media_path = split_media_marker(text)
        c = self._client()
        await c.post(f"{self._api}/sendMessage",
                     json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})
        if media_path:
            with open(media_path, "rb") as fp:
                await c.post(f"{self._api}/sendDocument",
                             data={"chat_id": chat_id},
                             files={"document": fp})

    async def _poll_loop(self) -> None:
        while self._running:
            # 熔断 paused 期间挂起而非退出：手动恢复（resume/reload）后自动继续轮询
            if self.paused:
                await asyncio.sleep(5)
                continue
            try:
                c = self._client()
                r = await c.get(f"{self._api}/getUpdates",
                                params={"offset": self._offset, "timeout": 30},
                                timeout=35)
                updates = r.json().get("result", [])
                for u in updates:
                    self._offset = u["update_id"] + 1
                    msg = u.get("message")
                    if not msg:
                        continue
                    chat = msg["chat"]
                    is_group = chat.get("type") in ("group", "supergroup")
                    msg_type = next((k for k in ("text", "photo", "document",
                                                 "voice", "video", "sticker",
                                                 "audio") if k in msg), "unknown")
                    logger.info("Telegram 入站消息 type=%s message_id=%s",
                                msg_type, msg.get("message_id"))
                    # 图片：走多模态对话（caption 作文字）
                    photo = msg.get("photo")
                    if photo:
                        await self._handle_photo(msg, chat, photo, is_group)
                        continue
                    # 入站文档：图片文件走多模态，其余下载后触发 Ingest
                    doc = msg.get("document")
                    if doc:
                        await self._handle_document(msg, chat, doc, is_group)
                        continue
                    if "text" not in msg:
                        await self.handle_unsupported(
                            platform_user_id=str(msg["from"]["id"]),
                            chat_id=str(chat["id"]),
                            message_id=str(msg["message_id"]),
                            msg_type=msg_type, is_group=is_group)
                        continue
                    await self.on_message(
                        platform_user_id=str(msg["from"]["id"]),
                        chat_id=str(chat["id"]), message_id=str(msg["message_id"]),
                        text=msg["text"], is_group=is_group)
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.warning("Telegram 轮询异常：%s", e)
                await asyncio.sleep(5)

    async def _download_file(self, file_id: str) -> tuple[bytes, str]:
        """getFile → 下载，返回 (内容, file_path)。失败抛异常由上层提示。"""
        c = self._client()
        r = await c.get(f"{self._api}/getFile", params={"file_id": file_id})
        file_path = r.json().get("result", {}).get("file_path")
        if not file_path:
            raise RuntimeError("Telegram getFile 未返回 file_path")
        fr = await c.get(
            f"https://api.telegram.org/file/bot{self.token}/{file_path}")
        if len(fr.content) > MAX_MEDIA_MB * 1024 * 1024:
            raise ValueError(f"媒体超过 {MAX_MEDIA_MB}MB 上限")
        return fr.content, file_path

    async def _fetch_photo(self, file_id: str) -> list[str]:
        """下载图片转 dataURL（供 on_message 懒回调，去重通过后才执行）。"""
        import base64
        import mimetypes
        content, file_path = await self._download_file(file_id)
        mime = mimetypes.guess_type(file_path)[0] or "image/jpeg"
        return [f"data:{mime};base64,{base64.b64encode(content).decode()}"]

    async def _handle_photo(self, msg: dict, chat: dict, photo: list,
                            is_group: bool) -> None:
        """图片消息 → 多模态对话（取最大尺寸，caption 作文字）。"""
        file_id = (photo[-1] or {}).get("file_id")
        if not file_id:
            return
        caption = (msg.get("caption") or "").strip()
        await self.on_message(
            platform_user_id=str(msg["from"]["id"]),
            chat_id=str(chat["id"]), message_id=str(msg["message_id"]),
            text=caption or "（图片）", is_group=is_group,
            media_fetch=lambda: self._fetch_photo(file_id))

    async def _handle_document(self, msg: dict, chat: dict, doc: dict, is_group: bool) -> None:
        """下载 Telegram 文档：图片文件 → 多模态对话（与全局语义对齐）；
        其余 → 触发入站文件 Ingest。"""
        try:
            import os
            file_id = doc.get("file_id")
            filename = doc.get("file_name") or f"{file_id}.bin"
            ext = os.path.splitext(filename)[1].lower()
            if ext in self.IMAGE_EXTS:
                caption = (msg.get("caption") or "").strip()
                await self.on_message(
                    platform_user_id=str(msg["from"]["id"]),
                    chat_id=str(chat["id"]),
                    message_id=str(msg["message_id"]),
                    text=caption or "（图片）", is_group=is_group,
                    media_fetch=lambda: self._fetch_photo(file_id))
                return
            content, _ = await self._download_file(file_id)
            await self.handle_inbound_file(
                platform_user_id=str(msg["from"]["id"]),
                chat_id=str(chat["id"]), message_id=str(msg["message_id"]),
                filename=filename, content=content, is_group=is_group)
        except Exception as e:  # noqa: BLE001
            logger.warning("Telegram 文档下载失败：%s", e)
