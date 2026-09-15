"""启动链路优化相关单元测试。"""
from __future__ import annotations

from infrastructure.startup_status import StartupStatus
from infrastructure.startup_timing import StartupTimer


def test_startup_status_core_ready():
    s = StartupStatus()
    assert s.snapshot()["core_ready"] is False
    s.set("core", "ready")
    s.set("embedding", "loading")
    snap = s.snapshot()
    assert snap["core_ready"] is True
    assert snap["stages"]["embedding"] == "loading"


def test_startup_timer_marks(tmp_path):
    t = StartupTimer()
    t.mark("a")
    t.mark("b")
    out = tmp_path / "timing.json"
    t.dump(out)
    text = out.read_text(encoding="utf-8")
    assert "a" in text and "b" in text
    assert "total_ms" in text
