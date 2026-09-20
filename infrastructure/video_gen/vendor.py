"""百炼地址整理。协议由 provider_type 决定，这里不猜厂商。"""
from __future__ import annotations

from urllib.parse import urlparse


def dashscope_api_root(base_url: str) -> str:
    """把用户填写的百炼地址收成原生 /api/v1。

    compatible-mode 只服务对话，视频合成不在那条路径上。
    """
    raw = (base_url or "").strip().rstrip("/")
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    path = (parsed.path or "").rstrip("/")
    if path.endswith("/api/v1"):
        return origin + path
    marker = "/services/aigc/"
    if marker in path:
        return origin + path.split(marker)[0].rstrip("/")
    return origin + "/api/v1"
