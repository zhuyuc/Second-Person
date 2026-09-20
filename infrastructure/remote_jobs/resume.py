"""启动时续跟 remote_jobs：有句柄则继续取结果，禁止盲标 failed 后重提。"""
from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

from infrastructure.background_tasks import track_task
from infrastructure.remote_jobs import JobCancelled, get_store, runtime
from infrastructure.remote_jobs.fetch import fetch_url_bytes, http_get_json_resilient

logger = logging.getLogger("second_person.remote_jobs.resume")


def _meta(row: dict[str, Any]) -> dict:
    raw = row.get("meta_json") or "{}"
    try:
        data = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def schedule_resume_running_jobs(container) -> int:
    """扫描 running 任务并后台续跟；返回调度数量。"""
    store = getattr(container, "remote_jobs", None) or get_store()
    if store is None:
        return 0
    rows = store.list_running(limit=50)
    n = 0
    for row in rows:
        kind = (row.get("kind") or "").strip()
        jid = row.get("id") or ""
        if not jid:
            continue
        meta = _meta(row)
        if meta.get("phase") == "download" and meta.get("result_url"):
            track_task(
                _resume_download(container, row, meta),
                name=f"remote_job_dl_{jid[:12]}")
            n += 1
        elif kind == "kling_video" and (row.get("remote_id") or "").strip():
            track_task(
                _resume_kling_poll(container, row),
                name=f"remote_job_kling_{jid[:12]}")
            n += 1
        elif kind == "dashscope_video" and (row.get("remote_id") or "").strip():
            track_task(
                _resume_dashscope_poll(container, row),
                name=f"remote_job_dashscope_{jid[:12]}")
            n += 1
        elif kind in ("comfy_video", "comfy_image") and (row.get("remote_id") or "").strip():
            track_task(
                _resume_comfy(container, row, meta),
                name=f"remote_job_comfy_{jid[:12]}")
            n += 1
    if n:
        logger.info("scheduled resume for %s remote_jobs", n)
    return n


async def _resume_download(container, row: dict, meta: dict) -> None:
    store = get_store()
    jid = row["id"]
    session_id = row.get("session_id") or ""
    url = meta.get("result_url") or ""
    live = runtime.register(
        kind=row.get("kind") or "media",
        backend=row.get("backend") or "",
        session_id=session_id,
        base_url=row.get("base_url") or "",
        remote_id=row.get("remote_id") or "",
        job_id=jid,
    )
    try:
        data = await fetch_url_bytes(
            url, job_id=jid, session_id=session_id, label="成片")
        kind = row.get("kind") or ""
        data_dir = Path(container.data_dir)
        if "image" in kind:
            out = data_dir / "chat_images"
            fname = f"gen_{uuid.uuid4().hex[:12]}.png"
        else:
            out = data_dir / "chat_videos"
            fname = f"genv_{uuid.uuid4().hex[:12]}.mp4"
        out.mkdir(parents=True, exist_ok=True)
        (out / fname).write_bytes(data)
        if store:
            store.settle(jid, status="succeeded", result_ref=fname)
        await _maybe_finish_workshop(container, row, fname, kind)
        logger.info("resume download ok job=%s file=%s", jid, fname)
    except JobCancelled:
        if store:
            store.settle(jid, status="cancelled", error_message="已停止生成")
    except Exception as exc:  # noqa: BLE001
        logger.warning("resume download failed job=%s: %s", jid, exc, exc_info=True)
        # 保持 running，下次启动再试；不标 failed
    finally:
        runtime.clear(live.job_id)


