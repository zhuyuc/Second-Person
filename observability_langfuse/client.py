"""
Langfuse Ingestion 客户端：把追踪事件批量异步上报到
`POST {host}/api/public/ingestion`（Basic Auth: public_key:secret_key）。

事件格式（官方批量协议）：
    {"batch": [{"id": <event_uuid>, "type": <type>, "timestamp": <iso>, "body": {...}}, ...]}
支持的 type：trace-create / span-create / span-update / generation-create / generation-update。

特性：
- 事件先入内存队列，后台协程按 flush_interval 或批量阈值批量上报
- 网络/服务端异常只记 WARNING，绝不影响对话主链路
- 失败重试：指数退避（1s→2s→4s），最多 3 次
- 熔断保护：连续 3 次 flush 失败进入冷却期（30s），期间跳过上报
- 事件保护：上报失败时事件重新入队，不永久丢失
- start() 在应用事件循环启动后调用；stop() 优雅停机时最后 flush
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time

logger = logging.getLogger("second_person.langfuse")

_MAX_QUEUE = 2000
_MAX_PER_POST = 200
_RETRY_DELAYS = [1.0, 2.0, 4.0]    # 指数退避
_CIRCUIT_FAILURES = 3               # 连续失败阈值
_CIRCUIT_COOLDOWN = 30.0            # 熔断冷却期（秒）


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
        # 熔断器
        self._consecutive_failures = 0
        self._circuit_open_until: float = 0.0

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
        # 熔断冷却期：跳过本次 flush，事件保留在队列
        if self._circuit_open_until > time.monotonic():
            return
        batch = self._queue[:_MAX_PER_POST]
        self._queue = self._queue[len(batch):]
        # 带退避重试
        last_err = None
        for i, delay in enumerate([0.0] + _RETRY_DELAYS):
            if delay:
                await asyncio.sleep(delay)
            try:
                r = await self._client.post(
                    self.host + "/api/public/ingestion",
                    json={"batch": batch},
                    headers={"Authorization": self._auth,
                             "Content-Type": "application/json"})
                if r.status_code >= 400:
                    logger.warning("Langfuse 上报失败 %s：%s",
                                   r.status_code, r.text[:200])
                    # 4xx 为客户端错误（如认证失败），不重试
                    if 400 <= r.status_code < 500:
                        self._on_failure()
                        return  # 不重新入队（4xx 重试无意义）
                    last_err = Exception(f"HTTP {r.status_code}")
                    continue  # 5xx 可重试
                # 成功：重置熔断计数器
                self._on_success()
                return
            except Exception as e:
                last_err = e
                continue
        # 全部重试耗尽
        self._on_failure()
        logger.warning("Langfuse 上报重试耗尽：%s", last_err)
        # 事件保护：失败时重新放回队列头部（不超过队列上限）
        if len(self._queue) + len(batch) <= _MAX_QUEUE:
            self._queue = batch + self._queue
        else:
            # 队列满：只保留最多 100 条事件，优先保留新事件
            keep = min(len(batch), 100)
            logger.warning(
                "Langfuse 上报队列满，丢弃 %d 条旧事件", len(batch) - keep)
            if keep:
                self._queue = batch[:keep] + self._queue

    def _on_success(self) -> None:
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

    def _on_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= _CIRCUIT_FAILURES:
            self._circuit_open_until = time.monotonic() + _CIRCUIT_COOLDOWN
            logger.warning(
                "Langfuse 熔断：连续 %d 次失败，冷却 %.0fs",
                self._consecutive_failures, _CIRCUIT_COOLDOWN)

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
