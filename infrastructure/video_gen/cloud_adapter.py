"""可灵云端文生视频：新版路径 API 为主，旧版 /v1/videos/text2video 兼容。"""
from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import httpx
import asyncio

from infrastructure.image_gen import active_jobs
from infrastructure.remote_jobs import JobCancelled, get_store, runtime
from infrastructure.remote_jobs.fetch import (
    fetch_url_bytes, http_get_json_resilient, sleep_or_cancel,
)

from .kling_auth import (
    is_legacy_kling_model, kling_auth_header, looks_like_ak_sk,
)
from .profiles import VideoProfile, aspect_ratio_of
from .types import VideoGenRequest, VideoGenResult

logger = logging.getLogger("second_person.video_gen.cloud")

ProgressCb = Callable[[str, str], Awaitable[None]] | None

_NEW_KEY_HINT = (
    "新版可灵（模型 ID 如 kling-3.0-turbo）请使用控制台 API Key，"
    "不要填 AccessKey:SecretKey"
)


def connect_error_message(base_url: str, exc: BaseException) -> str:
    """把 TLS/建连失败翻成可操作提示，避免只剩「无法连接」。"""
    detail = str(exc or "")
    host = (urlparse(base_url or "").hostname or "").lower()
    ssl_plain = "WRONG_VERSION_NUMBER" in detail or "wrong version number" in detail.lower()
    if ssl_plain or "SEC_E_INVALID_TOKEN" in detail:
        if "beijing" in host or "klingai.com" in host:
            return (
                "无法连接云端视频服务：HTTPS 握手失败。"
                "官方域名为 https://api-beijing.klingai.com，"
                "请检查本机 DNS/代理是否劫持该域名"
            )
        return "无法连接云端视频服务：目标地址 443 端口未提供 HTTPS"
    return f"无法连接云端视频服务：{detail[:160]}" if detail else "无法连接云端视频服务"


def _task_item(payload: dict) -> dict:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list):
        return data[0] if data and isinstance(data[0], dict) else {}
    if isinstance(data, dict):
        return data
    return payload if isinstance(payload, dict) else {}


def _extract_task_id(payload: dict) -> str:
    item = _task_item(payload)
    return str(
        item.get("id")
        or item.get("task_id")
        or payload.get("task_id")
        or payload.get("id")
        or ""
    ).strip()


def _extract_video(payload: dict) -> tuple[str, float | None]:
    item = _task_item(payload)
    outputs = item.get("outputs") if isinstance(item.get("outputs"), list) else []
    for out in outputs:
        if not isinstance(out, dict):
            continue
        if out.get("type") in ("video", None) and out.get("url"):
            return str(out.get("url") or "").strip(), _as_float(out.get("duration"))
    result = item.get("task_result") if isinstance(item.get("task_result"), dict) else {}
    videos = result.get("videos") if isinstance(result.get("videos"), list) else []
    first = videos[0] if videos and isinstance(videos[0], dict) else {}
    url = (
        first.get("url")
        or item.get("url")
        or item.get("video_url")
        or payload.get("url")
        or ""
    )
    duration = first.get("duration") or item.get("duration")
    return str(url).strip(), _as_float(duration)


def _as_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _task_status(payload: dict) -> str:
    item = _task_item(payload)
    return str(
        item.get("status")
        or item.get("task_status")
        or payload.get("status")
        or ""
    ).lower()


def _task_fail_message(payload: dict) -> str:
    item = _task_item(payload)
    return str(
        item.get("message")
        or item.get("task_status_msg")
        or payload.get("message")
        or "云端生视频失败"
    )[:240]


def _api_error(payload: dict) -> str | None:
    code = payload.get("code") if isinstance(payload, dict) else None
    if code in (None, 0, "0", 200, "200"):
        return None
    return str(payload.get("message") or f"云端生视频失败 code={code}")


