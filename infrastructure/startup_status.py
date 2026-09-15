"""启动就绪阶段状态 —— 供 /api/health 与后台外围任务共享。

core：聊天主链路可用（DB / FileWriter / 调度等）
embedding：本地向量服务（ready / loading / disabled / error）
gateway：IM 适配器
peripherals：连接器、watcher、自检等后台项
"""
from __future__ import annotations

import threading
import time
from typing import Any


class StartupStatus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stages: dict[str, str] = {
            "core": "starting",
            "embedding": "unknown",
            "gateway": "pending",
            "peripherals": "pending",
        }
        self._details: dict[str, Any] = {}
        self._updated_at = time.time()

    def set(self, stage: str, status: str, **detail: Any) -> None:
        with self._lock:
            if stage in self._stages:
                self._stages[stage] = status
            if detail:
                cur = dict(self._details.get(stage) or {})
                cur.update(detail)
                self._details[stage] = cur
            self._updated_at = time.time()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "stages": dict(self._stages),
                "details": {k: dict(v) for k, v in self._details.items()},
                "updated_at": self._updated_at,
                "core_ready": self._stages.get("core") == "ready",
            }


# 进程内单例：create_app / lifespan / health 共用
status = StartupStatus()
