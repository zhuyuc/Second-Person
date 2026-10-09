"""火山方舟 Seedream 文生图：POST /api/v3/images/generations。"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable

import httpx

from infrastructure.image_gen import active_jobs
from infrastructure.remote_jobs import JobCancelled, get_store, runtime
from infrastructure.remote_jobs.fetch import fetch_url_bytes
from infrastructure.video_gen.vendor import volcengine_api_root
from infrastructure.video_gen.volcengine_adapter import (
    volcengine_error_text, volcengine_headers,
)

from .types import ImageGenRequest, ImageGenResult

logger = logging.getLogger("second_person.image_gen.volcengine")

ProgressCb = Callable[[str, str], Awaitable[None]] | None


def _seedream_size(size: str) -> str:
    """Seedream 接受 WxH 或 1K/2K；本系统默认方图映射为 2K。"""
    raw = (size or "").strip().lower().replace("*", "x")
    if raw in ("1024x1024", "1024x1792", "1792x1024"):
        return raw
    if raw in ("1k", "2k", "4k"):
        return raw.upper()
    return "2K"


class VolcengineImageAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        data_dir: Path,
        timeout_sec: float = 180.0,
        model_id: str = "",
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_root = volcengine_api_root(self.base_url)
        self.api_key = api_key or ""
        self.data_dir = Path(data_dir)
        self.timeout_sec = timeout_sec
        self.model_id = model_id or ""

    def _headers(self) -> dict[str, str]:
        return volcengine_headers(self.api_key)

    async def probe(self, timeout: float = 12.0) -> dict:
        url = f"{self.api_root}/models"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url, headers=self._headers())
        except httpx.ConnectError:
            return {"ok": False, "error": "无法连接火山方舟图片服务"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:200]}
        if resp.status_code < 400:
            return {"ok": True, "protocol": "volcengine"}
        if resp.status_code in (401, 403):
            return {"ok": False, "error": "鉴权失败，请检查火山 API Key"}
        # /models 偶发 404 仍说明域名可达
        if resp.status_code in (404, 405):
            return {"ok": True, "protocol": "volcengine"}
        detail = volcengine_error_text(
            _safe_json(resp), (resp.text or "")[:160])
        return {
            "ok": False,
            "error": detail or f"火山图片接口返回 HTTP {resp.status_code}",
        }

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
        if not model_name:
            raise RuntimeError(
                "请填写火山图片模型 ID，例如 doubao-seedream-4-0-250828")
        if "seedance" in model_name.lower():
            raise RuntimeError(
                f"模型 {model_name} 是视频（Seedance），文生图请改用 Seedream")
        size = _seedream_size(req.size)
        prompt = (req.prompt or "")[:4000]
        if (req.negative_prompt or "").strip():
            prompt = f"{prompt}\n不要出现：{req.negative_prompt.strip()}"[:4000]
        body = {
            "model": model_name,
            "prompt": prompt,
            "size": size,
            "response_format": "url",
            "watermark": False,
        }
        # Seedream 4.x 用该字段关组图；5.x 不支持，带上会 InvalidParameter
        mid = model_name.lower()
        if "seedream-4" in mid or "seedream_4" in mid:
            body["sequential_image_generation"] = "disabled"
        job_id = active_jobs.register_job(
            session_id, base_url=self.base_url, prompt_id="",
            backend="volcengine", kind="volcengine_image")
        store = get_store()
        if store is not None:
            try:
                store.create(
                    kind="volcengine_image", backend="volcengine",
                    session_id=session_id, base_url=self.base_url,
                    job_id=job_id)
            except Exception:  # noqa: BLE001
                logger.debug("remote_jobs create skip", exc_info=True)
        settle_status = "failed"
        settle_err: str | None = None
        settle_ref: str | None = None
        try:
            runtime.raise_if_cancelled(job_id=job_id, session_id=session_id)
            if on_progress:
                await on_progress("submit", "正在提交火山生图…")
            url = f"{self.api_root}/images/generations"
            post_timeout = httpx.Timeout(
                max(300.0, float(self.timeout_sec) * 2), connect=30.0)
            try:
                async with httpx.AsyncClient(timeout=post_timeout) as client:
                    resp = await client.post(
                        url, json=body, headers=self._headers())
            except httpx.TimeoutException as exc:
                raise RuntimeError(
                    "火山生图超时：请勿立即重试同一请求以免重复扣费") from exc
            payload = _safe_json(resp)
            if resp.status_code >= 400 or (
                isinstance(payload.get("error"), dict)
                and payload["error"].get("code")
            ):
                raise RuntimeError(
                    "火山生图失败："
                    + (volcengine_error_text(
                        payload, f"HTTP {resp.status_code}") or "未知错误"))
            items = payload.get("data") if isinstance(payload.get("data"), list) else []
            first = items[0] if items and isinstance(items[0], dict) else {}
            img_url = str((first or {}).get("url") or payload.get("url") or "").strip()
            b64 = str((first or {}).get("b64_json") or "").strip()
            if img_url:
                if on_progress:
                    await on_progress("saving", "火山已完成，正在下载图片…")
                img_bytes = await fetch_url_bytes(
                    img_url, job_id=job_id, session_id=session_id,
                    on_progress=on_progress, stage="downloading",
                    label="图片", read_timeout=120.0)
            elif b64:
                if on_progress:
                    await on_progress("saving", "正在保存生成图…")
                img_bytes = base64.b64decode(b64)
            else:
                raise RuntimeError("火山生图未返回图片地址")
            out_dir = self.data_dir / "chat_images"
            out_dir.mkdir(parents=True, exist_ok=True)
            fname = f"gen_{uuid.uuid4().hex[:12]}.png"
            (out_dir / fname).write_bytes(img_bytes)
            settle_status = "succeeded"
            settle_ref = fname
            latency_ms = int((time.perf_counter() - t0) * 1000)
            return ImageGenResult(
                type="generated_image",
                filenames=[fname],
                public_urls=[f"/chat-images/{fname}"],
                n=1,
                revised_prompt=req.prompt,
                negative_prompt=(req.negative_prompt or "").strip() or None,
                size=size,
                provider_id=provider_id,
                model_id=model_name,
                latency_ms=latency_ms,
                backend="cloud",
                summary=f"已生成 1 张图（耗时 {max(1, latency_ms // 1000)}s）",
            )
        except JobCancelled:
            settle_status = "cancelled"
            settle_err = "已停止生成"
            raise
        except asyncio.CancelledError:
            settle_status = "cancelled"
            settle_err = "已停止生成"
            raise JobCancelled() from None
        except Exception as exc:
            settle_status = "failed"
            settle_err = (str(exc) or type(exc).__name__)[:500]
            raise
        finally:
            active_jobs.clear_job(session_id, job_id=job_id)
            if store is not None:
                try:
                    store.settle(
                        job_id, status=settle_status,
                        result_ref=settle_ref, error_message=settle_err)
                except Exception:  # noqa: BLE001
                    logger.debug("remote_jobs settle skip", exc_info=True)


def _safe_json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}
