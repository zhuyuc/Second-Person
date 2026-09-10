"""启动/会话预热：消掉进程或会话首次调用的额外 RTT，不改变检索决策。

预热内容：
1. Embedding 槽位一次短文本向量化
2. 精筛槽位一次最小 payload（走与线上相同的 llm_refine_fn）
3. 主对话槽位 probe（建连 / 可选前缀侧热身，不改正式 prompt）
4. 本地 ComfyUI（image_gen / video_gen）连通性探测

失败一律吞掉并打 debug/warning，不阻塞启动或建会话。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger("second_person.warmup")

# 同一进程内短时间多次建会话不重复打满三路，避免无意义费用
_DEFAULT_COALESCE_SECONDS = 45.0

_WARMUP_REFINE_CANDIDATES = [
    {
        "id": "warm_0",
        "title": "warmup",
        "summary": "connectivity probe only",
        "source_type": "memory",
        "confidence": "low",
        "verification_state": "direct",
        "freshness_state": "current",
        "relation": "primary",
        "from_seed": None,
    }
]


class RuntimeWarmer:
    """Coalesced background warmer for embed / refine / chat / local ComfyUI."""

    def __init__(self, container: Any,
                 coalesce_seconds: float = _DEFAULT_COALESCE_SECONDS) -> None:
        self._container = container
        self._coalesce_seconds = max(0.0, float(coalesce_seconds))
        self._lock = asyncio.Lock()
        self._last_started_at = 0.0
        self._in_flight: asyncio.Task | None = None

    def schedule(self, reason: str = "manual") -> None:
        """Fire-and-forget；与已有在途任务合并。无运行中事件循环时直接跳过。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("runtime warmup skipped (no running loop) reason=%s", reason)
            return

        from infrastructure.background_tasks import track_task

        async def _runner() -> None:
            await self.warm(reason=reason)

        track_task(_runner(), name=f"runtime_warmup:{reason}")

    async def warm(self, reason: str = "manual") -> dict[str, Any]:
        """执行预热；返回各路结果摘要（供测试/日志）。"""
        async with self._lock:
            now = time.monotonic()
            if (self._in_flight is not None and not self._in_flight.done()):
                return {"skipped": "in_flight", "reason": reason}
            if (self._last_started_at
                    and now - self._last_started_at < self._coalesce_seconds):
                return {
                    "skipped": "coalesced",
                    "reason": reason,
                    "age_s": round(now - self._last_started_at, 2),
                }
            self._last_started_at = now
            self._in_flight = asyncio.create_task(
                self._warm_all(reason), name=f"warmup-body:{reason}")
            task = self._in_flight
        try:
            return await task
        finally:
            async with self._lock:
                if self._in_flight is task:
                    self._in_flight = None

    async def _warm_all(self, reason: str) -> dict[str, Any]:
        started = time.perf_counter()
        comfy = await self._warm_comfyui()
        results = {
            "reason": reason,
            "embed": await self._warm_embed(),
            "refine": await self._warm_refine(),
            "chat": await self._warm_chat(),
            "image_gen": comfy.get("image_gen") or {"ok": False, "error": "missing"},
            "video_gen": comfy.get("video_gen") or {"ok": False, "error": "missing"},
        }
        results["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
        logger.info(
            "runtime warmup done reason=%s embed=%s refine=%s chat=%s "
            "image_gen=%s video_gen=%s elapsed_ms=%s",
            reason,
            results["embed"].get("ok"),
            results["refine"].get("ok"),
            results["chat"].get("ok"),
            results["image_gen"].get("ok"),
            results["video_gen"].get("ok"),
            results["elapsed_ms"],
        )
        return results

    async def _warm_embed(self) -> dict[str, Any]:
        embed_fn = getattr(self._container, "embed_fn", None)
        if embed_fn is None:
            return {"ok": False, "error": "embed_fn_missing"}
        try:
            vectors = await embed_fn(["runtime-warmup"])
            dim = len(vectors[0]) if vectors and vectors[0] is not None else 0
            return {"ok": True, "dim": dim}
        except Exception as exc:  # noqa: BLE001
            logger.debug("embed warmup failed: %s", exc, exc_info=True)
            return {"ok": False, "error": str(exc)[:200]}

    async def _warm_refine(self) -> dict[str, Any]:
        retriever = getattr(self._container, "retriever", None)
        refine_fn = getattr(retriever, "llm_refine_fn", None) if retriever else None
        if refine_fn is None:
            return {"ok": False, "error": "refine_fn_missing"}
        try:
            ids = await refine_fn(
                "warmup",
                list(_WARMUP_REFINE_CANDIDATES),
                session_id=None,
                context_text=None,
            )
            return {"ok": True, "ids": list(ids or [])[:5]}
        except Exception as exc:  # noqa: BLE001
            logger.debug("refine warmup failed: %s", exc, exc_info=True)
            return {"ok": False, "error": str(exc)[:200]}

    async def _warm_chat(self) -> dict[str, Any]:
        providers = getattr(self._container, "providers", None)
        llm = getattr(self._container, "llm", None)
        if providers is None or llm is None:
            return {"ok": False, "error": "llm_missing"}
        snap = providers.snapshot_for("chat") or providers.snapshot_for("agent")
        if snap is None:
            return {"ok": False, "error": "chat_snap_missing"}
        try:
            # probe 走连通性探测路径，不计入正式熔断/用量统计口径
            result = await llm.probe(snap)
            return {"ok": True, "protocol": result.get("protocol")}
        except Exception as exc:  # noqa: BLE001
            logger.debug("chat warmup failed: %s", exc, exc_info=True)
            return {"ok": False, "error": str(exc)[:200]}

    async def _warm_comfyui(self) -> dict[str, dict[str, Any]]:
        """探测 image_gen / video_gen 绑定的本地 ComfyUI（同 URL 只探一次）。"""
        providers = getattr(self._container, "providers", None)
        out: dict[str, dict[str, Any]] = {
            "image_gen": {"ok": False, "error": "providers_missing"},
            "video_gen": {"ok": False, "error": "providers_missing"},
        }
        if providers is None:
            return out

        try:
            from infrastructure.image_gen import probe_comfyui
        except Exception as exc:  # noqa: BLE001
            err = {"ok": False, "error": f"probe_import:{exc}"[:200]}
            return {"image_gen": dict(err), "video_gen": dict(err)}

        url_cache: dict[str, dict[str, Any]] = {}
        for slot in ("image_gen", "video_gen"):
            snap = providers.snapshot_for(slot)
            if snap is None:
                out[slot] = {"ok": False, "error": "unconfigured"}
                continue
            ptype = (getattr(snap, "provider_type", "") or "").strip().lower()
            if ptype != "comfyui":
                out[slot] = {"ok": False, "error": f"unsupported_type:{ptype}"}
                continue
            base = (getattr(snap, "base_url", "") or "").rstrip("/")
            if not base:
                out[slot] = {"ok": False, "error": "empty_base_url"}
                continue
            if base not in url_cache:
                try:
                    url_cache[base] = await probe_comfyui(base)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("comfyui warmup failed slot=%s: %s",
                                 slot, exc, exc_info=True)
                    url_cache[base] = {"ok": False, "error": str(exc)[:200]}
            probe = dict(url_cache[base])
            probe["base_url"] = base
            probe["model_id"] = getattr(snap, "model_id", "") or ""
            out[slot] = probe
        return out
