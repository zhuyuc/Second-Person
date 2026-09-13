"""可灵开放平台鉴权。

新版（模型 ID 在路径中，如 kling-3.0-turbo / kling-2.6）：控制台 API Key，Bearer 直传。
旧版（model_name 参数，如 kling-v1 / kling-v2-6）：AccessKey:SecretKey 签 JWT。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time

_LEGACY_MODEL = re.compile(r"^kling-v\d", re.I)


def is_legacy_kling_model(model_id: str) -> bool:
    """旧版设计：模型写在 model_name；新版：模型写在 URL 路径。"""
    return bool(_LEGACY_MODEL.match((model_id or "").strip()))


def looks_like_ak_sk(api_key: str) -> bool:
    key = (api_key or "").strip()
    return ":" in key and not key.lower().startswith(("sk-", "bearer "))


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def encode_kling_jwt(access_key: str, secret_key: str, ttl_sec: int = 1800) -> str:
    header = _b64url(json.dumps(
        {"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    now = int(time.time())
    payload = _b64url(json.dumps(
        {"iss": access_key, "exp": now + ttl_sec, "nbf": now - 5},
        separators=(",", ":")).encode())
    sig = hmac.new(
        secret_key.encode("utf-8"),
        f"{header}.{payload}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{header}.{payload}.{_b64url(sig)}"


def kling_auth_header(api_key: str, model_id: str | None = None) -> dict[str, str]:
    key = (api_key or "").strip()
    if not key:
        return {}
    if key.lower().startswith("bearer "):
        return {"Authorization": key}
    # 未指定模型时保持旧行为（AK:SK → JWT），供单测与旧调用。
    legacy = True if model_id is None else is_legacy_kling_model(model_id)
    if legacy and looks_like_ak_sk(key):
        ak, sk = key.split(":", 1)
        ak, sk = ak.strip(), sk.strip()
        if ak and sk:
            return {"Authorization": f"Bearer {encode_kling_jwt(ak, sk)}"}
    return {"Authorization": f"Bearer {key}"}
