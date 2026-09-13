"""远端结果获取：瞬时超时只重试，跟到成功或用户取消。

契约：远端已产出（或仍在跑）时，本地墙钟不得结案为 failed。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from . import JobCancelled, runtime

logger = logging.getLogger("second_person.remote_jobs.fetch")

ProgressCb = Callable[[str, str], Awaitable[None]] | None


async def sleep_or_cancel(
    seconds: float, *, job_id: str = "", session_id: str = "",
) -> None:
    end = time.monotonic() + max(0.0, float(seconds))
    while True:
        runtime.raise_if_cancelled(job_id=job_id, session_id=session_id)
        left = end - time.monotonic()
        if left <= 0:
            return
        await asyncio.sleep(min(0.5, left))


async def fetch_url_bytes(
    url: str,
    *,
    job_id: str = "",
    session_id: str = "",
    on_progress: ProgressCb = None,
    stage: str = "downloading",
    connect_timeout: float = 30.0,
    read_timeout: float = 180.0,
    label: str = "成片",
) -> bytes:
    """下载 URL 直到成功或取消；单次读超时只重试，不抛失败。"""
    url = (url or "").strip()
    if not url:
        raise RuntimeError(f"缺少{label}下载地址")
    attempt = 0
    while True:
        runtime.raise_if_cancelled(job_id=job_id, session_id=session_id)
        attempt += 1
        try:
            timeout = httpx.Timeout(read_timeout, connect=connect_timeout)
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("GET", url) as resp:
                    if resp.status_code >= 400:
                        raise RuntimeError(
                            f"下载{label}失败 HTTP {resp.status_code}")
                    total = int(resp.headers.get("content-length") or 0)
                    got = 0
                    last_ping = 0.0
                    chunks: list[bytes] = []
                    if on_progress and attempt > 1:
                        await on_progress(
                            stage, f"正在重试下载{label}…第 {attempt} 次")
                    async for chunk in resp.aiter_bytes(64 * 1024):
                        runtime.raise_if_cancelled(
                            job_id=job_id, session_id=session_id)
                        if not chunk:
                            continue
                        chunks.append(chunk)
                        got += len(chunk)
                        now = time.perf_counter()
                        if on_progress and now - last_ping >= 1.0:
                            if total > 0:
                                pct = min(99, int(got * 100 / total))
                                await on_progress(
                                    stage, f"正在下载{label}…{pct}%")
                            else:
                                mb = got / (1024 * 1024)
                                await on_progress(
                                    stage, f"正在下载{label}…已收 {mb:.1f}MB")
                            last_ping = now
                    data = b"".join(chunks)
            if not data:
                raise RuntimeError(f"下载{label}失败：空内容")
            return data
        except JobCancelled:
            raise
        except httpx.TimeoutException:
            if on_progress:
                await on_progress(
                    stage,
                    f"下载{label}较慢，继续重试中…（第 {attempt} 次）",
                )
            logger.info("download timeout retry attempt=%s url=%s",
                        attempt, url[:120])
            await sleep_or_cancel(2.0, job_id=job_id, session_id=session_id)
        except (httpx.TransportError, httpx.HTTPError, RuntimeError) as exc:
            # 4xx/空内容等也重试（CDN 偶发）；取消除外
            if on_progress:
                await on_progress(
                    stage,
                    f"下载{label}暂时失败，正在重试…（{type(exc).__name__}）",
                )
            logger.info("download error retry attempt=%s err=%s",
                        attempt, exc)
            await sleep_or_cancel(3.0, job_id=job_id, session_id=session_id)


async def http_get_json_resilient(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict | None = None,
    params: dict | None = None,
    job_id: str = "",
    session_id: str = "",
    on_progress: ProgressCb = None,
    stage: str = "waiting",
    label: str = "任务状态",
) -> Any:
    """GET JSON：瞬时超时/网络错误只重试，不结案。"""
    attempt = 0
    while True:
        runtime.raise_if_cancelled(job_id=job_id, session_id=session_id)
        attempt += 1
        try:
            resp = await client.get(url, headers=headers, params=params)
            if resp.status_code >= 500:
                raise RuntimeError(f"查询{label} HTTP {resp.status_code}")
            if resp.status_code >= 400:
                # 鉴权/参数类错误不盲重试
                raise RuntimeError(
                    f"查询{label}失败 HTTP {resp.status_code}")
            return resp.json()
        except JobCancelled:
            raise
        except RuntimeError as exc:
            msg = str(exc)
            if "HTTP 4" in msg:
                raise
            if on_progress:
                await on_progress(
                    stage, f"查询{label}暂时失败，正在重试…")
            await sleep_or_cancel(2.0, job_id=job_id, session_id=session_id)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if on_progress and attempt % 3 == 1:
                await on_progress(
                    stage, f"查询{label}较慢，继续等待…")
            logger.debug("poll retry attempt=%s err=%s", attempt, exc)
            await sleep_or_cancel(2.0, job_id=job_id, session_id=session_id)


async def http_get_bytes_resilient(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict | None = None,
    job_id: str = "",
    session_id: str = "",
    on_progress: ProgressCb = None,
    stage: str = "saving",
    label: str = "成片",
) -> bytes:
    """经已有 client 拉字节；超时/传输错误重试至成功或取消。"""
    attempt = 0
    while True:
        runtime.raise_if_cancelled(job_id=job_id, session_id=session_id)
        attempt += 1
        try:
            resp = await client.get(url, params=params)
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"下载{label}失败 HTTP {resp.status_code}")
            if not resp.content:
                raise RuntimeError(f"下载{label}失败：空内容")
            return resp.content
        except JobCancelled:
            raise
        except httpx.TimeoutException:
            if on_progress:
                await on_progress(
                    stage, f"读取{label}较慢，继续重试…（第 {attempt} 次）")
            await sleep_or_cancel(2.0, job_id=job_id, session_id=session_id)
        except (httpx.TransportError, RuntimeError) as exc:
            if isinstance(exc, RuntimeError) and "HTTP 4" in str(exc):
                # 404 等：仍重试一段时间（Comfy 偶发未就绪）
                pass
            if on_progress:
                await on_progress(
                    stage, f"读取{label}暂时失败，正在重试…")
            logger.info("view retry attempt=%s err=%s", attempt, exc)
            await sleep_or_cancel(2.0, job_id=job_id, session_id=session_id)
