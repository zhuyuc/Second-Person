"""
EventBus —— 模块间零直接调用，通过事件通信解耦。

设计要点（产品文档 §事件总线 EventBus / 开发文档 §6.12）：
- 模块只写订阅者：新功能通过 subscribe() 挂接，不改发布方
- 同步与异步订阅者都支持；异步订阅者在事件循环中调度
- 订阅者异常被捕获并记日志，不影响其他订阅者与发布方
核心事件（11 个）：
  memory.created / memory.updated / turn.completed / lint.completed /
  review.completed / soul_style.updated / output_style.updated /
  profile.rebuilt / task.progress / embedding.migration.completed / mood.updated
订阅方：container 启动时为全部预置事件挂接审计日志订阅者；
插件通过 on_load(event_bus=...) 按需追加订阅。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from collections import defaultdict
from typing import Any, Callable

logger = logging.getLogger("second_person.eventbus")

# 已知事件名常量（避免拼写漂移）
EVT_MEMORY_CREATED = "memory.created"
EVT_MEMORY_UPDATED = "memory.updated"
EVT_TURN_COMPLETED = "turn.completed"
EVT_LINT_COMPLETED = "lint.completed"
EVT_REVIEW_COMPLETED = "review.completed"
EVT_SOUL_STYLE_UPDATED = "soul_style.updated"
EVT_OUTPUT_STYLE_UPDATED = "output_style.updated"
EVT_PROFILE_REBUILT = "profile.rebuilt"
EVT_TASK_PROGRESS = "task.progress"
EVT_EMBEDDING_MIGRATION_COMPLETED = "embedding.migration.completed"
EVT_MOOD_UPDATED = "mood.updated"
# 响应策略引擎（意图理解与响应质量优化方案 v3 §事件总线）
EVT_STRATEGY_DECIDED = "strategy.decided"
EVT_SKELETON_EXTRACTED = "skeleton.extracted"
EVT_STRATEGY_EXECUTED = "strategy.executed"
EVT_STRATEGY_REFLECTED = "strategy.reflected"

KNOWN_EVENTS = {
    EVT_MEMORY_CREATED, EVT_MEMORY_UPDATED, EVT_TURN_COMPLETED, EVT_LINT_COMPLETED,
    EVT_REVIEW_COMPLETED, EVT_SOUL_STYLE_UPDATED,
    EVT_OUTPUT_STYLE_UPDATED, EVT_PROFILE_REBUILT, EVT_TASK_PROGRESS,
    EVT_EMBEDDING_MIGRATION_COMPLETED, EVT_MOOD_UPDATED,
    EVT_STRATEGY_DECIDED, EVT_SKELETON_EXTRACTED,
    EVT_STRATEGY_EXECUTED, EVT_STRATEGY_REFLECTED,
}


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callable]] = defaultdict(list)
        # 主事件循环引用：供非循环线程（如 FileWriter 工作线程）将
        # 协程订阅者安全投递回主循环，避免在临时循环上运行造成跨循环对象串扰
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """绑定主事件循环（启动时在循环线程调用）。"""
        try:
            self._loop = loop or asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    def subscribe(self, event: str, handler: Callable) -> Callable:
        """订阅事件。返回取消订阅的闭包。"""
        self._subscribers[event].append(handler)
        if event not in KNOWN_EVENTS:
            logger.debug("订阅了非预置事件：%s", event)

        def _unsub() -> None:
            try:
                self._subscribers[event].remove(handler)
            except ValueError:
                pass

        return _unsub

    def subscriber_count(self, event: str | None = None) -> int:
        if event is not None:
            return len(self._subscribers.get(event, []))
        return sum(len(v) for v in self._subscribers.values())

    async def publish(self, event: str, payload: dict[str, Any] | None = None) -> None:
        """异步发布。同步订阅者直接调用，协程订阅者 await 执行。"""
        payload = payload or {}
        for handler in list(self._subscribers.get(event, [])):
            try:
                result = handler(payload)
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001 - 订阅者隔离
                logger.exception(
                    "事件订阅者执行失败：event=%s handler=%s", event, handler)

    def publish_nowait(self, event: str, payload: dict[str, Any] | None = None) -> None:
        """在无 await 的同步上下文中发布：同步订阅者立即执行，协程订阅者投递到事件循环。"""
        payload = payload or {}
        for handler in list(self._subscribers.get(event, [])):
            try:
                result = handler(payload)
                if inspect.isawaitable(result):
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(result)  # type: ignore[arg-type]
                    except RuntimeError:
                        # 非事件循环线程（如 FileWriter 工作线程）：投递回主循环执行，
                        # 避免 asyncio.run 在临时循环运行造成与主循环对象串扰
                        if self._loop is not None and not self._loop.is_closed():
                            self._loop.call_soon_threadsafe(
                                lambda r=result: self._loop.create_task(r))
                        else:
                            asyncio.run(result)  # type: ignore[arg-type]
            except Exception:  # noqa: BLE001
                logger.exception("事件订阅者执行失败：event=%s", event)
