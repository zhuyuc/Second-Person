"""OpenAI 兼容文生视频：只追加 /videos，不改成可灵或百炼路径。"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from infrastructure.image_gen import active_jobs
from infrastructure.provider_modality import endpoint_url
from infrastructure.remote_jobs import JobCancelled, get_store, runtime

from .cloud_adapter import connect_error_message
from .profiles import VideoProfile
from .types import VideoGenRequest, VideoGenResult

logger = logging.getLogger("second_person.video_gen.openai")

ProgressCb = Callable[[str, str], Awaitable[None]] | None


class OpenAIVideoAdapter:
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

    def _headers(self) -> dict[str, str]:
        key = self.api_key.strip()
        if key.lower().startswith("bearer "):
            key = key[7:].strip()
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    async def probe(self, timeout: float = 12.0) -> dict:
        url = endpoint_url(self.base_url, "openai_compatible", "/models")
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url, headers=self._headers())
        except httpx.ConnectError as exc:
            return {"ok": False, "error": connect_error_message(self.base_url, exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:200]}
        if resp.status_code < 400:
            return {"ok": True, "protocol": "openai_compatible"}
        if resp.status_code in (401, 403):
            return {"ok": False, "error": "鉴权失败，请检查 API Key"}
        return {"ok": False, "error": f"视频接口返回 HTTP {resp.status_code}"}

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
        headers = self._headers()
        create_url = endpoint_url(self.base_url, "openai_compatible", "/videos")
        body = {
            "model": model_name,
            "prompt": (req.prompt or "")[:4000],
            "seconds": str(int(req.duration_sec)),
            "size": req.size,
        }
        job_id = active_jobs.register_job(
            session_id, base_url=self.base_url, prompt_id="",
            backend="openai", kind="openai_video")
        store = get_store()
        settle_status = "failed"
        settle_err: str | None = None
        settle_ref: str | None = None
        try:
            runtime.raise_if_cancelled(job_id=job_id, session_id=session_id)
            if on_progress:
                await on_progress("submit", "正在提交 OpenAI 兼容视频接口…")
            timeout = httpx.Timeout(60.0, connect=15.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                created = await client.post(create_url, json=body, headers=headers)
                payload = _json(created)
                if created.status_code >= 400:
                    raise RuntimeError(
                        f"视频接口失败 HTTP {created.status_code}："
                        f"{_err(payload, created.text)}")
                video_id = str(payload.get("id") or "").strip()
                if not video_id:
                    raise RuntimeError("OpenAI 兼容视频接口没有返回任务 id")
                deadline = time.perf_counter() + float(self.profile.timeout_sec or 600)
                status = str(payload.get("status") or "queued")
                while status not in ("completed", "failed", "cancelled"):
                    runtime.raise_if_cancelled(job_id=job_id, session_id=session_id)
                    if time.perf_counter() > deadline:
                        raise RuntimeError("视频生成超时")
                    if on_progress:
                        await on_progress("poll", "视频生成中…")
                    await asyncio.sleep(5)
                    polled = await client.get(
                        f"{create_url}/{video_id}", headers=headers)
                    payload = _json(polled)
                    if polled.status_code >= 400:
                        raise RuntimeError(
                            f"查询视频失败 HTTP {polled.status_code}："
                            f"{_err(payload, polled.text)}")
                    status = str(payload.get("status") or "")
                if status != "completed":
                    raise RuntimeError(f"视频生成失败：{_err(payload, status)}")
                if on_progress:
                    await on_progress("saving", "正在下载成片…")
                content = await client.get(
                    f"{create_url}/{video_id}/content", headers=headers)
            if content.status_code >= 400:
                raise RuntimeError(
                    f"下载视频失败 HTTP {content.status_code}：{(content.text or '')[:200]}")
            out_dir = self.data_dir / "chat_videos"
            out_dir.mkdir(parents=True, exist_ok=True)
            fname = f"genv_{uuid.uuid4().hex[:12]}.mp4"
            (out_dir / fname).write_bytes(content.content)
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
                duration_sec=float(req.duration_sec),
                fps=req.fps,
                provider_id=provider_id,
                model_id=model_name,
                latency_ms=latency_ms,
                backend="cloud",
                summary=(
                    f"已生成 1 条视频（约 {int(req.duration_sec)}s，"
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


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _err(payload: dict, fallback: str) -> str:
    err = payload.get("error")
    if isinstance(err, dict) and err.get("message"):
        return str(err["message"])[:240]
    if payload.get("message"):
        return str(payload["message"])[:240]
    return (fallback or "")[:240]
