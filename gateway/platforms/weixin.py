"""微信 ClawBot 适配器（iLink 协议直连长轮询，与 telegram.py 同构）。

- 入站：文本 → 对话；图片 → 多模态对话（CDN AES 解密转 dataURL）；
  文件 → 知识库入库；语音 → 优先取附带转文字当文本，无则降级提示；
  视频 → 降级提示；其余类型明确提示，杜绝静默丢弃
- 出站：sendmessage 必须携带入站 context_token；回复前 sendtyping
  输入状态；超长回复分段 + md 附件直发（iLink 原生支持文件）
- 主动推送：24h context_token 窗口内可推送，过期明确降级
- 持久化：bot_token / baseurl / context_token / update_buf 写入
  credentials（Fernet 加密），重启免扫码、游标续传不重不漏
- 白名单自动回填：whitelist 为空时首条入站自动写入发送者 ID，
  用户无需自查微信用户 ID
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os

from .base import BasePlatformAdapter, MAX_MEDIA_MB
from .ilink_client import (ILinkClient, MSG_FILE, MSG_IMAGE, MSG_TEXT,
                           MSG_VIDEO, MSG_VOICE, extract_media, extract_text,
                           media_aes_key, media_filename, media_url)

logger = logging.getLogger("second_person.weixin")

# 微信单条消息约 4000 字符上限（协议约束），分段留出余量
_WEIXIN_SEG = 3900
# 语音附带转文字的候选字段（随微信版本演进，宽容匹配）
_VOICE_TEXT_KEYS = ("text", "transcript", "recognized_text", "asr_text")
# 入站媒体约束沿用基类：单图 ≤50MB、单消息 ≤5 张
# 媒体 item 中图片 URL 与 AES 密钥的候选键
_MEDIA_URL_KEYS = ("url", "cdn_url", "media_url", "download_url", "file_url")


class WeixinAdapter(BasePlatformAdapter):
    platform_type = "weixin"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.client = ILinkClient(
            bot_token=self.config.get("bot_token", ""),
            base_url=self.config.get("baseurl", "") or None)
        self._context_token = self.config.get("context_token", "") or ""
        self._update_buf = self.config.get("update_buf", "") or ""
        self._typing_ticket = ""
        # 凭证持久化句柄（由 AdapterManager 注入，None 时不落库）
        self._credential_id = self.config.get("_credential_id")
        self._creds = self.config.get("_creds")
        self._task: asyncio.Task | None = None
        self._running = False

    # ---- 生命周期 ---------------------------------------------------------
    async def connect(self) -> None:
        if not self.client.connected:
            raise RuntimeError("微信渠道未绑定：请先在设置页完成扫码绑定")
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("微信渠道长轮询已启动")

    async def disconnect(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
        await self.client.aclose()

    # ---- 出站 -----------------------------------------------------------
    async def send_message(self, chat_id: str, text: str) -> None:
        if not self._context_token:
            raise RuntimeError("微信主动推送不可用：24 小时会话窗口已过期，"
                               "请在微信中先给 Bot 发一条消息")
        # 提取 MEDIA: 附件标记（基类 _send_reply 生成的超长附件）
        media_path = None
        if "MEDIA:" in text:
            lines = text.splitlines()
            text = "\n".join(l for l in lines if not l.startswith("MEDIA:"))
            media_path = next((l[6:]
                              for l in lines if l.startswith("MEDIA:")), None)
        # 协议单条上限兜底分段（即使 im_max_chars 调大也不越界）
        if text:
            for i in range(0, len(text), _WEIXIN_SEG):
                seg = text[i:i + _WEIXIN_SEG]
                if i > 0:
                    seg = f"（续前）\n{seg}"
                logger.debug("iLink sendmessage → to=%s seg_len=%d", chat_id, len(seg))
                resp = await self.client.send_message(
                    chat_id, self._context_token,
                    [{"type": MSG_TEXT, "text_item": {"text": seg}}])
                logger.debug("iLink sendmessage ← %s", resp)
        # 附件：iLink 原生支持文件，真实送达（优于企微 webhook 的提示兜底）
        if media_path and os.path.exists(media_path):
            await self._send_file(chat_id, media_path)

    async def _send_file(self, chat_id: str, path: str) -> None:
        with open(path, "rb") as fp:
            content = fp.read()
        ref = await self.client.upload_media(content)
        item = {"type": MSG_FILE,
                "file_item": {"file_name": os.path.basename(path),
                              "aes_key": ref["aes_key"], "cdn_ref": ref["cdn_ref"]}}
        await self.client.send_message(chat_id, self._context_token, [item])

    # ---- 入站长轮询 -------------------------------------------------------
    async def _poll_loop(self) -> None:
        while self._running:
            # 熔断 paused 期间挂起而非退出：手动恢复（resume/reload）后自动继续
            if self.paused:
                await asyncio.sleep(5)
                continue
            try:
                msgs, new_buf = await self.client.get_updates(self._update_buf)
                if new_buf != self._update_buf:
                    self._update_buf = new_buf
                for msg in msgs:
                    try:
                        await self._dispatch(msg)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:  # noqa: BLE001
                        logger.exception("微信入站消息处理失败")
                        self._record_failure(str(e))
                # 有新消息的轮次才持久化游标与会话 token（避免高频写库）
                if msgs:
                    self._persist()
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.warning("微信轮询异常：%s", e)
                await asyncio.sleep(5)

    async def _dispatch(self, msg: dict) -> None:
        from_user_id = msg.get("from_user_id", "")
        context_token = msg.get("context_token", "")
        if not from_user_id or not context_token:
            logger.warning("微信消息缺少用户/会话标识，跳过：%s", msg)
            return
        self._context_token = context_token
        # 白名单自动回填：用户无需自查微信用户 ID（xxx@im.wechat）
        if not self.whitelist_user_id:
            self.whitelist_user_id = from_user_id
            self.db.execute(
                "UPDATE platforms SET whitelist_user_id=? WHERE id=?",
                (from_user_id, self.platform_id))
            logger.info("微信首条入站自动回填白名单：%s", from_user_id)
        # 回复前发送"正在输入"（typing_ticket 缺失时先获取，失败忽略）
        asyncio.create_task(self._notify_typing(context_token))
        msg_type, item = extract_media(msg)
        if msg_type is None:
            text = extract_text(msg)
            if not text:
                await self.handle_unsupported(
                    from_user_id, from_user_id, context_token,
                    f"type_{msg.get('message_type')}")
                return
            await self.on_message(from_user_id, from_user_id, context_token, text)
            return
        if msg_type == MSG_IMAGE:
            await self._handle_image(msg, from_user_id, context_token, item)
        elif msg_type == MSG_FILE:
            await self._handle_file(msg, from_user_id, context_token, item)
        elif msg_type == MSG_VOICE:
            await self._handle_voice(msg, from_user_id, context_token, item)
        else:
            await self.handle_unsupported(
                from_user_id, from_user_id, context_token,
                f"type_{msg_type}", is_group=False,
                hint="当前微信渠道暂不支持该消息类型（支持：文字 / 图片 / 文件）")

    async def _notify_typing(self, context_token: str) -> None:
        if not self._typing_ticket:
            try:
                cfg = await self.client.get_config()
                self._typing_ticket = (cfg.get("typing_ticket")
                                       or cfg.get("ticket") or "")
            except Exception as e:  # noqa: BLE001
                logger.debug("微信 getconfig 失败（忽略）：%s", e)
        if self._typing_ticket:
            await self.client.send_typing(context_token, self._typing_ticket)

    async def _handle_image(self, msg: dict, from_user_id: str,
                            context_token: str, item: dict) -> None:
        """图片 → 多模态对话（media_fetch 懒回调：去重通过后才下载解密）。"""
        url = media_url(item)
        aes_key = media_aes_key(item)
        if not url or not aes_key:
            await self.send_message(from_user_id, "图片下载失败（媒体引用缺失），请重新发送")
            return
        caption = extract_text(msg)
        await self.on_message(
            from_user_id, from_user_id, context_token,
            caption or "（图片）",
            media_fetch=lambda: self._fetch_image(url, aes_key))

    async def _fetch_image(self, url: str, aes_key: str) -> list[str]:
        content = await self.client.download_media(url, aes_key)
        if len(content) > MAX_MEDIA_MB * 1024 * 1024:
            raise ValueError(f"媒体超过 {MAX_MEDIA_MB}MB 上限")
        mime = mimetypes.guess_type(url)[0] or "image/jpeg"
        return [f"data:{mime};base64,{base64.b64encode(content).decode()}"]

    async def _handle_file(self, msg: dict, from_user_id: str,
                           context_token: str, item: dict) -> None:
        """文件 → 下载解密后触发文档 Ingest（知识库入库，与飞书对齐）。"""
        url = media_url(item)
        aes_key = media_aes_key(item)
        if not url or not aes_key:
            await self.send_message(from_user_id, "文件下载失败（媒体引用缺失），请重新发送")
            return
        try:
            filename = media_filename(item, f"weixin_{context_token[:8]}.bin")
            content = await self.client.download_media(url, aes_key)
            await self.handle_inbound_file(
                from_user_id, from_user_id, context_token, filename, content)
        except Exception as e:  # noqa: BLE001
            logger.warning("微信文件下载失败：%s", e)

    async def _handle_voice(self, msg: dict, from_user_id: str,
                            context_token: str, item: dict) -> None:
        """语音：优先取附带转文字当文本，无则降级提示。"""
        text = ""
        for key in _VOICE_TEXT_KEYS:
            sub = item.get("voice_item") or item.get("audio_item") or item
            if isinstance(sub, dict) and sub.get(key):
                text = str(sub[key]).strip()
                break
        if text:
            await self.on_message(from_user_id, from_user_id, context_token, text)
            return
        await self.handle_unsupported(
            from_user_id, from_user_id, context_token, "type_voice",
            hint="暂不支持语音消息（该语音未附带转文字），请改用文字发送")

    # ---- 状态持久化 ---------------------------------------------------------
    def _persist(self) -> None:
        """update_buf / context_token 写入凭证（Fernet 加密，重启恢复）。"""
        if not self._credential_id or not self._creds:
            return
        payload = json.dumps({
            "bot_token": self.client.bot_token,
            "baseurl": self.client.base_url,
            "context_token": self._context_token,
            "update_buf": self._update_buf,
        }, ensure_ascii=False)
        try:
            self._creds.update(self._credential_id, payload)
        except Exception as e:  # noqa: BLE001
            logger.warning("微信凭证持久化失败（重启后将需重新扫码）：%s", e)
