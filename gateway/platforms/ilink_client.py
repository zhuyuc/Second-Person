"""微信 iLink 协议客户端（ClawBot 官方通道直连，HTTP/JSON 纯协议层）。

协议背景：微信 ClawBot 插件（2026-03 官方灰度）底层是腾讯官方 iLink
Bot API（ilinkai.weixin.qq.com）——标准 HTTP/JSON 接口，任何客户端均可
直连，无需 OpenClaw 网关。本模块能力：
- 登录：get_bot_qrcode 获取二维码 → get_qrcode_status 轮询确认 → bot_token
- 收消息：getupdates 长轮询（服务端 hold ≤35s），get_updates_buf 游标续传
- 发消息：sendmessage（必须携带入站 context_token 才能关联到正确对话窗口）
- 输入状态：getconfig 获取 typing_ticket → sendtyping 发送"正在输入"
- 媒体：CDN 文件 AES-128-ECB 加密；发送前 getuploadurl 预签名上传
- 认证：Authorization: Bearer <bot_token> + X-WECHAT-UIN（随机防重放）

协议细节基于 @tencent-weixin/openclaw-weixin 源码逆向文档与社区多语言
SDK 交叉验证；媒体 item 的字段结构以真实报文为准，解析采用多候选 key
宽容匹配，缺失字段记录日志而非静默丢弃。
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import random
from typing import Any

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

logger = logging.getLogger("second_person.ilink")

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
# 服务端 hold 最长 35s；httpx 超时需略大于 hold，超时视为空轮继续下一轮
POLL_TIMEOUT = 40.0
# 写操作（sendmessage 等）限流/5xx 指数退避重试
MAX_RETRY = 3

# 消息类型（item_list[].type）
MSG_TEXT = 1
MSG_IMAGE = 2
MSG_VOICE = 3
MSG_FILE = 4
MSG_VIDEO = 5


def _uin_header() -> str:
    """X-WECHAT-UIN：随机 uint32 → 十进制字符串 → base64，每次请求变化防重放。"""
    return base64.b64encode(str(random.getrandbits(32)).encode()).decode()


def _decrypt_aes_ecb(encrypted: bytes, aes_key: str | bytes) -> bytes:
    """AES-128-ECB 解密 CDN 媒体。aes_key 兼容 base64 / hex / 原始 16 字节。

    解密后按 PKCS7 优先剥填充，其次剥 ZeroPadding 尾部零；
    两者都不匹配则原样返回（明文恰好块对齐且尾部非零，如部分媒体）。"""
    raw = aes_key.encode() if isinstance(aes_key, str) else aes_key
    key = None
    for dec in (base64.b64decode, lambda b: bytes.fromhex(b.decode())):
        try:
            k = dec(raw)
            if len(k) == 16:
                key = k
                break
        except Exception:  # noqa: BLE001 - 尝试下一种编码
            continue
    if key is None and len(raw) == 16:
        key = raw
    if key is None:
        raise ValueError("无法解析 AES 密钥（需 16 字节，支持 base64/hex）")
    dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    plain = dec.update(encrypted) + dec.finalize()
    # PKCS7：末字节 1-16 且末尾连续相同字节数与值一致才剥
    if plain and 1 <= plain[-1] <= 16 and \
            plain[-plain[-1]:] == bytes([plain[-1]]) * plain[-1]:
        return plain[:-plain[-1]]
    # ZeroPadding：末字节为 0 时剥除尾部零（图片等媒体尾部非零，误伤概率极低）
    if plain and plain[-1] == 0:
        return plain.rstrip(b"\x00")
    return plain


def _encrypt_aes_ecb(plain: bytes, key: bytes) -> bytes:
    """AES-128-ECB 加密（PKCS7 填充，保证块对齐）。"""
    pad = 16 - len(plain) % 16
    padded = plain + bytes([pad]) * pad
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return enc.update(padded) + enc.finalize()


def extract_text(msg: dict) -> str:
    """提取消息文本（item type=1 的 text_item.text）。"""
    for item in msg.get("item_list") or []:
        if item.get("type") == MSG_TEXT:
            t = item.get("text_item") or {}
            return (t.get("text") or "").strip()
    return ""


def extract_media(msg: dict) -> tuple[int | None, dict]:
    """提取非文本媒体 item，返回 (type, item)；无媒体返回 (None, {})。

    媒体 item 字段结构随微信版本演进，解析做多候选 key 宽容匹配，
    由调用方按候选键（url/cdn_url/media_url、aes_key/key 等）提取。"""
    for item in msg.get("item_list") or []:
        itype = item.get("type")
        if itype and itype != MSG_TEXT:
            return itype, item
    return None, {}


def _media_ref(item: dict, keys: tuple[str, ...]) -> str:
    """从媒体 item 中按候选键提取引用（url / aes_key 等），找不到返回空串。"""
    sub = item.get("media_item") or item.get("image_item") or item.get(
        "file_item") or item.get("video_item") or item.get("audio_item") or item
    if isinstance(sub, dict):
        for k in keys:
            v = sub.get(k)
            if v:
                return str(v)
    return ""


def media_url(item: dict) -> str:
    return _media_ref(item, ("url", "cdn_url", "media_url", "download_url"))


def media_aes_key(item: dict) -> str:
    return _media_ref(item, ("aes_key", "key", "encrypt_key"))


def media_filename(item: dict, default: str) -> str:
    name = _media_ref(item, ("file_name", "filename", "name"))
    return name or default


class ILinkClient:
    """iLink 协议客户端。无状态协议封装：token / 游标由适配器层负责持久化。"""

    def __init__(self, bot_token: str = "", base_url: str = DEFAULT_BASE_URL):
        self.bot_token = bot_token
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._client: httpx.AsyncClient | None = None

    @property
    def connected(self) -> bool:
        return bool(self.bot_token)

    async def _request(self, method: str, path: str, *, json_body: dict | None = None,
                       params: dict | None = None) -> dict:
        """统一请求：认证头 + 防重放头；429/5xx 指数退避重试；
        TimeoutException 直接上抛（长轮询空轮由调用方处理）。"""
        url = f"{self.base_url}{path}"
        headers = {"Content-Type": "application/json",
                   "AuthorizationType": "ilink_bot_token",
                   "X-WECHAT-UIN": _uin_header()}
        if self.bot_token:
            headers["Authorization"] = f"Bearer {self.bot_token}"
        client = self._client or httpx.AsyncClient(timeout=POLL_TIMEOUT)
        if self._client is None:
            self._client = client
        delay = 1
        for attempt in range(MAX_RETRY + 1):
            try:
                r = await client.request(method, url, json=json_body, params=params,
                                         headers=headers)
                if r.status_code in (429,) or r.status_code >= 500:
                    if attempt < MAX_RETRY:
                        logger.warning(
                            "iLink HTTP %s 限流/服务端错误，%.1fs 后重试", r.status_code, delay)
                        await asyncio.sleep(delay)
                        delay *= 2
                        continue
                    raise RuntimeError(
                        f"iLink HTTP {r.status_code}: {r.text[:200]}")
                r.raise_for_status()
                return r.json()
            except httpx.TimeoutException:
                raise
            except (httpx.HTTPStatusError, httpx.RequestError):  # noqa: BLE001
                if attempt < MAX_RETRY:
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                raise
        raise RuntimeError("iLink 请求重试耗尽")  # pragma: no cover

    # ---- 登录 -----------------------------------------------------------
    async def request_qrcode(self) -> dict[str, Any]:
        """获取登录二维码：GET /ilink/bot/get_bot_qrcode?bot_type=3。"""
        return await self._request("GET", "/ilink/bot/get_bot_qrcode",
                                   params={"bot_type": 3})

    async def poll_qrcode(self, qrcode: str) -> dict[str, Any]:
        """轮询扫码状态：GET /ilink/bot/get_qrcode_status?qrcode=xxx。

        服务端为长轮询（hold 直到扫码状态变化）；hold 超时视为
        pending 继续轮询，不视为错误。返回 {status, bot_token?, baseurl?}。"""
        try:
            return await self._request("GET", "/ilink/bot/get_qrcode_status",
                                       params={"qrcode": qrcode})
        except httpx.TimeoutException:
            return {"status": "pending"}

    # ---- 消息收发 ---------------------------------------------------------
    async def get_updates(self, update_buf: str) -> tuple[list[dict], str]:
        """长轮询收消息：POST /ilink/bot/getupdates（hold ≤35s）。
        返回 (msgs, new_buf)；游标必须持久化，重启后续传不重不漏。
        空轮（hold 超时）返回 ([], 原游标)，不视为错误。"""
        body = {"get_updates_buf": update_buf or "",
                "base_info": {"channel_version": "1.0.2"}}
        try:
            data = await self._request("POST", "/ilink/bot/getupdates", json_body=body)
        except httpx.TimeoutException:
            return [], update_buf
        # 业务错误码防御：HTTP 200 但 errcode 非 0（如 session timeout）不得静默吞错
        if data.get("errcode"):
            logger.warning("iLink getupdates 业务错误 errcode=%s errmsg=%s（将重试）",
                           data.get("errcode"), data.get("errmsg"))
            return [], update_buf
        msgs = data.get("msgs") or []
        new_buf = data.get("get_updates_buf") or update_buf
        if msgs:
            logger.info("iLink 收到 %d 条消息", len(msgs))
        return msgs, new_buf

    async def get_config(self) -> dict[str, Any]:
        """获取配置（typing_ticket）：POST /ilink/bot/getconfig。"""
        return await self._request(
            "POST", "/ilink/bot/getconfig",
            json_body={"base_info": {"channel_version": "1.0.2"}})

    async def send_typing(self, context_token: str, typing_ticket: str) -> None:
        """发送"正在输入"状态：POST /ilink/bot/sendtyping。失败不抛（非关键）。"""
        try:
            await self._request("POST", "/ilink/bot/sendtyping", json_body={
                "context_token": context_token, "typing_ticket": typing_ticket})
        except Exception as e:  # noqa: BLE001
            logger.debug("iLink sendtyping 失败（忽略）：%s", e)

    async def send_message(self, to_user_id: str, context_token: str,
                           items: list[dict]) -> dict:
        """发送消息：POST /ilink/bot/sendmessage（必须携带入站 context_token）。
        items 为 item_list，如 [{"type": 1, "text_item": {"text": "你好"}}]。"""
        body = {"msg": {"to_user_id": to_user_id, "message_type": 2,
                        "message_state": 2, "context_token": context_token,
                        "item_list": items}}
        data = await self._request("POST", "/ilink/bot/sendmessage", json_body=body)
        if data.get("errcode"):
            raise RuntimeError(f"iLink sendmessage 失败 errcode={data.get('errcode')} "
                               f"errmsg={data.get('errmsg')}")
        return data

    # ---- 媒体 -----------------------------------------------------------
    async def download_media(self, url: str, aes_key: str) -> bytes:
        """下载并 AES-128-ECB 解密 CDN 媒体。独立长超时客户端（大文件）。"""
        async with httpx.AsyncClient(timeout=120.0) as c:
            r = await c.get(url)
            r.raise_for_status()
        return _decrypt_aes_ecb(r.content, aes_key)

    async def upload_media(self, file_bytes: bytes) -> dict[str, Any]:
        """加密并上传媒体到 CDN：getuploadurl 预签名 → AES 加密 → PUT。
        返回 {aes_key(base64), cdn_ref}，调用方组装媒体 item。"""
        aes_key = os.urandom(16)
        data = await self._request("POST", "/ilink/bot/getuploadurl", json_body={})
        upload_url = (data.get("upload_url") or data.get("url")
                      or data.get("uploadurl") or "")
        if not upload_url:
            raise RuntimeError(f"getuploadurl 未返回上传地址：{data}")
        encrypted = _encrypt_aes_ecb(file_bytes, aes_key)
        async with httpx.AsyncClient(timeout=120.0) as c:
            r = await c.put(upload_url, content=encrypted)
            r.raise_for_status()
        return {"aes_key": base64.b64encode(aes_key).decode(),
                "cdn_ref": data.get("cdn_ref") or data.get("file_ref") or {}}

    async def aclose(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
