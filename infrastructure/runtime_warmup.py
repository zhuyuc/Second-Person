"""启动/会话预热：消掉进程或会话首次调用的额外 RTT，不改变检索决策。

预热内容：
1. Embedding 槽位一次短文本向量化
2. 精筛槽位一次最小 payload（走与线上相同的 llm_refine_fn）
3. 主对话槽位 probe（建连 / 可选前缀侧热身，不改正式 prompt）

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
    """Coalesced background warmer for embed / refine / chat providers."""

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
        results = {
            "reason": reason,
            "embed": await self._warm_embed(),
            "refine": await self._warm_refine(),
            "chat": await self._warm_chat(),
        }
        results["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
        logger.info(
            "runtime warmup done reason=%s embed=%s refine=%s chat=%s elapsed_ms=%s",
            reason,
            results["embed"].get("ok"),
            results["refine"].get("ok"),
            results["chat"].get("ok"),
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
