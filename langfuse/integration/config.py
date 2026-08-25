"""
Langfuse 配置：优先读环境变量，其次读 config.yaml（通过传入的 get 回调）。

环境变量：
- LANGFUSE_ENABLED      "1"/"true"/"on" 开启；默认在提供了密钥时自动开启
- LANGFUSE_HOST         Langfuse 地址，默认 http://localhost:3000（自托管）
- LANGFUSE_PUBLIC_KEY   项目 Public Key（pk-lf-...）
- LANGFUSE_SECRET_KEY   项目 Secret Key（sk-lf-...）
- LANGFUSE_FLUSH_INTERVAL / LANGFUSE_FLUSH_BATCH  上报节流参数（可选）

对应的 config.yaml 键：langfuse_enabled / langfuse_host /
langfuse_public_key / langfuse_secret_key。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Optional

_TRUE = {"1", "true", "yes", "on", "True"}


def _env(name: str) -> Optional[str]:
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else None


@dataclass
class LangfuseConfig:
    enabled: bool = False
    host: str = "http://localhost:3000"
    public_key: str = ""
    secret_key: str = ""
    flush_interval: float = 3.0
    flush_batch: int = 20
    release: str = ""

    @classmethod
    def from_sources(cls, get: Optional[Callable[[str, object], object]] = None) -> "LangfuseConfig":
        """get: 形如 config.get(key, default) 的回调，可为 None（只读环境变量）。"""
        def cfg(key: str, default=None):
            if get is None:
                return default
            try:
                return get(key, default)
            except Exception:  # noqa: BLE001
                return default

        host = _env("LANGFUSE_HOST") or (
            cfg("langfuse_host", "") or "") or "http://localhost:3000"
        public = _env("LANGFUSE_PUBLIC_KEY") or (
            cfg("langfuse_public_key", "") or "")
        secret = _env("LANGFUSE_SECRET_KEY") or (
            cfg("langfuse_secret_key", "") or "")

        # 是否开启：显式标志优先，否则在提供了密钥对时自动开启
        flag_raw = _env("LANGFUSE_ENABLED")
        if flag_raw is None:
            cfg_flag = cfg("langfuse_enabled", None)
            flag = None if cfg_flag is None else bool(cfg_flag)
        else:
            flag = flag_raw in _TRUE
        has_keys = bool(public and secret)
        enabled = (flag if flag is not None else has_keys) and has_keys

        try:
            interval = float(_env("LANGFUSE_FLUSH_INTERVAL")
                             or cfg("langfuse_flush_interval", 3.0))
        except (TypeError, ValueError):
            interval = 3.0
        try:
            batch = int(_env("LANGFUSE_FLUSH_BATCH")
                        or cfg("langfuse_flush_batch", 20))
        except (TypeError, ValueError):
            batch = 20

        return cls(enabled=enabled, host=host.rstrip("/"), public_key=public,
                   secret_key=secret, flush_interval=interval, flush_batch=batch,
                   release=_env("LANGFUSE_RELEASE") or "")
