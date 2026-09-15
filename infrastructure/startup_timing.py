"""启动耗时打点：写入 data/logs/startup-timing.json，便于回归对比。"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("second_person.startup_timing")


class StartupTimer:
    def __init__(self) -> None:
        self._t0 = time.perf_counter()
        self._marks: list[dict[str, Any]] = []
        self._last = self._t0

    def mark(self, name: str, **extra: Any) -> float:
        now = time.perf_counter()
        delta_ms = round((now - self._last) * 1000)
        total_ms = round((now - self._t0) * 1000)
        row = {"name": name, "delta_ms": delta_ms, "total_ms": total_ms}
        if extra:
            row.update(extra)
        self._marks.append(row)
        self._last = now
        logger.info("startup timing %s delta_ms=%s total_ms=%s",
                    name, delta_ms, total_ms)
        return delta_ms

    def dump(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "marks": self._marks,
            "total_ms": round((time.perf_counter() - self._t0) * 1000),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
