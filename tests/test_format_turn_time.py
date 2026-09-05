"""format_turn_time 必须显式带星期，避免模型从日期自行推错。"""
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from agent.turn_runtime_helpers import format_turn_time


def test_format_turn_time_includes_weekday_saturday():
    # 2026-09-05 为星期六（用户复现「周五」误报的同一天）
    fixed = datetime(2026, 9, 5, 20, 46, 32, tzinfo=ZoneInfo("Asia/Shanghai"))

    class _Proxy:
        def weekday(self):
            return fixed.weekday()

        def __format__(self, spec):
            return format(fixed, spec)

    with patch("agent.turn_runtime_helpers.now_cst", return_value=_Proxy()):
        text = format_turn_time()
    assert text == "[北京时间] 2026-09-05 星期六"
