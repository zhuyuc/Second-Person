"""兼容层：会话级活跃媒体任务 → remote_jobs 运行时。

新代码请直接用 infrastructure.remote_jobs。
本模块保留 register_job / clear_job / interrupt_session 签名，避免旧调用点大面积改动。
"""
from __future__ import annotations

import logging
from typing import Any

from infrastructure.remote_jobs import (
    interrupt_session,
    runtime,
)

logger = logging.getLogger("second_person.image_gen.active")

# session_id -> 最近一次登记的 job_id（兼容单槽语义）
_SESSION_JOB: dict[str, str] = {}


def register_job(
    session_id: str,
    *,
    base_url: str,
    prompt_id: str,
    client_id: str = "",
    backend: str = "comfyui",
    kind: str = "",
    owner_type: str = "chat_tool",
    owner_ref: str = "",
    job_id: str | None = None,
) -> str:
    """登记活跃任务；返回 job_id。session 为空时仍登记（用匿名键）。"""
    inferred_kind = kind or (
        "kling_video" if backend == "kling"
        else "comfy_video" if backend == "comfyui" and "video" in (kind or "")
        else "comfy_image" if backend == "comfyui"
        else backend or "media"
    )
    live = runtime.register(
        kind=inferred_kind,
        backend=backend,
        session_id=session_id or "",
        owner_type=owner_type,
        owner_ref=owner_ref,
        base_url=base_url,
        remote_id=prompt_id or "",
        client_id=client_id,
        job_id=job_id,
    )
    if session_id:
        _SESSION_JOB[session_id] = live.job_id
    return live.job_id


def clear_job(session_id: str, job_id: str | None = None) -> None:
    jid = job_id
    if not jid and session_id:
        jid = _SESSION_JOB.pop(session_id, None)
    if jid:
        runtime.clear(jid)
        if session_id and _SESSION_JOB.get(session_id) == jid:
            _SESSION_JOB.pop(session_id, None)


def get_job(session_id: str) -> dict[str, Any] | None:
    jid = _SESSION_JOB.get(session_id or "")
    if not jid:
        return None
    live = runtime.get(jid)
    if live is None:
        return None
    return {
        "job_id": live.job_id,
        "base_url": live.base_url,
        "prompt_id": live.remote_id,
        "client_id": live.client_id,
        "backend": live.backend,
        "cancelled": live.cancelled,
    }


def bind_remote(session_id: str, remote_id: str, job_id: str | None = None) -> None:
    jid = job_id or _SESSION_JOB.get(session_id or "")
    if jid:
        runtime.bind_remote(jid, remote_id)


__all__ = [
    "bind_remote",
    "clear_job",
    "get_job",
    "interrupt_session",
    "register_job",
]
