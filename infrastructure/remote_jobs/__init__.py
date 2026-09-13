"""远端长任务：持久句柄 + 进程内取消扇出。

契约（与 pi / deepseek-harness 对齐）：
- 提交成功后立即登记 remote_id，禁止因本地等待超时撕票。
- 轮询跟到远端终态，或用户显式取消。
- 取消：标记 cancelled + 通知所有端口；ComfyUI 尽力 interrupt。
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any

import httpx

from infrastructure.timeutil import now_cst

logger = logging.getLogger("second_person.remote_jobs")

VALID_STATUS = ("running", "succeeded", "failed", "cancelled")
_STOP_MSG = "已停止生成"


def _new_id() -> str:
    return "rj_" + secrets.token_hex(6)


class JobCancelled(RuntimeError):
    """用户（或扇出取消）要求停止；不算远端失败。"""

    def __init__(self, message: str = _STOP_MSG):
        super().__init__(message or _STOP_MSG)


@dataclass
class LiveJob:
    job_id: str
    kind: str
    backend: str
    remote_id: str = ""
    session_id: str = ""
    owner_type: str = "chat_tool"
    owner_ref: str = ""
    base_url: str = ""
    client_id: str = ""
    # 仅进程内存：取消远端时需要的鉴权头（禁止落库）
    cancel_headers: dict[str, str] = field(default_factory=dict)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()


class RemoteJobRuntime:
    """进程内活跃任务表：按 session / job 取消，供适配器轮询检查。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._by_id: dict[str, LiveJob] = {}
        self._by_session: dict[str, set[str]] = {}

    def register(
        self,
        *,
        kind: str,
        backend: str,
        session_id: str = "",
        owner_type: str = "chat_tool",
        owner_ref: str = "",
        base_url: str = "",
        remote_id: str = "",
        client_id: str = "",
        job_id: str | None = None,
    ) -> LiveJob:
        jid = job_id or _new_id()
        live = LiveJob(
            job_id=jid,
            kind=kind,
            backend=backend,
            remote_id=remote_id or "",
            session_id=session_id or "",
            owner_type=owner_type or "chat_tool",
            owner_ref=owner_ref or "",
            base_url=(base_url or "").rstrip("/"),
            client_id=client_id or "",
        )
        with self._lock:
            self._by_id[jid] = live
            if live.session_id:
                self._by_session.setdefault(live.session_id, set()).add(jid)
        return live

    def bind_remote(self, job_id: str, remote_id: str) -> None:
        with self._lock:
            live = self._by_id.get(job_id)
            if live is not None:
                live.remote_id = remote_id or live.remote_id

    def set_cancel_headers(self, job_id: str, headers: dict[str, str] | None) -> None:
        """登记取消远端所需鉴权头（仅内存）。"""
        if not job_id or not headers:
            return
        cleaned = {
            str(k): str(v) for k, v in headers.items()
            if k and v is not None
        }
        with self._lock:
            live = self._by_id.get(job_id)
            if live is not None:
                live.cancel_headers = cleaned

    def get(self, job_id: str) -> LiveJob | None:
        with self._lock:
            return self._by_id.get(job_id)

    def is_cancelled(self, job_id: str = "", session_id: str = "") -> bool:
        with self._lock:
            if job_id:
                live = self._by_id.get(job_id)
                return bool(live and live.cancelled)
            if session_id:
                for jid in self._by_session.get(session_id, ()):
                    live = self._by_id.get(jid)
                    if live and live.cancelled:
                        return True
        return False

    def raise_if_cancelled(self, job_id: str = "", session_id: str = "") -> None:
        if self.is_cancelled(job_id=job_id, session_id=session_id):
            raise JobCancelled()

    def clear(self, job_id: str) -> None:
        with self._lock:
            live = self._by_id.pop(job_id, None)
            if live and live.session_id:
                bucket = self._by_session.get(live.session_id)
                if bucket is not None:
                    bucket.discard(job_id)
                    if not bucket:
                        self._by_session.pop(live.session_id, None)

    def request_cancel_session(self, session_id: str) -> list[LiveJob]:
        """标记该会话全部活跃任务取消，返回快照列表供 interrupt。"""
        if not session_id:
            return []
        out: list[LiveJob] = []
        with self._lock:
            for jid in list(self._by_session.get(session_id, ())):
                live = self._by_id.get(jid)
                if live is None:
                    continue
                live.cancel_event.set()
                out.append(live)
        return out

    def request_cancel_job(self, job_id: str) -> LiveJob | None:
        with self._lock:
            live = self._by_id.get(job_id)
            if live is None:
                return None
            live.cancel_event.set()
            return live

    def request_cancel_owner(self, owner_type: str, owner_ref: str) -> list[LiveJob]:
        if not owner_type or not owner_ref:
            return []
        out: list[LiveJob] = []
        with self._lock:
            for live in list(self._by_id.values()):
                if live.owner_type == owner_type and live.owner_ref == owner_ref:
                    live.cancel_event.set()
                    out.append(live)
        return out


