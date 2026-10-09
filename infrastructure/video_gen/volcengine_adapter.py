"""火山方舟 Seedance 文生/图生视频。

官方协议：POST/GET /api/v3/contents/generations/tasks
必填 content[]，与 OpenAI /videos 的 prompt 形态不兼容。
"""
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
from .profiles import VideoProfile, aspect_ratio_of
from .types import VideoGenRequest, VideoGenResult
from .vendor import volcengine_api_root

logger = logging.getLogger("second_person.video_gen.volcengine")

ProgressCb = Callable[[str, str], Awaitable[None]] | None

_AUTH_CODES = {
    "authenticationerror", "invalidauthentication",
    "invalidapikey", "accessdenied", "forbidden",
}
_PROBE_OK_CODES = {
    "invalidparameter", "notfound", "resourcenotfound",
    "invalidrequest", "missingparameter",
}


def volcengine_headers(api_key: str) -> dict[str, str]:
    key = (api_key or "").strip()
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    }


def _error_obj(payload: dict | None) -> dict:
    if not isinstance(payload, dict):
        return {}
    err = payload.get("error")
    return err if isinstance(err, dict) else {}


def volcengine_error_text(payload: dict | None, fallback: str = "") -> str:
    if not isinstance(payload, dict):
        return (fallback or "")[:240]
    err = _error_obj(payload)
    code = str(
        err.get("code") or payload.get("code") or "").strip()
    message = str(
        err.get("message") or payload.get("message") or "").strip()
    text = " ".join(part for part in (code, message) if part)
    return (text or fallback or "")[:240]


