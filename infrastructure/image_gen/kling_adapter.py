"""可灵官方文生图：POST /v1/images/generations + 轮询任务。"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from infrastructure.image_gen import active_jobs
from infrastructure.remote_jobs import JobCancelled, get_store, runtime
from infrastructure.remote_jobs.fetch import (
    fetch_url_bytes, http_get_json_resilient, sleep_or_cancel,
)
from infrastructure.video_gen.cloud_adapter import connect_error_message
from infrastructure.video_gen.kling_auth import (
    is_legacy_kling_model, kling_auth_header, looks_like_ak_sk,
)

from .types import ImageGenRequest, ImageGenResult

logger = logging.getLogger("second_person.image_gen.kling")

ProgressCb = Callable[[str, str], Awaitable[None]] | None

_NEW_KEY_HINT = (
    "可灵新版图片接口请使用控制台 API Key（Bearer），"
    "不要填 AccessKey:SecretKey"
)


def _aspect_of(size: str) -> str:
    raw = (size or "").strip().lower().replace("*", "x")
    if raw in ("1024x1792", "768x1344", "9:16"):
        return "9:16"
    if raw in ("1792x1024", "1344x768", "16:9"):
        return "16:9"
    return "1:1"


def _task_item(payload: dict) -> dict:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        return data
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return payload if isinstance(payload, dict) else {}


def _task_id(payload: dict) -> str:
    item = _task_item(payload)
    return str(item.get("task_id") or item.get("id") or "").strip()


def _task_status(payload: dict) -> str:
    item = _task_item(payload)
    return str(item.get("task_status") or item.get("status") or "").strip().lower()


def _image_url(payload: dict) -> str:
    item = _task_item(payload)
    result = item.get("task_result") if isinstance(item.get("task_result"), dict) else {}
    images = result.get("images") if isinstance(result, dict) else None
    if isinstance(images, list) and images and isinstance(images[0], dict):
        return str(images[0].get("url") or "").strip()
    return str(item.get("url") or "").strip()


def _api_error(payload: dict) -> str | None:
    if not isinstance(payload, dict):
        return None
    code = payload.get("code")
    if code in (0, "0", None):
        return None
    msg = str(payload.get("message") or "").strip()
    return (msg or f"可灵生图失败 code={code}")[:240]


class KlingImageAdapter:
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
        self.api_key = api_key or ""
        self.data_dir = Path(data_dir)
        self.timeout_sec = timeout_sec
        self.model_id = model_id or "kling-v2"

    def _headers(self, model_id: str | None = None) -> dict[str, str]:
        mid = model_id if model_id is not None else self.model_id
        headers = {"Content-Type": "application/json"}
        headers.update(kling_auth_header(self.api_key, model_id=mid))
        return headers

    async def probe(self, timeout: float = 12.0) -> dict:
        if not is_legacy_kling_model(self.model_id) and looks_like_ak_sk(self.api_key):
            # 图片入口普遍仍吃 JWT；新版模型名也可能用 Bearer，不硬拦 AK:SK
            pass
        url = f"{self.base_url}/v1/images/generations"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(
                    url, headers=self._headers(),
                    params={"pageNum": 1, "pageSize": 1})
        except httpx.ConnectError as exc:
            return {"ok": False, "error": connect_error_message(self.base_url, exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:200]}
        if resp.status_code < 400 or resp.status_code in (400, 405):
            return {"ok": True, "protocol": "kling"}
        if resp.status_code in (401, 403):
            return {"ok": False, "error": "鉴权失败，请检查可灵 API Key"}
        return {"ok": False, "error": f"可灵图片接口返回 HTTP {resp.status_code}"}

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
            raise RuntimeError("请填写可灵图片模型 ID，例如 kling-v2")
        if "turbo" in model_name.lower() and "image" not in model_name.lower():
            # kling-3.0-turbo 是视频模型
            if "v3" in model_name.lower() or "3.0" in model_name:
                raise RuntimeError(
                    f"模型 {model_name} 更像视频模型，文生图请用 kling-v1 / kling-v2 / kling-v3")
        prompt = (req.prompt or "")[:2500]
        body: dict[str, Any] = {
            "model_name": model_name,
            "prompt": prompt,
            "n": 1,
            "aspect_ratio": _aspect_of(req.size),
            "resolution": "1k",
        }
        if (req.negative_prompt or "").strip():
            body["negative_prompt"] = req.negative_prompt.strip()[:2500]
        headers = self._headers(model_name)
        create_url = f"{self.base_url}/v1/images/generations"
        job_id = active_jobs.register_job(
            session_id, base_url=self.base_url, prompt_id="",
            backend="kling", kind="kling_image")
        runtime.set_cancel_headers(job_id, headers)
        store = get_store()
        if store is not None:
            try:
                store.create(
                    kind="kling_image", backend="kling",
                    session_id=session_id, base_url=self.base_url,
                    job_id=job_id)
            except Exception:  # noqa: BLE001
                logger.debug("remote_jobs create skip", exc_info=True)
        settle_status = "failed"
        settle_err: str | None = None
        settle_ref: str | None = None
        try:
            if on_progress:
                await on_progress("submit", "正在提交可灵生图…")
            http_timeout = httpx.Timeout(30.0, connect=15.0)
            async with httpx.AsyncClient(timeout=http_timeout) as client:
                runtime.raise_if_cancelled(job_id=job_id, session_id=session_id)
                try:
                    created = await client.post(
                        create_url, json=body, headers=headers)
                except httpx.ConnectError as exc:
                    raise RuntimeError(
                        connect_error_message(self.base_url, exc)) from exc
                payload: dict = {}
                try:
                    parsed = created.json()
                    if isinstance(parsed, dict):
                        payload = parsed
                except Exception:  # noqa: BLE001
                    payload = {}
                if created.status_code >= 400:
                    raise RuntimeError(
                        f"可灵生图提交失败 HTTP {created.status_code}："
                        f"{(created.text or '')[:240]}")
                api_err = _api_error(payload)
                if api_err:
                    raise RuntimeError(api_err)
                task_id = _task_id(payload)
                if not task_id:
                    raise RuntimeError("可灵生图未返回任务 ID")
                active_jobs.bind_remote(session_id, task_id, job_id=job_id)
                if store is not None:
                    try:
                        store.set_remote_id(job_id, task_id)
                    except Exception:  # noqa: BLE001
                        logger.debug("remote_jobs bind skip", exc_info=True)
                if on_progress:
                    await on_progress("waiting", f"可灵生图中…任务 {task_id[:12]}")
                img_url = ""
                wait_t0 = time.perf_counter()
                last_wait_ping = 0.0
                while True:
                    runtime.raise_if_cancelled(
                        job_id=job_id, session_id=session_id)
                    info = await http_get_json_resilient(
                        client, f"{create_url}/{task_id}",
                        headers=headers,
                        job_id=job_id, session_id=session_id,
                        on_progress=on_progress, stage="waiting",
                        label="可灵图片任务")
                    if not isinstance(info, dict):
                        info = {}
                    api_err = _api_error(info)
                    if api_err and _task_status(info) in ("", "failed", "fail"):
                        raise RuntimeError(api_err)
                    status = _task_status(info)
                    if status in ("succeed", "succeeded", "success", "completed"):
                        img_url = _image_url(info)
                        break
                    if status in ("failed", "fail", "cancelled", "canceled"):
                        raise RuntimeError(api_err or "可灵生图失败")
                    now = time.perf_counter()
                    if on_progress and now - last_wait_ping >= 8.0:
                        await on_progress(
                            "waiting",
                            f"可灵生图中…已等待 {int(now - wait_t0)}s")
                        last_wait_ping = now
                    if store is not None:
                        try:
                            store.touch(job_id)
                        except Exception:  # noqa: BLE001
                            pass
                    await sleep_or_cancel(
                        2.0, job_id=job_id, session_id=session_id)
                if not img_url:
                    raise RuntimeError("可灵生图完成但未返回图片地址")
                if on_progress:
                    await on_progress("saving", "可灵已完成，正在下载图片…")
                img_bytes = await fetch_url_bytes(
                    img_url, job_id=job_id, session_id=session_id,
                    on_progress=on_progress, stage="downloading",
                    label="图片", read_timeout=120.0)
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
                size=req.size or "1024x1024",
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
