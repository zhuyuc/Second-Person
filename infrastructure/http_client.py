"""
分级 HTTP 超时管理（对外 HTTP 请求的超时统一配置点）。

不同场景的时延特征差异很大：本地 Embedding 微服务毫秒级返回、LLM 流式
回复可持续数分钟、网页抓取要求快速失败。统一硬编码超时（如一律等 120s）
会让轻量请求失败过慢、长任务被误杀。本模块按 profile 提供分级
httpx.Timeout 配置：

- default：LLM 常规非流式调用
- embedding：本地 Embedding 微服务（读超时短，快速失败）
- stream：LLM 流式回复（读超时长，按 chunk 间隔计时）
- quick：轻量快速任务（快速意图预判/参数推断等）的标准配置
- web：外部网页抓取/搜索的标准配置（connect/pool 基线）

契约由 tests/test_optimizations.py::test_timeout_for 守护。
"""
from __future__ import annotations

import httpx

TIMEOUTS: dict[str, httpx.Timeout] = {
    "default": httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0),
    "embedding": httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0),
    "stream": httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0),
    "quick": httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
    "web": httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=10.0),
}


def timeout_for(profile: str) -> httpx.Timeout:
    """按 profile 返回对应超时配置；未知 profile 回退 default。"""
    return TIMEOUTS.get(profile, TIMEOUTS["default"])