class KlingVideoAdapter:
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
        self.model_id = model_id or "kling-3.0-turbo"

    def _legacy(self) -> bool:
        return is_legacy_kling_model(self.model_id)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        headers.update(kling_auth_header(self.api_key, model_id=self.model_id))
        return headers

    def _new_key_required_error(self) -> str | None:
        if not self._legacy() and looks_like_ak_sk(self.api_key):
            return _NEW_KEY_HINT
        return None

    async def probe(self, timeout: float = 12.0) -> dict:
        blocked = self._new_key_required_error()
        if blocked:
            return {"ok": False, "error": blocked}
        if self._legacy():
            url = f"{self.base_url}/v1/videos/text2video"
            params: dict[str, Any] = {"pageNum": 1, "pageSize": 1}
        else:
            url = f"{self.base_url}/tasks"
            params = {"task_ids": "0"}
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(
                    url, headers=self._headers(), params=params)
            if resp.status_code < 400:
                return {"ok": True, "protocol": "video"}
            if resp.status_code in (401, 403):
                body = ""
                try:
                    body = str((resp.json() or {}).get("message") or "")
                except Exception:  # noqa: BLE001
                    body = (resp.text or "")[:120]
                return {"ok": False, "error": body or "鉴权失败，请检查 API Key"}
            return {"ok": False, "error": f"视频接口返回 HTTP {resp.status_code}"}
        except httpx.ConnectError as exc:
            logger.warning("kling probe connect fail url=%s err=%s", url, exc)
            return {"ok": False, "error": connect_error_message(self.base_url, exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:200]}

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
        model_name = model_id or req.model_id or self.model_id
        legacy = is_legacy_kling_model(model_name)
        if not legacy and looks_like_ak_sk(self.api_key):
            raise RuntimeError(_NEW_KEY_HINT)
        aspect = aspect_ratio_of(self.profile, req.size)
        image_name = (req.image_filename or "").strip() or None
        image_uri = ""
        if image_name:
            from .images import image_file_to_data_uri, resolve_chat_image
            img_path = resolve_chat_image(self.data_dir, image_name)
            if img_path is None:
                raise RuntimeError(f"找不到参考图：{image_name}")
            image_uri = image_file_to_data_uri(img_path)
            if on_progress:
                await on_progress("submit", "正在提交云端图生视频…")
        elif on_progress:
            await on_progress("submit", "正在提交云端生视频…")
        # 单次 HTTP 超时与「跟到远端终态」分离：本地墙钟不得在远端仍 running 时判死刑。
        http_timeout = httpx.Timeout(30.0, connect=15.0)
        headers = {"Content-Type": "application/json"}
        headers.update(kling_auth_header(self.api_key, model_id=model_name))
        # 有参考图 → 图生视频；否则文生视频
        if image_uri:
            if legacy:
                create_url = f"{self.base_url}/v1/videos/image2video"
                body: dict[str, Any] = {
                    "model_name": model_name,
                    "image": image_uri,
                    "prompt": (req.prompt or "")[:2500],
                    "duration": str(int(req.duration_sec)),
                    "mode": "std",
                }
                if (req.negative_prompt or "").strip():
                    body["negative_prompt"] = req.negative_prompt.strip()[:500]
            else:
                create_url = f"{self.base_url}/image-to-video/{model_name}"
                prompt = (req.prompt or "")[:2500]
                if (req.negative_prompt or "").strip():
                    prompt = (
                        f"{prompt}\n不要出现：{req.negative_prompt.strip()}"
                    )[:2500]
                body = {
                    "prompt": prompt,
                    "image_url": image_uri,
                    "settings": {
                        "duration": int(req.duration_sec),
                        "resolution": (req.resolution or "720p").strip() or "720p",
                        "aspect_ratio": aspect,
                    },
                }
        elif legacy:
            create_url = f"{self.base_url}/v1/videos/text2video"
            body = {
                "model_name": model_name,
                "prompt": (req.prompt or "")[:2500],
                "aspect_ratio": aspect,
                "duration": str(int(req.duration_sec)),
                "mode": "std",
            }
            if (req.negative_prompt or "").strip():
                body["prompt"] = (
                    f"{body['prompt']}\n不要出现：{req.negative_prompt.strip()}"
                )[:2500]
        else:
            create_url = f"{self.base_url}/text-to-video/{model_name}"
            prompt = (req.prompt or "")[:2500]
            if (req.negative_prompt or "").strip():
                prompt = f"{prompt}\n不要出现：{req.negative_prompt.strip()}"[:2500]
            body = {
                "prompt": prompt,
                "settings": {
                    "duration": int(req.duration_sec),
                    "resolution": (req.resolution or "720p").strip() or "720p",
                    "aspect_ratio": aspect,
                },
            }
        # 轮询路径：legacy I2V 用 image2video；T2V 用 text2video；新版统一 /tasks
        legacy_poll_base = (
            f"{self.base_url}/v1/videos/image2video"
            if image_uri and legacy
            else f"{self.base_url}/v1/videos/text2video"
        )
        job_id = active_jobs.register_job(
            session_id, base_url=self.base_url, prompt_id="",
            backend="kling", kind="kling_video")
        runtime.set_cancel_headers(job_id, headers)
        store = get_store()
        if store is not None:
            try:
                store.create(
                    kind="kling_video", backend="kling",
                    session_id=session_id, base_url=self.base_url,
                    job_id=job_id,
                    meta={"mode": "i2v" if image_uri else "t2v",
                          "image_filename": image_name or ""})
            except Exception:  # noqa: BLE001
                logger.debug("remote_jobs create skip", exc_info=True)
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
                if created.status_code >= 400:
                    raise RuntimeError(
                        f"云端生视频提交失败 HTTP {created.status_code}："
                        f"{(created.text or '')[:240]}")
                payload = created.json()
                api_err = _api_error(payload) if isinstance(payload, dict) else None
                if api_err:
                    raise RuntimeError(api_err)
                task_id = _extract_task_id(payload)
                if not task_id:
                    raise RuntimeError("云端生视频未返回任务 ID")
                active_jobs.bind_remote(session_id, task_id, job_id=job_id)
                if store is not None:
                    try:
                        store.set_remote_id(job_id, task_id)
                    except Exception:  # noqa: BLE001
                        logger.debug("remote_jobs bind skip", exc_info=True)
                if on_progress:
                    await on_progress(
                        "waiting", f"云端渲染中…任务 {task_id[:12]}")
                video_url = ""
                wait_t0 = time.perf_counter()
                last_wait_ping = 0.0
                while True:
                    runtime.raise_if_cancelled(
                        job_id=job_id, session_id=session_id)
                    if legacy:
                        info = await http_get_json_resilient(
                            client,
                            f"{legacy_poll_base}/{task_id}",
                            headers=headers,
                            job_id=job_id, session_id=session_id,
                            on_progress=on_progress, stage="waiting",
                            label="云端视频任务",
                        )
                    else:
                        info = await http_get_json_resilient(
                            client,
                            f"{self.base_url}/tasks",
                            headers=headers,
                            params={"task_ids": task_id},
                            job_id=job_id, session_id=session_id,
                            on_progress=on_progress, stage="waiting",
                            label="云端视频任务",
                        )
                    if not isinstance(info, dict):
                        info = {}
                    status = _task_status(info)
                    if status in ("succeed", "succeeded", "success", "completed"):
                        video_url, dur = _extract_video(info)
                        if dur:
                            duration = dur
                        break
                    if status in ("failed", "fail", "error"):
                        raise RuntimeError(_task_fail_message(info))
                    now = time.perf_counter()
                    if on_progress and now - last_wait_ping >= 8.0:
                        elapsed = int(now - wait_t0)
                        await on_progress(
                            "waiting",
                            f"云端渲染中…已等待 {elapsed}s",
                        )
                        last_wait_ping = now
                    if store is not None:
                        try:
                            store.touch(job_id)
                        except Exception:  # noqa: BLE001
                            pass
                    await sleep_or_cancel(
                        2.0, job_id=job_id, session_id=session_id)
                if not video_url:
                    raise RuntimeError("云端生视频完成但未返回视频地址")
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
                    await on_progress("saving", "云端已完成，正在下载成片…")
                # 下载跟到成功或取消；单次超时只重试，绝不因下载墙钟判失败。
                video_bytes = await fetch_url_bytes(
                    video_url,
                    job_id=job_id, session_id=session_id,
                    on_progress=on_progress, stage="downloading",
                    label="成片",
                )
                if on_progress:
                    await on_progress("saving", "成片已下载，正在保存…")
            out_dir = self.data_dir / "chat_videos"
            out_dir.mkdir(parents=True, exist_ok=True)
            fname = f"genv_{uuid.uuid4().hex[:12]}.mp4"
            (out_dir / fname).write_bytes(video_bytes)
            settle_status = "succeeded"
            settle_ref = fname
            latency_ms = int((time.perf_counter() - t0) * 1000)
            result = VideoGenResult(
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
            logger.info("cloud video_gen ok provider=%s file=%s ms=%s",
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
            if store is not None and settle_status != "running":
                try:
                    store.settle(
                        job_id, status=settle_status,
                        result_ref=settle_ref, error_message=settle_err)
                except Exception:  # noqa: BLE001
                    logger.debug("remote_jobs settle skip", exc_info=True)
