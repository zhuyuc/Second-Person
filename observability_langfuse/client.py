"""
Langfuse Ingestion 客户端：把追踪事件批量异步上报到
`POST {host}/api/public/ingestion`（Basic Auth: public_key:secret_key）。

事件格式（官方批量协议）：
    {"batch": [{"id": <event_uuid>, "type": <type>, "timestamp": <iso>, "body": {...}}, ...]}
支持的 type：trace-create / span-create / span-update / generation-create / generation-update。

特性：
- 事件先入内存队列，后台协程按 flush_interval 或批量阈值批量上报，避免阻塞主流程
- 网络/服务端异常只记 WARNING，绝不影响对话主链路
- start() 在应用事件循环启动后调用；stop() 优雅停机时最后 flush
"""
from __future__ import annotations

import asyncio
import base64
import logging

logger = logging.getLogger("second_person.langfuse")

_MAX_QUEUE = 2000
_MAX_PER_POST = 200


class IngestionClient:
    def __init__(self, host: str, public_key: str, secret_key: str, *,
                 flush_interval: float = 3.0, batch_size: int = 20) -> None:
        self.host = host.rstrip("/")
        self._auth = "Basic " + base64.b64encode(
            f"{public_key}:{secret_key}".encode()).decode()
        self.flush_interval = max(0.5, flush_interval)
        self.batch_size = max(1, batch_size)
        self._queue: list[dict] = []
        self._client = None
        self._task: asyncio.Task | None = None
        self._stopped = True

    def enqueue(self, event: dict) -> None:
        if self._stopped:
            return
        self._queue.append(event)
        if len(self._queue) > _MAX_QUEUE:
            dropped = len(self._queue) - _MAX_QUEUE
            self._queue = self._queue[-_MAX_QUEUE:]
            logger.warning(
                "Langfuse ingestion queue overflow: %d events dropped", dropped)

    async def start(self) -> None:
        try:
            import httpx
        except ImportError:  # pragma: no cover
            logger.warning("未安装 httpx，Langfuse 上报不可用")
            return
        self._client = httpx.AsyncClient(timeout=10)
        self._stopped = False
        self._task = asyncio.create_task(self._loop())
        logger.info("Langfuse 上报客户端已启动 → %s", self.host)

    async def _loop(self) -> None:
        try:
            while not self._stopped:
                await asyncio.sleep(self.flush_interval)
                await self.flush()
        except asyncio.CancelledError:
            pass

    async def flush(self) -> None:
        if not self._client or not self._queue:
            return
        batch = self._queue[:_MAX_PER_POST]
        self._queue = self._queue[len(batch):]
        try:
            r = await self._client.post(
                self.host + "/api/public/ingestion",
                json={"batch": batch},
                headers={"Authorization": self._auth,
                         "Content-Type": "application/json"})
            if r.status_code >= 400:
                logger.warning("Langfuse 上报失败 %s：%s",
                               r.status_code, r.text[:200])
        except Exception as e:  # noqa: BLE001 - 上报失败不影响主流程
            logger.error(
                "Langfuse flush failed: %d events lost, error: %s", len(batch), e)

    async def stop(self) -> None:
        self._stopped = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self.flush()
        if self._client:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
