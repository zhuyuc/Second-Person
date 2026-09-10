"""当前会话活跃的文生图任务（供取消 turn 时 interrupt）。"""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("second_person.image_gen.active")

# session_id -> {base_url, prompt_id, client_id, backend}
_ACTIVE: dict[str, dict[str, Any]] = {}


def register_job(session_id: str, *, base_url: str, prompt_id: str,
                 client_id: str = "", backend: str = "comfyui") -> None:
    if not session_id:
        return
    _ACTIVE[session_id] = {
        "base_url": base_url.rstrip("/"),
        "prompt_id": prompt_id,
        "client_id": client_id,
        "backend": backend,
    }


def clear_job(session_id: str) -> None:
    if session_id:
        _ACTIVE.pop(session_id, None)


def get_job(session_id: str) -> dict[str, Any] | None:
    return _ACTIVE.get(session_id)


async def interrupt_session(session_id: str) -> bool:
    """尽力取消该会话正在进行的本地生图。返回是否发出了中断请求。"""
    job = _ACTIVE.pop(session_id, None) if session_id else None
    if not job:
        return False
    if job.get("backend") != "comfyui":
        return False
    url = f"{job['base_url']}/interrupt"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url)
        logger.info("已请求 ComfyUI interrupt session=%s prompt_id=%s",
                    session_id, job.get("prompt_id"))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("ComfyUI interrupt 失败：%s", exc)
        return False
