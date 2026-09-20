"""阿里云百炼万相文生视频。与可灵协议分开，不共用 /tasks?task_ids=。"""
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
from .vendor import dashscope_api_root

logger = logging.getLogger("second_person.video_gen.dashscope")

ProgressCb = Callable[[str, str], Awaitable[None]] | None

_AUTH_CODES = {"invalidapikey", "accessdenied", "forbidden"}
# 用一个不存在的任务号探测：密钥有效时百炼回 400，而不是 401。
_PROBE_OK_CODES = {"invalidparameter", "invalidtask", "tasknotexist"}


def dashscope_headers(api_key: str) -> dict[str, str]:
    key = (api_key or "").strip()
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
        "X-DashScope-Async": "enable",
    }


def dashscope_query_headers(api_key: str) -> dict[str, str]:
    """查任务只带 Authorization。异步头只用于创建，带上会被工作空间域名回 403。"""
    key = (api_key or "").strip()
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    return {"Authorization": f"Bearer {key}"}


def _output(payload: dict) -> dict:
    out = payload.get("output") if isinstance(payload, dict) else None
    return out if isinstance(out, dict) else {}


def dashscope_task_id(payload: dict) -> str:
    out = _output(payload)
    return str(out.get("task_id") or payload.get("task_id") or "").strip()


def dashscope_status(payload: dict) -> str:
    out = _output(payload)
    return str(out.get("task_status") or payload.get("task_status") or "").upper()


def dashscope_video_url(payload: dict) -> tuple[str, float | None]:
    out = _output(payload)
    url = str(out.get("video_url") or "").strip()
    if not url:
        results = out.get("results")
        first = results[0] if isinstance(results, list) and results else {}
        if isinstance(first, dict):
            url = str(first.get("url") or "").strip()
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    duration = usage.get("duration") or usage.get("output_video_duration")
    try:
        dur = float(duration) if duration not in (None, "") else None
    except (TypeError, ValueError):
        dur = None
    return url, dur


def dashscope_error_text(payload: dict | None, fallback: str = "") -> str:
    if not isinstance(payload, dict):
        return fallback[:240]
    out = _output(payload)
    code = str(payload.get("code") or out.get("code") or "").strip()
    message = str(payload.get("message") or out.get("message") or "").strip()
    text = " ".join(part for part in (code, message) if part)
    return (text or fallback)[:240]


def _resolution(req: VideoGenRequest) -> str:
    raw = (req.resolution or "720p").strip().upper().replace("P", "")
    if raw in ("480", "720", "1080"):
        return f"{raw}P"
    return "720P"


def _size_star(req: VideoGenRequest, aspect: str) -> str:
    res = _resolution(req)
    table = {
        ("720P", "16:9"): "1280*720",
        ("720P", "9:16"): "720*1280",
        ("720P", "1:1"): "960*960",
        ("480P", "16:9"): "832*480",
        ("480P", "9:16"): "480*832",
        ("480P", "1:1"): "624*624",
        ("1080P", "16:9"): "1920*1080",
        ("1080P", "9:16"): "1080*1920",
        ("1080P", "1:1"): "1440*1440",
    }
    return table.get((res, aspect), "1280*720")


def _parameters(model_name: str, req: VideoGenRequest, aspect: str) -> dict[str, Any]:
    params: dict[str, Any] = {
        "duration": int(req.duration_sec),
        "prompt_extend": False,
        "watermark": False,
    }
    mid = (model_name or "").lower()
    if "wan3" in mid or "2.7" in mid:
        params["resolution"] = _resolution(req)
        params["ratio"] = aspect
    else:
        params["size"] = _size_star(req, aspect)
    return params
    raw = (req.resolution or "720p").strip().upper().replace("P", "")
    if raw in ("480", "720", "1080"):
        return f"{raw}P"
    return "720P"


def _is_wan3(model_id: str) -> bool:
    return "wan3" in (model_id or "").lower()


