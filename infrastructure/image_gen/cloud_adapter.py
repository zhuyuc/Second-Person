"""OpenAI 兼容云端文生图：提交后下载跟到成功或取消。"""
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

from .types import ImageGenRequest, ImageGenResult

logger = logging.getLogger("second_person.image_gen.cloud")

ProgressCb = Callable[[str, str], Awaitable[None]] | None


class OpenAIImageAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        data_dir: Path,
        timeout_sec: float = 120.0,
        model_id: str = "",
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.data_dir = Path(data_dir)
        # 仅作「单次生成 POST」读超时；下载走可重试获取，不受此限制结案
        self.timeout_sec = timeout_sec
        self.model_id = model_id

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def probe(self, timeout: float = 12.0) -> dict:
        url = f"{self.base_url}/models"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url, headers=self._headers())
            if resp.status_code < 400:
                return {"ok": True, "protocol": "image"}
            return {"ok": False, "error": f"图片接口返回 HTTP {resp.status_code}"}
        except httpx.ConnectError:
            return {"ok": False, "error": "无法连接云端图片服务"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:200]}

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
        model_name = model_id or req.model_id or self.model_id
        size = req.size if req.size in ("1024x1024", "1024x1792", "1792x1024") else "1024x1024"
        job_id = active_jobs.register_job(
            session_id, base_url=self.base_url, prompt_id="",
            backend="openai_image", kind="cloud_image")
        store = get_store()
        if store is not None:
            try:
                store.create(
                    kind="cloud_image", backend="openai_image",
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
                await on_progress("submit", "正在提交云端生图…")
            body = {
                "model": model_name,
                "prompt": (req.prompt or "")[:4000],
                "n": 1,
                "size": size,
            }
            url = f"{self.base_url}/images/generations"
            # 生成 POST：跟到响应或用户取消；ReadTimeout 视为「可能已扣费无句柄」——禁盲重试
            post_timeout = httpx.Timeout(
                max(300.0, float(self.timeout_sec) * 2), connect=30.0)
            try:
                async with httpx.AsyncClient(timeout=post_timeout) as client:
                    resp = await client.post(
                        url, json=body, headers=self._headers())
            except httpx.ReadTimeout as exc:
                raise RuntimeError(
                    "云端生图请求超时：远端可能已出图但未返回句柄。"
                    "请勿立即用同一提示词重试以免重复扣费；可稍后再试或更换描述"
                ) from exc
            except httpx.TimeoutException as exc:
                raise RuntimeError(
                    "云端生图连接/请求超时：请勿立即重试同一请求以免重复扣费"
                ) from exc
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"云端生图失败 HTTP {resp.status_code}：{(resp.text or '')[:240]}")
            payload = resp.json()
            items = payload.get("data") if isinstance(payload.get("data"), list) else []
            first = items[0] if items and isinstance(items[0], dict) else payload
            img_url = (first or {}).get("url") or payload.get("url") or ""
            b64 = (first or {}).get("b64_json") or ""
            if img_url:
                active_jobs.bind_remote(session_id, str(img_url)[:200], job_id=job_id)
                if store is not None:
                    try:
                        store.merge_meta(job_id, {
                            "phase": "download", "result_url": str(img_url)})
                        store.set_remote_id(job_id, "url")
                    except Exception:  # noqa: BLE001
                        pass
                if on_progress:
                    await on_progress("saving", "云端已完成，正在下载图片…")
                img_bytes = await fetch_url_bytes(
                    str(img_url),
                    job_id=job_id, session_id=session_id,
                    on_progress=on_progress, stage="downloading",
                    label="图片", read_timeout=120.0,
                )
            elif b64:
                if on_progress:
                    await on_progress("saving", "正在保存生成图…")
                img_bytes = base64.b64decode(b64)
            else:
                raise RuntimeError("云端生图未返回图片地址")

            out_dir = self.data_dir / "chat_images"
            out_dir.mkdir(parents=True, exist_ok=True)
            fname = f"gen_{uuid.uuid4().hex[:12]}.png"
            (out_dir / fname).write_bytes(img_bytes)
            settle_status = "succeeded"
            settle_ref = fname
            latency_ms = int((time.perf_counter() - t0) * 1000)
            result = ImageGenResult(
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
            logger.info("cloud image_gen ok provider=%s file=%s ms=%s",
                        provider_id, fname, latency_ms)
            return result
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
