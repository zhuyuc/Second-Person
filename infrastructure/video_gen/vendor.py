"""云端视频厂商地址整理。协议由 provider_type 决定，这里不猜厂商。"""
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


def volcengine_api_root(base_url: str) -> str:
    """把用户填写的方舟地址收成原生 /api/v3。

    文生图 / 文生视频都在 ``/api/v3``。对话用的 ``/api/plan``、``/api/coding``
    以及误粘贴的 ``.../contents/generations/tasks`` 不能当 API 根。
    """
    raw = (base_url or "").strip().rstrip("/")
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    path = (parsed.path or "").rstrip("/")
    # 优先剥离误填的 Plan/Coding，避免 ``/api/plan/v3/...`` 被当成有效根
    if "/api/plan" in path or path.startswith("/api/coding"):
        return origin + "/api/v3"
    if path.endswith("/api/v3"):
        return origin + path
    marker = "/contents/generations"
    if marker in path:
        root = origin + path.split(marker)[0].rstrip("/")
        return root if root.endswith("/api/v3") else origin + "/api/v3"
    return origin + "/api/v3"
