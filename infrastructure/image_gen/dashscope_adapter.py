"""阿里云百炼万相文生图：异步 text2image + 查 /tasks。"""
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
from infrastructure.video_gen.dashscope_adapter import (
    dashscope_error_text,
    dashscope_headers,
    dashscope_query_headers,
    dashscope_status,
    dashscope_task_id,
)
from infrastructure.video_gen.vendor import dashscope_api_root

from .types import ImageGenRequest, ImageGenResult

logger = logging.getLogger("second_person.image_gen.dashscope")

ProgressCb = Callable[[str, str], Awaitable[None]] | None

_AUTH_CODES = {"invalidapikey", "accessdenied", "forbidden"}
_PROBE_OK_CODES = {"invalidparameter", "invalidtask", "tasknotexist"}


def _size_star(size: str) -> str:
    raw = (size or "1024x1024").strip().lower().replace("x", "*")
    if raw in ("1024*1024", "1024*1792", "1792*1024", "1280*1280"):
        return raw
    return "1024*1024"


def _image_url(payload: dict) -> str:
    out = payload.get("output") if isinstance(payload.get("output"), dict) else {}
    results = out.get("results") if isinstance(out, dict) else None
    if isinstance(results, list) and results:
        first = results[0] if isinstance(results[0], dict) else {}
        url = str(first.get("url") or "").strip()
        if url:
            return url
    return str((out or {}).get("url") or payload.get("url") or "").strip()


class DashScopeImageAdapter:
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
        self.api_root = dashscope_api_root(self.base_url)
        self.api_key = api_key or ""
        self.data_dir = Path(data_dir)
        self.timeout_sec = timeout_sec
        self.model_id = model_id or ""

    def _headers(self) -> dict[str, str]:
        return dashscope_headers(self.api_key)

    async def probe(self, timeout: float = 12.0) -> dict:
        url = f"{self.api_root}/tasks/0"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url, headers=self._headers())
        except httpx.ConnectError:
            return {"ok": False, "error": "无法连接百炼图片服务"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:200]}
        payload: dict = {}
        try:
            body = resp.json()
            if isinstance(body, dict):
                payload = body
        except Exception:  # noqa: BLE001
            payload = {}
        code = str(payload.get("code") or "").lower()
        message = dashscope_error_text(payload, (resp.text or "")[:160])
        if resp.status_code in (401, 403) or code in _AUTH_CODES:
            return {"ok": False, "error": message or "鉴权失败，请检查百炼 API Key"}
        if resp.status_code < 400:
            return {"ok": True, "protocol": "dashscope"}
        if resp.status_code == 400 and (
            code in _PROBE_OK_CODES or "task" in message.lower()
        ):
            return {"ok": True, "protocol": "dashscope"}
        return {
            "ok": False,
            "error": message or f"百炼图片接口返回 HTTP {resp.status_code}",
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
            raise RuntimeError("请填写百炼图片模型 ID，例如 wan2.2-t2i-flash")
        if "t2v" in model_name.lower() or "i2v" in model_name.lower():
            raise RuntimeError(
                f"模型 {model_name} 是视频模型，文生图请改用 wan*-t2i / wanx")
        prompt = (req.prompt or "")[:2000]
        neg = (req.negative_prompt or "").strip()
        body_input: dict[str, Any] = {"prompt": prompt}
        if neg:
            body_input["negative_prompt"] = neg[:500]
        body = {
            "model": model_name,
            "input": body_input,
            "parameters": {
                "size": _size_star(req.size),
                "n": 1,
            },
        }
        headers = self._headers()
        create_url = (
            f"{self.api_root}/services/aigc/text2image/image-synthesis"
        )
        job_id = active_jobs.register_job(
            session_id, base_url=self.base_url, prompt_id="",
            backend="dashscope", kind="dashscope_image")
        runtime.set_cancel_headers(job_id, headers)
        store = get_store()
        if store is not None:
            try:
                store.create(
                    kind="dashscope_image", backend="dashscope",
                    session_id=session_id, base_url=self.base_url,
                    job_id=job_id, meta={"api_root": self.api_root})
            except Exception:  # noqa: BLE001
                logger.debug("remote_jobs create skip", exc_info=True)
        settle_status = "failed"
        settle_err: str | None = None
        settle_ref: str | None = None
        try:
            if on_progress:
                await on_progress("submit", "正在提交百炼生图…")
            http_timeout = httpx.Timeout(30.0, connect=15.0)
            async with httpx.AsyncClient(timeout=http_timeout) as client:
                runtime.raise_if_cancelled(job_id=job_id, session_id=session_id)
                created = await client.post(
                    create_url, json=body, headers=headers)
                payload: dict = {}
                try:
                    parsed = created.json()
                    if isinstance(parsed, dict):
                        payload = parsed
                except Exception:  # noqa: BLE001
                    payload = {}
                if created.status_code >= 400 or payload.get("code"):
                    raise RuntimeError(
                        "百炼生图提交失败："
                        + (dashscope_error_text(
                            payload, f"HTTP {created.status_code}") or "未知错误"))
                task_id = dashscope_task_id(payload)
                if not task_id:
                    raise RuntimeError("百炼生图未返回任务 ID")
                active_jobs.bind_remote(session_id, task_id, job_id=job_id)
                if store is not None:
                    try:
                        store.set_remote_id(job_id, task_id)
                    except Exception:  # noqa: BLE001
                        logger.debug("remote_jobs bind skip", exc_info=True)
                if on_progress:
                    await on_progress("waiting", f"百炼生图中…任务 {task_id[:12]}")
                img_url = ""
                wait_t0 = time.perf_counter()
                last_wait_ping = 0.0
                while True:
                    runtime.raise_if_cancelled(
                        job_id=job_id, session_id=session_id)
                    info = await http_get_json_resilient(
                        client, f"{self.api_root}/tasks/{task_id}",
                        headers=dashscope_query_headers(self.api_key),
                        job_id=job_id, session_id=session_id,
                        on_progress=on_progress, stage="waiting",
                        label="百炼图片任务")
                    if not isinstance(info, dict):
                        info = {}
                    status = dashscope_status(info)
                    if status == "SUCCEEDED":
                        img_url = _image_url(info)
                        break
                    if status in ("FAILED", "CANCELED", "UNKNOWN"):
                        raise RuntimeError(
                            dashscope_error_text(info, "百炼生图失败"))
                    now = time.perf_counter()
                    if on_progress and now - last_wait_ping >= 8.0:
                        await on_progress(
                            "waiting",
                            f"百炼生图中…已等待 {int(now - wait_t0)}s")
                        last_wait_ping = now
                    if store is not None:
                        try:
                            store.touch(job_id)
                        except Exception:  # noqa: BLE001
                            pass
                    await sleep_or_cancel(
                        2.0, job_id=job_id, session_id=session_id)
                if not img_url:
                    raise RuntimeError("百炼生图完成但未返回图片地址")
                if on_progress:
                    await on_progress("saving", "百炼已完成，正在下载图片…")
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
                negative_prompt=neg or None,
                size=_size_star(req.size).replace("*", "x"),
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
