"""飞书 Bot 适配器（官方 SDK 长连接接入，本地部署无需公网回调 URL）。

- 入站：lark-oapi WebSocket 长连接在独立线程接收 im.message.receive_v1 事件，
  回调里只解析轻量元数据后桥接回主事件循环；媒体下载等重操作在主循环
  异步完成，避免阻塞 WS 线程导致心跳超时断连
- 消息类型：text/post（图文）→ 多模态对话；image → 多模态对话；
  file → 文档入知识库（图片文件改走多模态）；其余类型明确提示不支持
- 出站：流式交付 —— 先发“思考中”卡片，节流更新卡片实现打字机效果，
  结束后收敛为最终答案（与网页端思考完成自动折叠一致）
- token 失效自动刷新重试一次；handle_webhook 保留兼容公网回调接入
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading

import httpx

from .base import (BasePlatformAdapter, MAX_IMAGES_PER_MSG, MAX_MEDIA_MB)

# 顶层预导入 SDK（进程启动阶段完成）：大包首次导入是 CPU 密集操作，
# 若留在 ws 线程运行期执行会长时间占用 GIL，饿死主事件循环（实测阻塞 21s+）
try:
    import lark_oapi as lark
    import lark_oapi.ws.client as ws_client_mod
except ImportError:  # SDK 未安装：适配器仍可导入，连接时报错提示
    lark = None
    ws_client_mod = None

logger = logging.getLogger("second_person.adapter")


class FeishuAdapter(BasePlatformAdapter):
    platform_type = "feishu"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.app_id = self.config.get("bot_token", "")
        self.app_secret = self.config.get("app_secret", "")
        self._token = None
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._ws_thread: threading.Thread | None = None
        self._stopping = False

    async def connect(self) -> None:
        await self._refresh_token()
        if not self._token:
            raise RuntimeError(
                "飞书鉴权失败：无法获取 tenant_access_token，请检查 App ID / App Secret")
        self._main_loop = asyncio.get_running_loop()
        self._start_ws()

    async def disconnect(self) -> None:
        self._stopping = True
        ws_loop = self._ws_loop
        if ws_loop and ws_loop.is_running():
            ws_loop.call_soon_threadsafe(ws_loop.stop)
        if self._ws_thread:
            self._ws_thread.join(timeout=5)
        self._ws_thread = None
        self._ws_loop = None

    # ---- 长连接（WebSocket）事件接收 --------------------------------------
    def _start_ws(self) -> None:
        self._stopping = False
        self._ws_thread = threading.Thread(
            target=self._ws_worker, name="feishu-ws", daemon=True)
        self._ws_thread.start()

    def _ws_worker(self) -> None:
        """独立线程运行 SDK 长连接（start() 为阻塞式，内部使用模块级 loop）。"""
        try:
            if ws_client_mod is None:
                raise RuntimeError(
                    "lark-oapi 未安装，无法建立飞书长连接（pip install lark-oapi）")
            ws_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(ws_loop)
            self._ws_loop = ws_loop
            ws_client_mod.loop = ws_loop  # SDK 模块级 loop 须绑定到本线程
            handler = lark.EventDispatcherHandler.builder("", "") \
                .register_p2_im_message_receive_v1(self._on_ws_message) \
                .build()
            client = ws_client_mod.Client(
                self.app_id, self.app_secret,
                event_handler=handler, log_level=lark.LogLevel.INFO)
            logger.info("飞书长连接启动（app_id=%s）", self.app_id)
            client.start()
        except Exception:  # noqa: BLE001
            if not self._stopping:
                logger.exception("飞书长连接异常退出")

    def _on_ws_message(self, data) -> None:
        """SDK 回调线程：只解析轻量元数据，桥接回主事件循环统一分流
        （媒体下载在主循环协程内异步完成，不阻塞 WS 线程）。"""
        try:
            msg = data.event.message
            sender_id = data.event.sender.sender_id
            coro = self._handle_inbound(
                msg_type=msg.message_type or "",
                raw_content=msg.content or "{}",
                platform_user_id=sender_id.open_id or "",
                message_id=msg.message_id or "",
                is_group=(msg.chat_type == "group"))
            if self._main_loop and not self._main_loop.is_closed():
                asyncio.run_coroutine_threadsafe(coro, self._main_loop)
            else:
                coro.close()
        except Exception:  # noqa: BLE001
            logger.exception("飞书长连接消息解析失败")

    # ---- 入站分流（主事件循环内执行） ----------------------------------
    async def _handle_inbound(self, msg_type: str, raw_content: str,
                              platform_user_id: str, message_id: str,
                              is_group: bool) -> None:
        """text/post → 对话（post 带图走多模态）；image → 多模态对话；
        file → 文档入知识库；其余类型明确提示不支持。"""
        logger.info("飞书入站消息 type=%s message_id=%s", msg_type, message_id)
        try:
            payload = json.loads(raw_content or "{}")
        except ValueError:
            payload = {}
        chat_id = platform_user_id
        try:
            if msg_type == "text":
                text = payload.get("text", "")
                if not text.strip():
                    return
                await self.on_message(
                    platform_user_id=platform_user_id, chat_id=chat_id,
                    message_id=message_id, text=text, is_group=is_group)
            elif msg_type == "post":
                text, image_keys = self._parse_post(payload)
                if not text.strip() and not image_keys:
                    return
                fetch = ((lambda: self._fetch_images(message_id, image_keys))
                         if image_keys else None)
                await self.on_message(
                    platform_user_id=platform_user_id, chat_id=chat_id,
                    message_id=message_id, text=text.strip() or "（图片）",
                    is_group=is_group, media_fetch=fetch)
            elif msg_type == "image":
                key = payload.get("image_key", "")
                if not key:
                    return
                await self.on_message(
                    platform_user_id=platform_user_id, chat_id=chat_id,
                    message_id=message_id, text="（图片）", is_group=is_group,
                    media_fetch=lambda: self._fetch_images(message_id, [key]))
            elif msg_type == "file":
                await self._handle_file(platform_user_id, chat_id,
                                        message_id, payload, is_group)
            else:
                await self.handle_unsupported(platform_user_id, chat_id,
                                              message_id, msg_type, is_group)
        except Exception:  # noqa: BLE001
            logger.exception("飞书入站消息处理失败 type=%s", msg_type)

    @staticmethod
    def _parse_post(payload: dict) -> tuple[str, list[str]]:
        """解析富文本：拼接全部文字段，收集全部图片 image_key。"""
        texts: list[str] = []
        image_keys: list[str] = []
        title = (payload.get("title") or "").strip()
        if title:
            texts.append(title)
        for para in payload.get("content") or []:
            line: list[str] = []
            for seg in para or []:
                tag = seg.get("tag", "")
                if tag in ("text", "a", "md"):
                    line.append(seg.get("text", ""))
                elif tag == "img" and seg.get("image_key"):
                    image_keys.append(seg["image_key"])
            if line:
                texts.append("".join(line))
        return "\n".join(texts), image_keys[:MAX_IMAGES_PER_MSG]

    # ---- 媒体下载（主循环异步，token 失效刷新重试一次） ----------------
    async def _download_resource(self, c: httpx.AsyncClient, message_id: str,
                                 key: str, rtype: str) -> tuple[bytes, str]:
        """下载消息内媒体资源，返回 (内容, mime)。失败抛异常由上层提示。"""
        if not self._token:
            await self._refresh_token()
        url = (f"https://open.feishu.cn/open-apis/im/v1/messages/"
               f"{message_id}/resources/{key}?type={rtype}")
        r = None
        for attempt in (0, 1):
            r = await c.get(url, headers={"Authorization": f"Bearer {self._token}"})
            ct = (r.headers.get("content-type") or "").split(";")[0].strip()
            # 成功时返回二进制流；鉴权/业务错误返回 application/json
            if r.status_code == 200 and ct != "application/json":
                if len(r.content) > MAX_MEDIA_MB * 1024 * 1024:
                    raise ValueError(f"媒体超过 {MAX_MEDIA_MB}MB 上限")
                return r.content, ct or "application/octet-stream"
            if attempt == 0:
                await self._refresh_token()
        raise RuntimeError(f"飞书媒体下载失败：HTTP {r.status_code}")

    async def _fetch_images(self, message_id: str, keys: list[str]) -> list[str]:
        """下载图片并转 dataURL（供 on_message 懒回调，去重通过后才执行）。"""
        import base64
        out: list[str] = []
        async with httpx.AsyncClient(timeout=60) as c:
            for key in keys[:MAX_IMAGES_PER_MSG]:
                content, mime = await self._download_resource(
                    c, message_id, key, "image")
                if not mime.startswith("image/"):
                    mime = "image/png"
                out.append(f"data:{mime};base64,"
                           f"{base64.b64encode(content).decode()}")
        return out

    async def _handle_file(self, platform_user_id: str, chat_id: str,
                           message_id: str, payload: dict, is_group: bool) -> None:
        """文件消息：图片文件 → 多模态对话；文档 → 知识库提炼入库。
        下载前先过群聊/白名单/去重与扩展名校验，避免无效下载。"""
        if is_group:
            return
        if self.whitelist_user_id and platform_user_id != self.whitelist_user_id:
            return
        if message_id and self._is_duplicate(message_id):
            return
        import os
        filename = payload.get("file_name") or ""
        file_key = payload.get("file_key", "")
        ext = os.path.splitext(filename)[1].lower()
        if not file_key or ext not in self.SUPPORTED_FILE_EXT:
            await self.handle_unsupported(
                platform_user_id, chat_id, message_id, "file", is_group,
                hint=f"文件类型 {ext or '未知'} 暂不支持解析")
            return
        try:
            async with httpx.AsyncClient(timeout=120) as c:
                content, _ = await self._download_resource(
                    c, message_id, file_key, "file")
        except Exception as e:  # noqa: BLE001
            logger.warning("飞书文件下载失败 message_id=%s: %s", message_id, e)
            await self.send_message(chat_id, "文件下载失败，请重新发送")
            return
        if ext in self.IMAGE_EXTS:
            # 图片文件与图片消息语义对齐：走多模态对话
            import base64
            import mimetypes
            mime = mimetypes.guess_type(filename)[0] or "image/png"
            du = f"data:{mime};base64,{base64.b64encode(content).decode()}"
            await self.on_message(
                platform_user_id=platform_user_id, chat_id=chat_id,
                message_id=message_id, text="（图片）", is_group=is_group,
                images=[du])
        else:
            await self.handle_inbound_file(
                platform_user_id=platform_user_id, chat_id=chat_id,
                message_id=message_id, filename=filename,
                content=content, is_group=is_group)

    # ---- 出站发送 ----------------------------------------------------------
    async def _refresh_token(self) -> None:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret})
            data = r.json()
            self._token = data.get("tenant_access_token")
            if not self._token:
                logger.warning("飞书获取 tenant_access_token 失败：%s",
                               data.get("msg"))

    async def _post_text(self, c: httpx.AsyncClient, chat_id: str, text: str) -> dict:
        r = await c.post(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
            headers={"Authorization": f"Bearer {self._token}"},
            json={"receive_id": chat_id, "msg_type": "text",
                  "content": json.dumps({"text": text})})
        try:
            return r.json()
        except ValueError:
            return {"code": -1, "msg": f"HTTP {r.status_code}"}

    async def send_message(self, chat_id: str, text: str) -> None:
        # 提取 MEDIA: 附件标记 → 上传飞书文件
        media_path = None
        if "MEDIA:" in text:
            lines = text.splitlines()
            text = "\n".join(l for l in lines if not l.startswith("MEDIA:"))
            media_path = next((l[6:]
                              for l in lines if l.startswith("MEDIA:")), None)
        if not self._token:
            await self._refresh_token()
        async with httpx.AsyncClient(timeout=30) as c:
            if text.strip():
                resp = await self._post_text(c, chat_id, text)
                if resp.get("code") != 0:
                    # token 过期/失效 → 刷新后重试一次
                    await self._refresh_token()
                    resp = await self._post_text(c, chat_id, text)
                    if resp.get("code") != 0:
                        logger.warning("飞书消息发送失败：code=%s msg=%s",
                                       resp.get("code"), resp.get("msg"))
            # 上传文件附件
            if media_path:
                try:
                    from pathlib import Path
                    fp = Path(media_path)
                    if fp.exists():
                        with open(media_path, "rb") as f:
                            await c.post(
                                "https://open.feishu.cn/open-apis/im/v1/files",
                                headers={
                                    "Authorization": f"Bearer {self._token}"},
                                files={"file": (fp.name, f)},
                                data={"file_type": "stream", "file_name": fp.name})
                except Exception:  # noqa: BLE001
                    logger.warning("飞书附件上传失败：%s", media_path)

    # ---- 流式交付（消息卡片打字机效果） -----------------------------------
    STREAM_INTERVAL = 1.2       # 卡片更新节流（秒），避开飞书更新频控
    THINKING_MAX_BYTES = 20000  # 飞书卡片 content 硬上限约 30KB，预留余量

    @staticmethod
    def _card(md: str) -> str:
        return json.dumps({
            "config": {"wide_screen_mode": True, "update_multi": True},
            "elements": [{"tag": "markdown", "content": md}]},
            ensure_ascii=False)

    async def _send_card(self, c: httpx.AsyncClient, chat_id: str, md: str) -> str | None:
        data = {}
        for attempt in (0, 1):
            r = await c.post(
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
                headers={"Authorization": f"Bearer {self._token}"},
                json={"receive_id": chat_id, "msg_type": "interactive",
                      "content": self._card(md)})
            try:
                data = r.json()
            except ValueError:
                data = {"code": -1, "msg": f"HTTP {r.status_code}"}
            if data.get("code") == 0:
                return (data.get("data") or {}).get("message_id")
            if attempt == 0:
                await self._refresh_token()
        logger.warning("飞书卡片发送失败：code=%s msg=%s",
                       data.get("code"), data.get("msg"))
        return None

    async def _update_card(self, c: httpx.AsyncClient, message_id: str, md: str) -> None:
        for attempt in (0, 1):
            r = await c.patch(
                f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}",
                headers={"Authorization": f"Bearer {self._token}"},
                json={"content": self._card(md)})
            try:
                data = r.json()
            except ValueError:
                data = {"code": -1, "msg": f"HTTP {r.status_code}"}
            if data.get("code") == 0:
                return
            if attempt == 0:
                await self._refresh_token()
        # 中途刷新失败非致命（下一次节流更新会补齐），仅记录
        logger.debug("飞书卡片更新失败：code=%s msg=%s",
                     data.get("code"), data.get("msg"))

    @staticmethod
    def _tail_by_bytes(s: str, max_bytes: int) -> str:
        """按 UTF-8 字节数保留尾部（仅在逼近卡片硬上限时才截断）。"""
        b = s.encode("utf-8")
        if len(b) <= max_bytes:
            return s
        return b[-max_bytes:].decode("utf-8", errors="ignore")

    def _render_stream(self, thinking: list[str], content: list[str]) -> str:
        """流式中间态：有正文时展示正文+光标；否则全量展示灰色思考过程。"""
        body = "".join(content)
        if body.strip():
            return body[: self.im_max_chars] + " ▍"
        md = "🤔 **思考中…**"
        th = "".join(thinking).strip()
        if th:
            shown = self._tail_by_bytes(th, self.THINKING_MAX_BYTES)
            if len(shown) < len(th):
                shown = "（思考过程较长，已省略前部）……" + shown
            # 逐行包 font 标签：保留段落结构的同时保证灰色样式正常渲染
            lines = [f"<font color='grey'>{l}</font>"
                     for l in shown.splitlines() if l.strip()]
            md += "\n" + "\n".join(lines)
        return md

    async def _deliver(self, chat_id: str, sid: str, text: str,
                       images: list[str] | None = None) -> None:
        """流式交付：思考过程与正文增量实时刷到同一张卡片上。"""
        import time
        if not self._token:
            await self._refresh_token()
        async with httpx.AsyncClient(timeout=30) as c:
            msg_id = await self._send_card(c, chat_id, "🤔 **思考中…**")
            if not msg_id:
                # 卡片发送失败 → 回退为收齐后一次性文本
                await super()._deliver(chat_id, sid, text, images=images)
                return
            thinking, content, err_msg = [], [], ""
            last, dirty = 0.0, False
            async for evt in self.core.run(sid, text, images=images):
                if evt["event"] == "thinking_delta":
                    thinking.append(evt["data"].get("text", ""))
                    dirty = True
                elif evt["event"] == "content_delta":
                    content.append(evt["data"].get("text", ""))
                    dirty = True
                elif evt["event"] == "error":
                    err_msg = evt["data"].get("message", "处理失败")
                now = time.monotonic()
                if dirty and now - last >= self.STREAM_INTERVAL:
                    await self._update_card(
                        c, msg_id, self._render_stream(thinking, content))
                    last, dirty = now, False
            # 收敛为最终答案（思考过程隐去，与网页端自动折叠一致）
            reply = "".join(content)
            if len(reply) > self.im_max_chars and self.data_dir:
                from pathlib import Path
                from memory.naming import im_attachment_name
                from infrastructure.observability import get_trace_id
                fname = im_attachment_name(get_trace_id() or "im")
                fpath = Path(self.data_dir) / "temp" / "attachments" / fname
                fpath.parent.mkdir(parents=True, exist_ok=True)
                fpath.write_text(reply, encoding="utf-8")
                await self._update_card(c, msg_id, "回复内容较长，已作为附件发送")
                await self.send_message(chat_id, f"MEDIA:{fpath}")
            else:
                await self._update_card(
                    c, msg_id, reply.strip() or err_msg or "（本次未生成回复内容）")

    # ---- 公网回调兼容入口 ----------------------------------------------------
    async def handle_webhook(self, payload: dict) -> None:
        """由 /api/im/webhook/feishu 路由调用（公网回调 URL 接入方式），
        与长连接共用 _handle_inbound 统一分流。"""
        event = payload.get("event", {})
        msg = event.get("message", {})
        sender = event.get("sender", {}).get("sender_id", {})
        await self._handle_inbound(
            msg_type=msg.get("message_type", ""),
            raw_content=msg.get("content", "{}"),
            platform_user_id=sender.get("open_id", ""),
            message_id=msg.get("message_id", ""),
            is_group=(msg.get("chat_type") == "group"))
