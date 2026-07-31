"""统一时间工具：全系统时间一律使用中国标准时间（Asia/Shanghai, UTC+8）。

约定：
- 所有落库/落盘的时间戳一律来自本模块，禁止直接使用 datetime.now() / datetime('now')。
- 返回 naive datetime（不带 tzinfo），isoformat 后与历史数据格式完全一致
  （如 2026-07-31T16:00:00），保证字符串比较与 fromisoformat 解析兼容。
- 与机器本地时区无关：即使部署机不是东八区，产生的时间也固定为 CST。
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

CST = ZoneInfo("Asia/Shanghai")


def now_cst() -> datetime:
    """当前中国标准时间（naive datetime，无 tzinfo）。"""
    return datetime.now(CST).replace(tzinfo=None)


def now_iso() -> str:
    """当前中国标准时间 ISO 字符串（秒级），如 2026-07-31T16:00:00。"""
    return now_cst().isoformat(timespec="seconds")


def from_ts(ts: float) -> datetime:
    """epoch 秒 → 中国标准时间（naive datetime）。"""
    return datetime.fromtimestamp(ts, CST).replace(tzinfo=None)
