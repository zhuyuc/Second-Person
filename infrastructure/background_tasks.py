"""后台任务统一管理 —— fire-and-forget 任务的注册、异常落日志、优雅停机。

为什么需要：
- 项目里多处用 `asyncio.create_task(...)` 启动后台任务（mood 判定、标题生成、
  handoff 摘要、watcher 触发的索引重建等）。原写法有两个问题：
  1. 任务引用未持有，GC 可能在任务完成前回收协程（CPython 实现细节上通常
     不会，但官方文档明确警告）。
  2. 任务异常被静默吞掉（除非显式 await / add_done_callback）。
  3. 进程关闭时这些任务没有统一取消/等待，可能中断 mid-flight 写入。
- 本模块提供模块级单例 `track_task(coro, name=...)`，统一注册 + done callback
  记日志；`shutdown()` 在 AppContainer 停机时 cancel + gather，避免泄漏。

使用约定：
- 对"失败不影响主链路"的后台任务，用 `track_task(...)` 替代 `asyncio.create_task`。
- 对必须等结果的任务，仍用普通 `await`，不要走这里。
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("second_person.background")

_tasks: set[asyncio.Task] = set()


def track_task(coro, *, name: str | None = None) -> asyncio.Task:
    """启动并注册一个后台任务。

    - 持有强引用，避免 GC 回收未完成的协程。
    - done callback 自动从集合中移除，并把非取消类异常记到日志（避免静默吞掉）。
    """
    task = asyncio.create_task(coro, name=name)
    _tasks.add(task)
    task.add_done_callback(_on_done)
    return task


def _on_done(task: asyncio.Task) -> None:
    _tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "[bg task %s] 未捕获异常: %r",
            task.get_name() or "<anonymous>", exc, exc_info=exc,
        )


def pending_count() -> int:
    """当前注册中（未完成）的后台任务数。仅供观测/测试。"""
    return len(_tasks)


async def shutdown(*, timeout: float = 5.0) -> None:
    """优雅停机：取消所有后台任务并等待它们落地。

    - 优先取消（让任务有机会进 finally 释放资源）。
    - gather 等待最多 timeout 秒；超时后不再阻塞停机流程。
    - 任何任务异常在此被 gather(return_exceptions=True) 吞掉，已由 done callback 记日志。
    """
    if not _tasks:
        return
    pending = list(_tasks)
    for t in pending:
        if not t.done():
            t.cancel()
    try:
        await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "后台任务停机等待超时（%ss），仍有 %d 个未落地",
            timeout, sum(1 for t in pending if not t.done()),
        )
    _tasks.clear()
