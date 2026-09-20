"""Google 文生视频：固定走 predictLongRunning，不按地址猜厂商。"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin

import httpx

from infrastructure.remote_jobs.fetch import fetch_url_bytes

from .profiles import VideoProfile
from .types import VideoGenRequest, VideoGenResult

logger = logging.getLogger("second_person.video_gen.google")

ProgressCb = Callable[[str, str], Awaitable[None]] | None


def _video_uri(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("uri", "video_url", "url"):
        if isinstance(payload.get(key), str) and payload[key].startswith("http"):
            return payload[key]
    for value in payload.values():
        if isinstance(value, dict):
            found = _video_uri(value)
            if found:
                return found
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    found = _video_uri(item)
                    if found:
                        return found
    return ""


class GoogleVideoAdapter:
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

    def _op_url(self, name: str) -> str:
        if name.startswith("http"):
            return name
        return urljoin(self.base_url + "/", name.lstrip("/"))

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
        req: VideoGenRequest,
        *,
        provider_id: str = "",
        model_id: str = "",
        session_id: str = "",
        on_progress: ProgressCb = None,
    ) -> VideoGenResult:
        t0 = time.perf_counter()
        model_name = (model_id or req.model_id or self.model_id).strip()
        submit = (
            f"{self.base_url}/models/{model_name}:predictLongRunning"
            f"?key={self.api_key}"
        )
        body: dict[str, Any] = {
            "instances": [{"prompt": (req.prompt or "")[:4000]}],
            "parameters": {"durationSeconds": int(req.duration_sec)},
        }
        if on_progress:
            await on_progress("submit", "正在提交 Google 视频接口…")
        timeout = httpx.Timeout(60.0, connect=15.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            created = await client.post(submit, json=body)
            payload = created.json() if created.content else {}
            if created.status_code >= 400:
                raise RuntimeError(
                    f"Google 视频失败 HTTP {created.status_code}："
                    f"{(created.text or '')[:240]}")
            if not isinstance(payload, dict):
                payload = {}
            name = str(payload.get("name") or "")
            if not name:
                raise RuntimeError("Google 视频接口没有返回任务名")
            deadline = time.perf_counter() + float(self.profile.timeout_sec or 600)
            while not payload.get("done"):
                if time.perf_counter() > deadline:
                    raise RuntimeError("视频生成超时")
                if on_progress:
                    await on_progress("poll", "视频生成中…")
                await asyncio.sleep(5)
                polled = await client.get(
                    self._op_url(name) + f"?key={self.api_key}")
                payload = polled.json() if polled.content else {}
                if polled.status_code >= 400:
                    raise RuntimeError(
                        f"查询 Google 视频失败 HTTP {polled.status_code}")
                if not isinstance(payload, dict):
                    payload = {}
            if payload.get("error"):
                raise RuntimeError(f"视频生成失败：{str(payload['error'])[:240]}")
        uri = _video_uri(payload.get("response") if isinstance(payload.get("response"), dict) else payload)
        if not uri:
            raise RuntimeError("Google 视频接口没有返回成片地址")
        if on_progress:
            await on_progress("saving", "正在下载成片…")
        video_bytes = await fetch_url_bytes(uri, session_id=session_id)
        out_dir = self.data_dir / "chat_videos"
        out_dir.mkdir(parents=True, exist_ok=True)
        fname = f"genv_{uuid.uuid4().hex[:12]}.mp4"
        (out_dir / fname).write_bytes(video_bytes)
        latency_ms = int((time.perf_counter() - t0) * 1000)
        logger.info("google video ok file=%s ms=%s", fname, latency_ms)
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
