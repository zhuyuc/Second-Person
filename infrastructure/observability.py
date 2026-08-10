"""
可观测性 —— 分层日志 / trace_id 全链路 / 慢请求检测 / operation_logs 写入。

对齐产品文档 §可观测性：
- 分层日志 DEBUG > INFO > WARN > ERROR
- 每个用户请求分配 trace_id，全链路携带（contextvars 传播）
- 操作日志写入触发点（Provider 增删改、参数修改、备份、记忆删除、SOUL 编辑等）
  写入 operation_logs 表，保留 90 天，仅供内部排障不提供查询界面
- 慢请求检测：耗时超阈值自动标记
"""
from __future__ import annotations

import logging
import time
import uuid
from contextvars import ContextVar
from datetime import timedelta
from typing import Any
from infrastructure.timeutil import now_cst

# 当前请求的 trace_id（跨 await 传播）
_trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)

SLOW_REQUEST_THRESHOLD_MS = 3000


def new_trace_id() -> str:
    return f"tr_{uuid.uuid4().hex[:12]}"


def set_trace_id(trace_id: str | None = None) -> str:
    tid = trace_id or new_trace_id()
    _trace_id_var.set(tid)
    return tid


def get_trace_id() -> str | None:
    return _trace_id_var.get()


class _TraceFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = get_trace_id() or "-"
        return True


def setup_logging(level: str = "INFO") -> None:
    """初始化根 logger，日志行携带 trace_id。"""
    handler = logging.StreamHandler()
    handler.addFilter(_TraceFilter())
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(trace_id)s] %(name)s: %(message)s"
    ))
    root = logging.getLogger("second_person")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.propagate = False


class OperationLogger:
    """操作日志写入器；写入 operation_logs 表（保留 90 天，无查询 API）。"""

    def __init__(self, db) -> None:  # db: infrastructure.db.Database
        self._db = db

    def log(self, operation: str, detail: str = "", trace_id: str | None = None) -> None:
        try:
            # 火忘式写入：操作日志属旁路记录，入队即返回不占调用线程
            self._db.execute_nowait(
                "INSERT INTO operation_logs(operation, detail, trace_id, create_time) "
                "VALUES(?,?,?,?)",
                (operation, detail, trace_id or get_trace_id(),
                 now_cst().isoformat(timespec="seconds")),
            )
        except Exception:  # noqa: BLE001 - 日志失败不应影响主流程
            logging.getLogger("second_person.oplog").exception("操作日志写入失败")

    def purge_expired(self, retention_days: int = 90) -> int:
        """清理超期操作日志，返回删除条数。"""
        cutoff_iso = (now_cst() - timedelta(days=retention_days)).isoformat(
            timespec="seconds")
        cur = self._db.execute(
            "DELETE FROM operation_logs WHERE create_time < ?", (cutoff_iso,)
        )
        return cur.rowcount if cur else 0


class Timer:
    """上下文管理器：测量耗时并对慢请求告警。"""

    def __init__(self, label: str, threshold_ms: int = SLOW_REQUEST_THRESHOLD_MS):
        self.label = label
        self.threshold_ms = threshold_ms
        self._start = 0.0
        self.elapsed_ms = 0.0

    def __enter__(self) -> "Timer":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.elapsed_ms = (time.perf_counter() - self._start) * 1000
        if self.elapsed_ms > self.threshold_ms:
            logging.getLogger("second_person.slow").warning(
                "慢操作 %s 耗时 %.0fms（阈值 %dms）",
                self.label, self.elapsed_ms, self.threshold_ms,
            )


class EventLoopMonitor:
    """事件循环卡顿哨兵（对话零阻塞架构的回归防线）。

    每秒 sleep(1) 后对比单调时钟漂移：漂移超阈值说明有同步重操作占用了
    事件循环（会冻结对话 SSE），>0.5s 记 warning，>2s 记 error，
    新引入的阻塞点立即在日志中现形。成本近零。
    """

    WARN_SEC = 0.5
    ERROR_SEC = 2.0

    def __init__(self) -> None:
        self._task = None
        self._log = logging.getLogger("second_person.loop_monitor")

    async def start(self) -> None:
        import asyncio
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        import asyncio
        try:
            while True:
                t0 = time.monotonic()
                await asyncio.sleep(1)
                lag = time.monotonic() - t0 - 1
                if lag > self.ERROR_SEC:
                    self._log.error(
                        "事件循环被阻塞 %.2fs！存在同步重操作占用循环，"
                        "会冻结对话流，请排查近期变更", lag)
                elif lag > self.WARN_SEC:
                    self._log.warning("事件循环卡顿 %.2fs（阈值 %.1fs）",
                                      lag, self.WARN_SEC)
        except asyncio.CancelledError:
            pass
