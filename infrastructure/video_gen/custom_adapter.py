"""自定义文生视频：请求地址原样使用，不补可灵、百炼或 OpenAI 路径。"""
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

from .cloud_adapter import connect_error_message
from .dashscope_adapter import (
    _parameters,
    dashscope_error_text,
    dashscope_headers,
    dashscope_query_headers,
    dashscope_status,
    dashscope_task_id,
    dashscope_video_url,
)
from .profiles import VideoProfile, aspect_ratio_of
from .types import VideoGenRequest, VideoGenResult
from .vendor import dashscope_api_root

logger = logging.getLogger("second_person.video_gen.custom")

ProgressCb = Callable[[str, str], Awaitable[None]] | None


def _video_url(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("video_url", "url"):
        if payload.get(key):
            return str(payload[key]).strip()
    output = payload.get("output")
    if isinstance(output, dict):
        found = _video_url(output)
        if found:
            return found
    data = payload.get("data")
    if isinstance(data, dict):
        return _video_url(data)
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return _video_url(data[0])
    return ""


def _rejects_synchronous(status: int, payload: dict, text: str) -> bool:
    """百炼视频 HTTP 接口不接受同步调用，正文里会写 synchronous。"""
    parts = [str(payload.get("message") or ""), text or ""]
    output = payload.get("output")
    if isinstance(output, dict):
        parts.append(str(output.get("message") or ""))
    blob = " ".join(parts).lower()
    return status in (400, 403) and "synchronous" in blob


class CustomVideoAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        data_dir: Path,
        profile: VideoProfile,
        model_id: str = "",
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.data_dir = Path(data_dir)
        self.profile = profile
        self.model_id = model_id or ""

    async def _finish_async_task(
        self,
        req: VideoGenRequest,
        *,
        model_name: str,
        prompt: str,
        job_id: str,
        session_id: str,
        store,
        on_progress: ProgressCb,
    ) -> tuple[bytes, float]:
        """同一地址补上异步头再提交，再按百炼文档查 /tasks/{task_id}。"""
        headers = dashscope_headers(self.api_key)
        aspect = aspect_ratio_of(self.profile, req.size)
        body = {
            "model": model_name,
            "input": {"prompt": prompt},
            "parameters": _parameters(model_name, req, aspect),
        }
        api_root = dashscope_api_root(self.base_url)
        timeout = httpx.Timeout(30.0, connect=15.0)
        duration = float(req.duration_sec)
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                created = await client.post(self.base_url, json=body, headers=headers)
            except httpx.ConnectError as exc:
                raise RuntimeError(connect_error_message(self.base_url, exc)) from exc
            payload: dict = {}
            try:
                parsed = created.json()
                if isinstance(parsed, dict):
                    payload = parsed
            except Exception:  # noqa: BLE001
                payload = {}
            if created.status_code >= 400 or payload.get("code"):
                raise RuntimeError(
                    "自定义视频接口失败："
                    + (dashscope_error_text(payload, f"HTTP {created.status_code}")
                       or "未知错误"))
            task_id = dashscope_task_id(payload)
            if not task_id:
                raise RuntimeError("异步视频接口没有返回任务 ID")
            active_jobs.bind_remote(session_id, task_id, job_id=job_id)
            if store is not None:
                try:
                    store.set_remote_id(job_id, task_id)
                except Exception:  # noqa: BLE001
                    logger.debug("remote_jobs bind skip", exc_info=True)
            if on_progress:
                await on_progress("waiting", "视频渲染中…")
            video_url = ""
            wait_t0 = time.perf_counter()
            last_wait_ping = 0.0
            while True:
                runtime.raise_if_cancelled(job_id=job_id, session_id=session_id)
                info = await http_get_json_resilient(
                    client, f"{api_root}/tasks/{task_id}",
                    headers=dashscope_query_headers(self.api_key),
                    job_id=job_id, session_id=session_id,
                    on_progress=on_progress, stage="waiting", label="视频任务")
                if not isinstance(info, dict):
                    info = {}
                status = dashscope_status(info)
                if status == "SUCCEEDED":
                    video_url, dur = dashscope_video_url(info)
                    if dur:
                        duration = dur
                    break
                if status in ("FAILED", "CANCELED", "UNKNOWN"):
                    raise RuntimeError(dashscope_error_text(info, "视频生成失败"))
                now = time.perf_counter()
                if on_progress and now - last_wait_ping >= 8.0:
                    await on_progress(
                        "waiting", f"视频渲染中…已等待 {int(now - wait_t0)}s")
                    last_wait_ping = now
                if store is not None:
                    try:
                        store.touch(job_id)
                    except Exception:  # noqa: BLE001
                        pass
                await sleep_or_cancel(2.0, job_id=job_id, session_id=session_id)
        if not video_url:
            raise RuntimeError("异步视频完成但没有返回地址")
        if on_progress:
            await on_progress("saving", "正在下载成片…")
        video_bytes = await fetch_url_bytes(
            video_url, job_id=job_id, session_id=session_id,
            on_progress=on_progress, stage="downloading", label="成片")
        return video_bytes, duration

    def _headers(self) -> dict[str, str]:
        key = self.api_key.strip()
        if key.lower().startswith("bearer "):
            key = key[7:].strip()
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        }

    async def probe(self, timeout: float = 12.0) -> dict:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(self.base_url, headers=self._headers())
        except httpx.ConnectError as exc:
            return {"ok": False, "error": connect_error_message(self.base_url, exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:200]}
        # 自定义地址只接受生成用的 POST。空 GET 回 400/405 说明服务在，只是不接受探测。
        if resp.status_code < 400 or resp.status_code in (400, 405):
            return {"ok": True, "protocol": "custom"}
        if resp.status_code in (401, 403):
            return {"ok": False, "error": "鉴权失败，请检查 API Key"}
        detail = (resp.text or "").strip().replace("\n", " ")[:180]
        msg = f"自定义视频接口返回 HTTP {resp.status_code}"
        if detail:
            msg = f"{msg}：{detail}"
        return {"ok": False, "error": msg}

    async def generate(
        self,
        req: VideoGenRequest,
        *,
        provider_id: str = "",
        model_id: str = "",
        session_id: str = "",
        on_progress: ProgressCb = None,
    ) -> VideoGenResult:
        t0 = time.perf_counter()
        model_name = (model_id or req.model_id or self.model_id).strip()
        prompt = (req.prompt or "")[:4000]
        if (req.negative_prompt or "").strip():
            prompt = f"{prompt}\n不要出现：{req.negative_prompt.strip()}"[:4000]
        body: dict[str, Any] = {
            "model": model_name,
            "prompt": prompt,
            "duration": int(req.duration_sec),
            "size": req.size,
            "resolution": req.resolution or "720p",
        }
        headers = self._headers()
        job_id = active_jobs.register_job(
            session_id, base_url=self.base_url, prompt_id="",
            backend="custom", kind="custom_video")
        store = get_store()
        if store is not None:
            try:
                store.create(
                    kind="custom_video", backend="custom",
                    session_id=session_id, base_url=self.base_url,
                    job_id=job_id, meta={"mode": "t2v"})
            except Exception:  # noqa: BLE001
                logger.debug("remote_jobs create skip", exc_info=True)
        settle_status = "failed"
        settle_err: str | None = None
        settle_ref: str | None = None
        try:
            runtime.raise_if_cancelled(job_id=job_id, session_id=session_id)
            if on_progress:
                await on_progress("submit", "正在提交自定义视频接口…")
            timeout = httpx.Timeout(30.0, connect=15.0)
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    created = await client.post(
                        self.base_url, json=body, headers=headers)
            except httpx.ConnectError as exc:
                raise RuntimeError(connect_error_message(self.base_url, exc)) from exc
            payload: dict = {}
            try:
                parsed = created.json()
                if isinstance(parsed, dict):
                    payload = parsed
            except Exception:  # noqa: BLE001
                payload = {}
            if _rejects_synchronous(created.status_code, payload, created.text or ""):
                if on_progress:
                    await on_progress("submit", "接口要求异步提交，正在重新提交…")
                video_bytes, duration = await self._finish_async_task(
                    req, model_name=model_name, prompt=prompt,
                    job_id=job_id, session_id=session_id, store=store,
                    on_progress=on_progress)
            else:
                if created.status_code >= 400:
                    detail = str(payload.get("message") or created.text or "")[:240]
                    raise RuntimeError(
                        f"自定义视频接口失败 HTTP {created.status_code}：{detail}")
                video_url = _video_url(payload)
                if not video_url:
                    raise RuntimeError("自定义视频接口没有直接返回视频地址")
                if on_progress:
                    await on_progress("saving", "正在下载成片…")
                video_bytes = await fetch_url_bytes(
                    video_url, job_id=job_id, session_id=session_id,
                    on_progress=on_progress, stage="downloading", label="成片")
                duration = float(req.duration_sec)
            out_dir = self.data_dir / "chat_videos"
            out_dir.mkdir(parents=True, exist_ok=True)
            fname = f"genv_{uuid.uuid4().hex[:12]}.mp4"
            (out_dir / fname).write_bytes(video_bytes)
            settle_status = "succeeded"
            settle_ref = fname
            latency_ms = int((time.perf_counter() - t0) * 1000)
            return VideoGenResult(
                type="generated_video",
                filenames=[fname],
                public_urls=[f"/chat-videos/{fname}"],
                n=1,
                revised_prompt=req.prompt,
                size=req.size,
                duration_sec=float(duration),
                fps=req.fps,
                provider_id=provider_id,
                model_id=model_name,
                latency_ms=latency_ms,
                backend="cloud",
                summary=(
                    f"已生成 1 条视频（约 {int(duration)}s，"
                    f"耗时 {max(1, latency_ms // 1000)}s）"
                ),
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
            if store is not None and settle_status != "running":
                try:
                    store.settle(
                        job_id, status=settle_status,
                        result_ref=settle_ref, error_message=settle_err)
                except Exception:  # noqa: BLE001
                    logger.debug("remote_jobs settle skip", exc_info=True)