async def _resume_kling_poll(container, row: dict) -> None:
    """崩溃后续查可灵任务；成功则下载落盘。"""
    import httpx
    from infrastructure.video_gen.cloud_adapter import (
        _extract_video, _task_status, is_legacy_kling_model,
    )
    from infrastructure.video_gen.kling_auth import kling_auth_header

    store = get_store()
    jid = row["id"]
    session_id = row.get("session_id") or ""
    task_id = (row.get("remote_id") or "").strip()
    base_url = (row.get("base_url") or "").rstrip("/")
    snap = None
    try:
        snap = container.providers.snapshot_for("video_gen")
    except Exception:  # noqa: BLE001
        snap = None
    if not snap or not task_id or not base_url:
        logger.warning("resume kling skip job=%s missing snap/task", jid)
        return
    api_key = getattr(snap, "api_key", "") or ""
    model_id = snap.model_id or ""
    legacy = is_legacy_kling_model(model_id)
    headers = {"Content-Type": "application/json"}
    headers.update(kling_auth_header(api_key, model_id=model_id))
    live = runtime.register(
        kind="kling_video", backend="kling",
        session_id=session_id, base_url=base_url,
        remote_id=task_id, job_id=jid)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=15.0)) as client:
            while True:
                runtime.raise_if_cancelled(job_id=jid, session_id=session_id)
                if legacy:
                    info = await http_get_json_resilient(
                        client,
                        f"{base_url}/v1/videos/text2video/{task_id}",
                        headers=headers, job_id=jid, session_id=session_id)
                else:
                    info = await http_get_json_resilient(
                        client, f"{base_url}/tasks",
                        headers=headers, params={"task_ids": task_id},
                        job_id=jid, session_id=session_id)
                if not isinstance(info, dict):
                    info = {}
                status = _task_status(info)
                if status in ("succeed", "succeeded", "success", "completed"):
                    video_url, _dur = _extract_video(info)
                    if not video_url:
                        raise RuntimeError("云端完成但无视频地址")
                    if store:
                        store.merge_meta(jid, {
                            "phase": "download", "result_url": video_url})
                    data = await fetch_url_bytes(
                        video_url, job_id=jid, session_id=session_id)
                    out = Path(container.data_dir) / "chat_videos"
                    out.mkdir(parents=True, exist_ok=True)
                    fname = f"genv_{uuid.uuid4().hex[:12]}.mp4"
                    (out / fname).write_bytes(data)
                    if store:
                        store.settle(jid, status="succeeded", result_ref=fname)
                    await _maybe_finish_workshop(
                        container, row, fname, "kling_video")
                    return
                if status in ("failed", "fail", "error"):
                    if store:
                        store.settle(
                            jid, status="failed",
                            error_message="云端任务失败")
                    return
                if store:
                    store.touch(jid)
                from infrastructure.remote_jobs.fetch import sleep_or_cancel
                await sleep_or_cancel(2.0, job_id=jid, session_id=session_id)
    except JobCancelled:
        if store:
            store.settle(jid, status="cancelled", error_message="已停止生成")
    except Exception as exc:  # noqa: BLE001
        logger.warning("resume kling failed job=%s: %s", jid, exc, exc_info=True)
    finally:
        runtime.clear(live.job_id)


async def _resume_dashscope_poll(container, row: dict) -> None:
    """崩溃后续查百炼任务；成功则下载落盘。"""
    import httpx
    from infrastructure.video_gen.dashscope_adapter import (
        dashscope_error_text, dashscope_headers, dashscope_status, dashscope_video_url,
    )
    from infrastructure.video_gen.vendor import dashscope_api_root

    store = get_store()
    jid = row["id"]
    session_id = row.get("session_id") or ""
    task_id = (row.get("remote_id") or "").strip()
    base_url = (row.get("base_url") or "").rstrip("/")
    snap = None
    try:
        snap = container.providers.snapshot_for("video_gen")
    except Exception:  # noqa: BLE001
        snap = None
    if not snap or not task_id or not base_url:
        logger.warning("resume dashscope skip job=%s missing snap/task", jid)
        return
    headers = dashscope_headers(getattr(snap, "api_key", "") or "")
    api_root = dashscope_api_root(base_url)
    live = runtime.register(
        kind="dashscope_video", backend="dashscope",
        session_id=session_id, base_url=base_url,
        remote_id=task_id, job_id=jid)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=15.0)) as client:
            while True:
                runtime.raise_if_cancelled(job_id=jid, session_id=session_id)
                info = await http_get_json_resilient(
                    client, f"{api_root}/tasks/{task_id}",
                    headers=headers, job_id=jid, session_id=session_id)
                if not isinstance(info, dict):
                    info = {}
                status = dashscope_status(info)
                if status == "SUCCEEDED":
                    video_url, _dur = dashscope_video_url(info)
                    if not video_url:
                        raise RuntimeError("百炼完成但无视频地址")
                    if store:
                        store.merge_meta(jid, {
                            "phase": "download", "result_url": video_url})
                    data = await fetch_url_bytes(
                        video_url, job_id=jid, session_id=session_id)
                    out = Path(container.data_dir) / "chat_videos"
                    out.mkdir(parents=True, exist_ok=True)
                    fname = f"genv_{uuid.uuid4().hex[:12]}.mp4"
                    (out / fname).write_bytes(data)
                    if store:
                        store.settle(jid, status="succeeded", result_ref=fname)
                    await _maybe_finish_workshop(
                        container, row, fname, "dashscope_video")
                    return
                if status in ("FAILED", "CANCELED", "UNKNOWN"):
                    if store:
                        store.settle(
                            jid, status="failed",
                            error_message=dashscope_error_text(info, "百炼任务失败"))
                    return
                if store:
                    store.touch(jid)
                from infrastructure.remote_jobs.fetch import sleep_or_cancel
                await sleep_or_cancel(2.0, job_id=jid, session_id=session_id)
    except JobCancelled:
        if store:
            store.settle(jid, status="cancelled", error_message="已停止生成")
    except Exception as exc:  # noqa: BLE001
        logger.warning("resume dashscope failed job=%s: %s", jid, exc, exc_info=True)
    finally:
        runtime.clear(live.job_id)