class DashScopeVideoAdapter:
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
        self.api_root = dashscope_api_root(self.base_url)
        self.api_key = api_key or ""
        self.data_dir = Path(data_dir)
        self.profile = profile
        self.model_id = model_id or ""

    def _headers(self) -> dict[str, str]:
        return dashscope_headers(self.api_key)

    async def probe(self, timeout: float = 12.0) -> dict:
        url = f"{self.api_root}/tasks/0"
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
        code = str(payload.get("code") or _output(payload).get("code") or "").lower()
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
            "error": message or f"百炼视频接口返回 HTTP {resp.status_code}",
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
            raise RuntimeError("请填写百炼模型 ID，例如 wan2.6-t2v")
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
        parameters = _parameters(model_name, req, aspect)
        body_input: dict[str, Any] = {"prompt": prompt}
        if image_uri:
            if _is_wan3(model_name):
                body_input["media"] = [{"type": "first_frame", "url": image_uri}]
                parameters["ratio"] = "adaptive"
            else:
                body_input["img_url"] = image_uri
        body = {"model": model_name, "input": body_input, "parameters": parameters}
        headers = self._headers()
        create_url = f"{self.api_root}/services/aigc/video-generation/video-synthesis"
        http_timeout = httpx.Timeout(30.0, connect=15.0)
        job_id = active_jobs.register_job(
            session_id, base_url=self.base_url, prompt_id="",
            backend="dashscope", kind="dashscope_video")
        runtime.set_cancel_headers(job_id, headers)
        store = get_store()
        if store is not None:
            try:
                store.create(
                    kind="dashscope_video", backend="dashscope",
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
                "正在提交百炼图生视频…" if image_uri else "正在提交百炼生视频…",
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
                    created = await client.post(create_url, json=body, headers=headers)
                except httpx.ConnectError as exc:
                    raise RuntimeError(
                        connect_error_message(self.base_url, exc)) from exc
                payload = {}
                try:
                    parsed = created.json()
                    if isinstance(parsed, dict):
                        payload = parsed
                except Exception:  # noqa: BLE001
                    payload = {}
                if created.status_code >= 400 or payload.get("code"):
                    raise RuntimeError(
                        "百炼生视频提交失败："
                        + (dashscope_error_text(
                            payload, f"HTTP {created.status_code}") or "未知错误"))
                task_id = dashscope_task_id(payload)
                if not task_id:
                    raise RuntimeError("百炼生视频未返回任务 ID")
                active_jobs.bind_remote(session_id, task_id, job_id=job_id)
                if store is not None:
                    try:
                        store.set_remote_id(job_id, task_id)
                    except Exception:  # noqa: BLE001
                        logger.debug("remote_jobs bind skip", exc_info=True)
                if on_progress:
                    await on_progress("waiting", f"百炼渲染中…任务 {task_id[:12]}")
                video_url = ""
                wait_t0 = time.perf_counter()
                last_wait_ping = 0.0
                while True:
                    runtime.raise_if_cancelled(job_id=job_id, session_id=session_id)
                    info = await http_get_json_resilient(
                        client,
                        f"{self.api_root}/tasks/{task_id}",
                        headers=dashscope_query_headers(self.api_key),
                        job_id=job_id, session_id=session_id,
                        on_progress=on_progress, stage="waiting",
                        label="百炼视频任务",
                    )
                    if not isinstance(info, dict):
                        info = {}
                    status = dashscope_status(info)
                    if status == "SUCCEEDED":
                        video_url, dur = dashscope_video_url(info)
                        if dur:
                            duration = dur
                        break
                    if status in ("FAILED", "CANCELED", "UNKNOWN"):
                        raise RuntimeError(
                            dashscope_error_text(info, "百炼生视频失败"))
                    now = time.perf_counter()
                    if on_progress and now - last_wait_ping >= 8.0:
                        elapsed = int(now - wait_t0)
                        await on_progress("waiting", f"百炼渲染中…已等待 {elapsed}s")
                        last_wait_ping = now
                    if store is not None:
                        try:
                            store.touch(job_id)
                        except Exception:  # noqa: BLE001
                            pass
                    await sleep_or_cancel(2.0, job_id=job_id, session_id=session_id)
                if not video_url:
                    raise RuntimeError("百炼生视频完成但未返回视频地址")
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
                    await on_progress("saving", "百炼已完成，正在下载成片…")
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