# 进程级单例（适配器 / 路由共享）
runtime = RemoteJobRuntime()
_store: RemoteJobStore | None = None


def set_store(store: RemoteJobStore | None) -> None:
    global _store
    _store = store


def get_store() -> RemoteJobStore | None:
    return _store


class RemoteJobStore:
    """SQLite 持久化：崩溃后可按 remote_id 续跟。"""

    def __init__(self, db):
        self.db = db

    def create(
        self,
        *,
        kind: str,
        backend: str,
        remote_id: str = "",
        session_id: str = "",
        owner_type: str = "chat_tool",
        owner_ref: str = "",
        base_url: str = "",
        meta: dict | None = None,
        job_id: str | None = None,
    ) -> str:
        jid = job_id or _new_id()
        now = now_cst()
        self.db.execute(
            "INSERT INTO remote_jobs("
            "id,kind,backend,remote_id,status,session_id,owner_type,owner_ref,"
            "base_url,meta_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                jid, kind, backend, remote_id or "", "running",
                session_id or None, owner_type, owner_ref or None,
                (base_url or "").rstrip("/") or None,
                json.dumps(meta or {}, ensure_ascii=False),
                now, now,
            ),
        )
        return jid

    def set_remote_id(self, job_id: str, remote_id: str) -> None:
        self.db.execute(
            "UPDATE remote_jobs SET remote_id=?, updated_at=? WHERE id=?",
            (remote_id, now_cst(), job_id),
        )

    def merge_meta(self, job_id: str, patch: dict[str, Any]) -> None:
        row = self.get(job_id)
        if not row:
            return
        raw = row.get("meta_json") or "{}"
        try:
            meta = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        except Exception:  # noqa: BLE001
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        meta.update(patch or {})
        self.db.execute(
            "UPDATE remote_jobs SET meta_json=?, updated_at=? WHERE id=?",
            (json.dumps(meta, ensure_ascii=False), now_cst(), job_id),
        )

    def touch(self, job_id: str) -> None:
        self.db.execute(
            "UPDATE remote_jobs SET updated_at=? WHERE id=? AND status=?",
            (now_cst(), job_id, "running"),
        )

    def find_running_by_session(self, session_id: str) -> list[dict[str, Any]]:
        if not session_id:
            return []
        rows = self.db.query_all(
            "SELECT * FROM remote_jobs WHERE status=? AND session_id=?",
            ("running", session_id),
        )
        return [dict(r) for r in rows]

    def settle(
        self,
        job_id: str,
        *,
        status: str,
        result_ref: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if status not in VALID_STATUS or status == "running":
            raise ValueError(f"invalid settle status: {status}")
        now = now_cst()
        self.db.execute(
            "UPDATE remote_jobs SET status=?, result_ref=?, error_message=?, "
            "updated_at=?, settled_at=? WHERE id=? AND status=?",
            (
                status, result_ref, error_message, now, now, job_id, "running",
            ),
        )

    def mark_cancelled(self, job_id: str, message: str = _STOP_MSG) -> None:
        self.settle(job_id, status="cancelled", error_message=message)

    def mark_cancelled_session(self, session_id: str, message: str = _STOP_MSG) -> int:
        if not session_id:
            return 0
        now = now_cst()
        result = self.db.execute(
            "UPDATE remote_jobs SET status=?, error_message=?, updated_at=?, "
            "settled_at=? WHERE session_id=? AND status=?",
            ("cancelled", message, now, now, session_id, "running"),
        )
        return int(result.rowcount or 0)

    def mark_cancelled_owner(
        self, owner_type: str, owner_ref: str, message: str = _STOP_MSG,
    ) -> int:
        if not owner_type or not owner_ref:
            return 0
        now = now_cst()
        result = self.db.execute(
            "UPDATE remote_jobs SET status=?, error_message=?, updated_at=?, "
            "settled_at=? WHERE owner_type=? AND owner_ref=? AND status=?",
            ("cancelled", message, now, now, owner_type, owner_ref, "running"),
        )
        return int(result.rowcount or 0)

    def list_running(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.query_all(
            "SELECT * FROM remote_jobs WHERE status=? "
            "ORDER BY updated_at ASC LIMIT ?",
            ("running", max(1, min(int(limit or 100), 500))),
        )
        return [dict(r) for r in rows]

    def get(self, job_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM remote_jobs WHERE id=?", (job_id,))
        return dict(row) if row else None


async def _try_cancel_kling(live: LiveJob) -> bool:
    """尽力取消可灵远端任务。官方文档未必有统一 cancel，多路径试探，失败只记日志。"""
    tid = (live.remote_id or "").strip()
    base = (live.base_url or "").rstrip("/")
    if not tid or not base:
        return False
    headers = dict(live.cancel_headers or {})
    headers.setdefault("Content-Type", "application/json")
    candidates: list[tuple[str, str, dict | None]] = [
        ("POST", f"{base}/v1/videos/text2video/{tid}/cancel", None),
        ("DELETE", f"{base}/v1/videos/text2video/{tid}", None),
        ("POST", f"{base}/tasks/{tid}/cancel", None),
        ("DELETE", f"{base}/tasks/{tid}", None),
        ("POST", f"{base}/v1/videos/text2video/cancel", {"task_id": tid}),
    ]
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            for method, url, body in candidates:
                try:
                    if method == "DELETE":
                        resp = await client.delete(url, headers=headers)
                    elif body is not None:
                        resp = await client.post(url, headers=headers, json=body)
                    else:
                        resp = await client.post(url, headers=headers)
                    if resp.status_code < 400:
                        logger.info(
                            "kling cancel ok job=%s remote_id=%s via %s %s",
                            live.job_id, tid, method, url,
                        )
                        return True
                except Exception:  # noqa: BLE001
                    continue
    except Exception as exc:  # noqa: BLE001
        logger.warning("kling cancel attempt failed job=%s: %s", live.job_id, exc)
    logger.info(
        "kling cancel 无可用端点或已不可取消 job=%s remote_id=%s（本地已停跟）",
        live.job_id, tid,
    )
    return False


async def interrupt_backends(lives: list[LiveJob]) -> int:
    """对已标记取消的任务尽力打断后端（ComfyUI /interrupt、可灵 cancel）。"""
    n = 0
    seen_comfy: set[str] = set()
    for live in lives:
        backend = (live.backend or "").strip()
        if backend == "comfyui" and live.base_url:
            url = f"{live.base_url.rstrip('/')}/interrupt"
            if url in seen_comfy:
                continue
            seen_comfy.add(url)
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    await client.post(url)
                n += 1
                logger.info(
                    "已请求 ComfyUI interrupt job=%s remote_id=%s",
                    live.job_id, live.remote_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("ComfyUI interrupt 失败：%s", exc)
        elif backend == "kling":
            if await _try_cancel_kling(live):
                n += 1
    return n


async def cancel_session(
    session_id: str,
    *,
    store: RemoteJobStore | None = None,
    message: str = _STOP_MSG,
) -> dict[str, Any]:
    """取消某会话全部活跃远端任务，并扇出后端 interrupt。"""
    lives = runtime.request_cancel_session(session_id)
    db_n = 0
    if store is not None:
        try:
            db_n = store.mark_cancelled_session(session_id, message)
        except Exception:  # noqa: BLE001
            logger.warning("remote_jobs DB cancel session failed", exc_info=True)
    interrupted = await interrupt_backends(lives)
    return {
        "live": len(lives),
        "db": db_n,
        "interrupted": interrupted,
        "message": message,
    }


async def cancel_owner(
    owner_type: str,
    owner_ref: str,
    *,
    store: RemoteJobStore | None = None,
    message: str = _STOP_MSG,
) -> dict[str, Any]:
    lives = runtime.request_cancel_owner(owner_type, owner_ref)
    for live in lives:
        # 同时按 session 标记，适配器若只查 session 也能停
        if live.session_id:
            runtime.request_cancel_session(live.session_id)
    db_n = 0
    if store is not None:
        try:
            db_n = store.mark_cancelled_owner(owner_type, owner_ref, message)
        except Exception:  # noqa: BLE001
            logger.warning("remote_jobs DB cancel owner failed", exc_info=True)
    interrupted = await interrupt_backends(lives)
    return {
        "live": len(lives),
        "db": db_n,
        "interrupted": interrupted,
        "message": message,
    }


# 兼容旧 import：infrastructure.image_gen.interrupt_session
async def interrupt_session(session_id: str) -> bool:
    """取消会话活跃媒体任务；返回是否至少标记/打断了一个。"""
    result = await cancel_session(session_id)
    return bool(result["live"] or result["interrupted"] or result["db"])
