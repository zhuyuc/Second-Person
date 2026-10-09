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
                # 鉴权/参数类错误不盲重试，带上正文避免只剩状态码
                detail = (resp.text or "").strip().replace("\n", " ")[:180]
                msg = f"查询{label}失败 HTTP {resp.status_code}"
                if detail:
                    msg = f"{msg}：{detail}"
                raise RuntimeError(msg)
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


def _comfy_prompt_in_queue(payload: Any, prompt_id: str) -> bool:
    if not isinstance(payload, dict) or not prompt_id:
        return False
    for key in ("queue_running", "queue_pending"):
        for item in payload.get(key) or []:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                if str(item[1]) == str(prompt_id):
                    return True
    return False


async def wait_comfy_history_outputs(
    client: httpx.AsyncClient,
    base_url: str,
    prompt_id: str,
    *,
    job_id: str = "",
    session_id: str = "",
    on_progress: ProgressCb = None,
    stage: str = "sampling",
    label: str = "本地生成",
    soft_timeout_sec: float = 180.0,
    poll_interval: float = 0.8,
    on_tick: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """跟到 Comfy history 产出 outputs；远端仍在跑时不得因墙钟结案 failed。

    soft_timeout_sec 仅用于「队列/历史都看不到任务」时的孤儿判定与进度文案，
    不用于杀掉仍在 queue_running / queue_pending 的任务。
    """
    root = (base_url or "").rstrip("/")
    t0 = time.perf_counter()
    soft = max(5.0, float(soft_timeout_sec or 180.0))
    orphan_grace = min(45.0, soft)
    last_progress_at = 0.0
    last_active_at = time.perf_counter()
    saw_active = False

    while True:
        runtime.raise_if_cancelled(job_id=job_id, session_id=session_id)
        entry: dict[str, Any] = {}
        try:
            hist = await client.get(f"{root}/history/{prompt_id}")
        except (httpx.TimeoutException, httpx.TransportError):
            if on_progress:
                await on_progress(stage, f"查询{label}队列较慢，继续等待…")
            await sleep_or_cancel(2.0, job_id=job_id, session_id=session_id)
            continue

        if hist.status_code == 200:
            data = hist.json() or {}
            raw_entry = data.get(prompt_id) or {}
            if isinstance(raw_entry, dict):
                entry = raw_entry
                status = entry.get("status") or {}
                for m in status.get("messages") or []:
                    if isinstance(m, list) and m and m[0] == "execution_error":
                        raise RuntimeError(f"ComfyUI 执行失败: {m}")
                if entry.get("outputs"):
                    return entry["outputs"]

        active = False
        try:
            q = await client.get(f"{root}/queue")
            if q.status_code == 200:
                active = _comfy_prompt_in_queue(q.json() or {}, prompt_id)
        except (httpx.TimeoutException, httpx.TransportError):
            # 查队列失败时保守视为仍可能在跑，避免误杀
            active = True

        now = time.perf_counter()
        if active:
            saw_active = True
            last_active_at = now

        if on_progress and now - last_progress_at >= 8.0:
            elapsed = int(now - t0)
            await on_progress(
                stage, f"{label}进行中（已等待 {elapsed}s）…")
            last_progress_at = now
        if on_tick is not None:
            try:
                on_tick()
            except Exception:  # noqa: BLE001
                pass

        elapsed = now - t0
        inactive_for = now - last_active_at
        # 从未进过队列且长期无历史：提交可能丢失
        if (elapsed >= soft and not saw_active and not entry
                and inactive_for >= orphan_grace):
            raise RuntimeError(
                f"ComfyUI {label}超时（>{int(soft)}s 且队列中无此任务），"
                "请检查本地服务或工作流")
        # 曾在跑但已消失、又无 outputs：给短宽限后失败（崩溃/被清队列）
        if (saw_active and not active and not entry.get("outputs")
                and inactive_for >= orphan_grace and elapsed >= soft):
            # 最后再捞一次 history（刚好写完的竞态）
            try:
                hist2 = await client.get(f"{root}/history/{prompt_id}")
                if hist2.status_code == 200:
                    e2 = (hist2.json() or {}).get(prompt_id) or {}
                    if isinstance(e2, dict) and e2.get("outputs"):
                        return e2["outputs"]
            except (httpx.TimeoutException, httpx.TransportError):
                pass
            raise RuntimeError(
                f"ComfyUI {label}中断（队列已空且无输出，已等待 {int(elapsed)}s）")

        await sleep_or_cancel(
            poll_interval, job_id=job_id, session_id=session_id)


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
