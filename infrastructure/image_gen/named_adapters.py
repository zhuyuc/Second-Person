"""图片的 Anthropic / Google：协议名决定接口，不按地址猜厂商。"""
from __future__ import annotations

import base64
import logging
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable

import httpx

from .types import ImageGenRequest, ImageGenResult

logger = logging.getLogger("second_person.image_gen.named")

ProgressCb = Callable[[str, str], Awaitable[None]] | None


def _anthropic_headers(api_key: str) -> dict[str, str]:
    return {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }


async def probe_anthropic(base_url: str, api_key: str, model_id: str,
                          timeout: float = 12.0) -> dict:
    """和文本一样打 {地址}/messages，不改成 OpenAI 路径。"""
    url = f"{(base_url or '').rstrip('/')}/messages"
    body = {
        "model": model_id or "claude",
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                url, json=body, headers=_anthropic_headers(api_key))
    except httpx.ConnectError:
        return {"ok": False, "error": "无法连接 Anthropic 接口"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}
    if resp.status_code < 400:
        return {"ok": True, "protocol": "anthropic"}
    if resp.status_code in (401, 403):
        return {"ok": False, "error": "鉴权失败，请检查 API Key"}
    detail = (resp.text or "")[:180]
    return {"ok": False, "error": f"Anthropic 接口返回 HTTP {resp.status_code}：{detail}"}


class AnthropicImageAdapter:
    def __init__(self, *, base_url: str, api_key: str, data_dir: Path,
                 timeout_sec: float = 120.0, model_id: str = "") -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.data_dir = Path(data_dir)
        self.timeout_sec = timeout_sec
        self.model_id = model_id or ""

    async def probe(self, timeout: float = 12.0) -> dict:
        return await probe_anthropic(self.base_url, self.api_key, self.model_id, timeout)

    async def generate(self, req: ImageGenRequest, **_kw) -> ImageGenResult:
        raise RuntimeError(
            "Anthropic 没有图片生成接口。生图请改选 OpenAI 兼容、Google 或自定义")


def _inline_image(payload: dict) -> bytes:
    parts = []
    for cand in payload.get("candidates") or []:
        content = (cand or {}).get("content") or {}
        parts.extend(content.get("parts") or [])
    for part in parts:
        if not isinstance(part, dict):
            continue
        inline = part.get("inlineData") or part.get("inline_data") or {}
        data = inline.get("data") if isinstance(inline, dict) else ""
        if data:
            return base64.b64decode(data)
    return b""


class GoogleImageAdapter:
    """与文本 Google 同一条 generateContent，从返回里取图片。"""

    def __init__(self, *, base_url: str, api_key: str, data_dir: Path,
                 timeout_sec: float = 120.0, model_id: str = "") -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.data_dir = Path(data_dir)
        self.timeout_sec = timeout_sec
        self.model_id = model_id or ""

    def _url(self, model_name: str) -> str:
        return (
            f"{self.base_url}/models/{model_name}:generateContent"
            f"?key={self.api_key}"
        )

    async def probe(self, timeout: float = 12.0) -> dict:
        url = f"{self.base_url}/models?key={self.api_key}"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url)
        except httpx.ConnectError:
            return {"ok": False, "error": "无法连接 Google 接口"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:200]}
        if resp.status_code < 400:
            return {"ok": True, "protocol": "google"}
        if resp.status_code in (401, 403):
            return {"ok": False, "error": "鉴权失败，请检查 API Key"}
        return {"ok": False, "error": f"Google 接口返回 HTTP {resp.status_code}"}

    async def generate(
        self,
        req: ImageGenRequest,
        *,
        provider_id: str = "",
        model_id: str = "",
        session_id: str = "",
        on_progress: ProgressCb = None,
    ) -> ImageGenResult:
        t0 = time.perf_counter()
        model_name = (model_id or req.model_id or self.model_id).strip()
        if on_progress:
            await on_progress("submit", "正在提交 Google 生图…")
        body = {
            "contents": [{"role": "user", "parts": [{"text": (req.prompt or "")[:4000]}]}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
        }
        timeout = httpx.Timeout(max(120.0, float(self.timeout_sec)), connect=20.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(self._url(model_name), json=body)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Google 生图失败 HTTP {resp.status_code}：{(resp.text or '')[:240]}")
        payload = resp.json() if resp.content else {}
        img = _inline_image(payload if isinstance(payload, dict) else {})
        if not img:
            raise RuntimeError("Google 接口没有返回图片")
        out_dir = self.data_dir / "chat_images"
        out_dir.mkdir(parents=True, exist_ok=True)
        fname = f"gen_{uuid.uuid4().hex[:12]}.png"
        (out_dir / fname).write_bytes(img)
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return ImageGenResult(
            type="generated_image",
            filenames=[fname],
            public_urls=[f"/chat-images/{fname}"],
            n=1,
            revised_prompt=req.prompt,
            size=req.size,
            provider_id=provider_id,
            model_id=model_name,
            latency_ms=latency_ms,
            backend="cloud",
            summary=f"已生成 1 张图（耗时 {max(1, latency_ms // 1000)}s）",
        )