def volcengine_task_id(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""
    return str(
        payload.get("id")
        or payload.get("task_id")
        or ""
    ).strip()


def volcengine_status(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("status") or "").strip().lower()


def volcengine_video_url(payload: dict) -> tuple[str, float | None]:
    """成功响应：content.video_url；duration 可能在顶层。"""
    if not isinstance(payload, dict):
        return "", None
    content = payload.get("content")
    url = ""
    if isinstance(content, dict):
        url = str(content.get("video_url") or content.get("file_url") or "").strip()
    if not url:
        url = str(payload.get("video_url") or "").strip()
    duration = payload.get("duration")
    try:
        dur = float(duration) if duration not in (None, "") else None
    except (TypeError, ValueError):
        dur = None
    return url, dur


def _resolution(req: VideoGenRequest) -> str:
    raw = (req.resolution or "720p").strip().lower()
    if raw.endswith("p") and raw[:-1].isdigit():
        return raw
    if raw in ("480", "720", "1080"):
        return f"{raw}p"
    return "720p"


def _build_content(
    prompt: str, *, image_uri: str = "",
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = [
        {"type": "text", "text": prompt},
    ]
    if image_uri:
        items.append({
            "type": "image_url",
            "image_url": {"url": image_uri},
            "role": "first_frame",
        })
    return items


class VolcengineVideoAdapter:
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
        self.api_root = volcengine_api_root(self.base_url)
        self.api_key = api_key or ""
        self.data_dir = Path(data_dir)
        self.profile = profile
        self.model_id = model_id or ""

    def _headers(self) -> dict[str, str]:
        return volcengine_headers(self.api_key)

    def _tasks_url(self, task_id: str = "") -> str:
        root = f"{self.api_root}/contents/generations/tasks"
        if task_id:
            return f"{root}/{task_id}"
        return root

    async def probe(self, timeout: float = 12.0) -> dict:
        # 用不存在的任务号探测：密钥有效时常回 404/400，而不是去真创建任务。
        url = self._tasks_url("0")
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url, headers=self._headers())
        except httpx.ConnectError as exc:
            return {"ok": False, "error": connect_error_message(self.base_url, exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:200]}
        payload: dict = {}
        try:
            body = resp.json()
            if isinstance(body, dict):
                payload = body
        except Exception:  # noqa: BLE001
            payload = {}
        err = _error_obj(payload)
        code = str(
            err.get("code") or payload.get("code") or "").strip().lower()
        message = volcengine_error_text(payload, (resp.text or "")[:160])
        if resp.status_code in (401, 403) or code in _AUTH_CODES:
            return {"ok": False, "error": message or "鉴权失败，请检查火山 API Key"}
        if resp.status_code < 400:
            return {"ok": True, "protocol": "volcengine"}
        if resp.status_code in (400, 404) and (
            code in _PROBE_OK_CODES
            or "not found" in message.lower()
            or "task" in message.lower()
            or "parameter" in message.lower()
        ):
            return {"ok": True, "protocol": "volcengine"}
        return {
            "ok": False,
            "error": message or f"火山视频接口返回 HTTP {resp.status_code}",
        }

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
        if not model_name:
            raise RuntimeError(
                "请填写火山模型 ID，例如 doubao-seedance-1-0-pro-250528")
        image_name = (req.image_filename or "").strip() or None
        image_uri = ""
        if image_name:
            from .images import image_file_to_data_uri, resolve_chat_image
            img_path = resolve_chat_image(self.data_dir, image_name)
            if img_path is None:
                raise RuntimeError(f"找不到参考图：{image_name}")
            image_uri = image_file_to_data_uri(img_path)
        prompt = (req.prompt or "")[:4000]
        if (req.negative_prompt or "").strip():
            prompt = f"{prompt}\n不要出现：{req.negative_prompt.strip()}"[:4000]
        aspect = aspect_ratio_of(self.profile, req.size)
        body: dict[str, Any] = {
            "model": model_name,
            "content": _build_content(prompt, image_uri=image_uri),
            "duration": int(req.duration_sec),
            "ratio": "adaptive" if image_uri else aspect,
            "resolution": _resolution(req),
            "watermark": False,
        }
        headers = self._headers()
        create_url = self._tasks_url()
        http_timeout = httpx.Timeout(30.0, connect=15.0)
        job_id = active_jobs.register_job(
            session_id, base_url=self.base_url, prompt_id="",
            backend="volcengine", kind="volcengine_video")
        runtime.set_cancel_headers(job_id, headers)
        store = get_store()
        if store is not None:
            try:
                store.create(
                    kind="volcengine_video", backend="volcengine",
                    session_id=session_id, base_url=self.base_url,
                    job_id=job_id,
                    meta={"mode": "i2v" if image_uri else "t2v",
                          "image_filename": image_name or "",
                          "api_root": self.api_root})
            except Exception:  # noqa: BLE001
                logger.debug("remote_jobs create skip", exc_info=True)
        if on_progress:
            await on_progress(
                "submit",
                "正在提交火山图生视频…" if image_uri else "正在提交火山生视频…",
            )
        video_bytes = b""
        duration = float(req.duration_sec)
        settle_status = "failed"
        settle_err: str | None = None
        settle_ref: str | None = None
        try:
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
                if created.status_code >= 400 or _error_obj(payload).get("code"):
                    raise RuntimeError(
                        "火山生视频提交失败："
                        + (volcengine_error_text(
                            payload, f"HTTP {created.status_code}") or "未知错误"))
                task_id = volcengine_task_id(payload)
                if not task_id:
                    raise RuntimeError("火山生视频未返回任务 ID")
                active_jobs.bind_remote(session_id, task_id, job_id=job_id)
                if store is not None:
                    try:
                        store.set_remote_id(job_id, task_id)
                    except Exception:  # noqa: BLE001
                        logger.debug("remote_jobs bind skip", exc_info=True)
                if on_progress:
                    await on_progress("waiting", f"火山渲染中…任务 {task_id[:12]}")
                video_url = ""
                wait_t0 = time.perf_counter()
                last_wait_ping = 0.0
                while True:
                    runtime.raise_if_cancelled(job_id=job_id, session_id=session_id)
                    info = await http_get_json_resilient(
                        client,
                        self._tasks_url(task_id),
                        headers=headers,
                        job_id=job_id, session_id=session_id,
                        on_progress=on_progress, stage="waiting",
                        label="火山视频任务",
                    )
                    if not isinstance(info, dict):
                        info = {}
                    status = volcengine_status(info)
                    if status == "succeeded":
                        video_url, dur = volcengine_video_url(info)
                        if dur:
                            duration = dur
                        break
                    if status in ("failed", "cancelled", "canceled", "expired"):
                        raise RuntimeError(
                            volcengine_error_text(info, "火山生视频失败"))
                    now = time.perf_counter()
                    if on_progress and now - last_wait_ping >= 8.0:
                        elapsed = int(now - wait_t0)
                        await on_progress(
                            "waiting", f"火山渲染中…已等待 {elapsed}s")
                        last_wait_ping = now
                    if store is not None:
                        try:
                            store.touch(job_id)
                        except Exception:  # noqa: BLE001
                            pass
                    await sleep_or_cancel(2.0, job_id=job_id, session_id=session_id)
                if not video_url:
                    raise RuntimeError("火山生视频完成但未返回视频地址")
                if store is not None:
                    try:
                        store.merge_meta(job_id, {
                            "phase": "download",
                            "result_url": video_url,
                            "duration_sec": duration,
                        })
                    except Exception:  # noqa: BLE001
                        logger.debug("remote_jobs meta skip", exc_info=True)
                if on_progress:
                    await on_progress("saving", "火山已完成，正在下载成片…")
                video_bytes = await fetch_url_bytes(
                    video_url,
                    job_id=job_id, session_id=session_id,
                    on_progress=on_progress, stage="downloading",
                    label="成片",
                )
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
                negative_prompt=(req.negative_prompt or "").strip() or None,
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