async def _resume_comfy(container, row: dict, meta: dict) -> None:
    import httpx
    from infrastructure.remote_jobs.fetch import http_get_bytes_resilient, sleep_or_cancel

    store = get_store()
    jid = row["id"]
    session_id = row.get("session_id") or ""
    prompt_id = (row.get("remote_id") or "").strip()
    base_url = (row.get("base_url") or "").rstrip("/")
    if not prompt_id or not base_url:
        return
    live = runtime.register(
        kind=row.get("kind") or "comfy_video",
        backend="comfyui",
        session_id=session_id, base_url=base_url,
        remote_id=prompt_id, job_id=jid)
    kind = row.get("kind") or "comfy_video"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=15.0)) as client:
            view = meta.get("view") if isinstance(meta.get("view"), dict) else None
            if not view:
                while True:
                    runtime.raise_if_cancelled(job_id=jid, session_id=session_id)
                    try:
                        hist = await client.get(f"{base_url}/history/{prompt_id}")
                    except (httpx.TimeoutException, httpx.TransportError):
                        await sleep_or_cancel(2.0, job_id=jid, session_id=session_id)
                        continue
                    if hist.status_code == 200:
                        data = hist.json() or {}
                        entry = data.get(prompt_id) or {}
                        if entry.get("outputs"):
                            outputs = entry["outputs"]
                            break
                    await sleep_or_cancel(1.2, job_id=jid, session_id=session_id)
                # 粗提 meta
                view = None
                for _nid, node_out in (outputs or {}).items():
                    if not isinstance(node_out, dict):
                        continue
                    for key in ("videos", "gifs", "images"):
                        items = node_out.get(key)
                        if items and isinstance(items[0], dict) and items[0].get("filename"):
                            view = items[0]
                            break
                    if view:
                        break
            if not view or not view.get("filename"):
                raise RuntimeError("ComfyUI 续跟未找到输出")
            raw = await http_get_bytes_resilient(
                client, f"{base_url}/view",
                params={
                    "filename": view["filename"],
                    "subfolder": view.get("subfolder") or "",
                    "type": view.get("type") or "output",
                },
                job_id=jid, session_id=session_id,
                label="媒体",
            )
            data_dir = Path(container.data_dir)
            if "image" in kind:
                out = data_dir / "chat_images"
                fname = f"gen_{uuid.uuid4().hex[:12]}.png"
            else:
                out = data_dir / "chat_videos"
                fname = f"genv_{uuid.uuid4().hex[:12]}.mp4"
            out.mkdir(parents=True, exist_ok=True)
            (out / fname).write_bytes(raw)
            if store:
                store.settle(jid, status="succeeded", result_ref=fname)
            await _maybe_finish_workshop(container, row, fname, kind)
    except JobCancelled:
        if store:
            store.settle(jid, status="cancelled", error_message="已停止生成")
    except Exception as exc:  # noqa: BLE001
        logger.warning("resume comfy failed job=%s: %s", jid, exc, exc_info=True)
    finally:
        runtime.clear(live.job_id)


async def _maybe_finish_workshop(container, row: dict, fname: str, kind: str) -> None:
    """若 owner 是工坊项目且仍在 doing，写成片。"""
    workshop = getattr(container, "workshop", None)
    if workshop is None:
        return
    owner_type = row.get("owner_type") or ""
    owner_ref = row.get("owner_ref") or ""
    session_id = row.get("session_id") or ""
    project_id = owner_ref if owner_type == "workshop" else ""
    if not project_id and session_id.startswith("vp_"):
        project_id = session_id
    if not project_id:
        # 尝试用 session 反查
        try:
            rows = container.db.query_all(
                "SELECT id FROM video_projects WHERE status=? AND "
                "(session_id=? OR id=?)",
                ("doing", session_id, session_id))
            if rows:
                project_id = rows[0]["id"]
        except Exception:  # noqa: BLE001
            return
    if not project_id or "image" in (kind or ""):
        return
    try:
        url = f"/chat-videos/{fname}"
        workshop.update(
            project_id,
            status="done", progress=100, error_message=None,
            filename=fname, public_url=url)
        logger.info("workshop project resumed done id=%s file=%s",
                    project_id, fname)
    except Exception:  # noqa: BLE001
        logger.debug("workshop resume update skip", exc_info=True)
