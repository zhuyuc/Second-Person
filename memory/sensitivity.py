"""敏感信息识别与脱敏（供 write_gate / file_writer / langfuse 复用）。

三档语义：
- none：未命中任何模式
- medium：命中 PII 类（手机、邮箱、身份证号中段、地址片段、URL-token、银行卡号），
        允许写入但内容必须脱敏，evidence excerpt 保留脱敏后版本
- high：命中密钥类（API key / secret / access_token / 密码 / 私钥 / 支付密码），
        禁止写入原文，evidence 只保留哈希指纹

设计原则：
- 单一入口 `scan(text)` 返回 (level, redacted_text, matches_meta)
- 幂等：多次调用同一输入结果一致
- 无副作用：不落库，不打日志
- 保持结构：脱敏用 [REDACTED:kind] 占位，供后续人工审核识别
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Pattern

# ---------------------------------------------------------------------------
# 模式表：kind → (pattern, level)
# 排序对性能有意义（长模式在前避免短模式先命中截断），也影响脱敏顺序
# ---------------------------------------------------------------------------

_HIGH_PATTERNS: list[tuple[str, Pattern[str]]] = [
    ("api_key", re.compile(r"(?i)\b(?:api[_ -]?key|secret[_ -]?key|access[_ -]?token|refresh[_ -]?token)\s*[:：=]\s*[\w\-]{8,}")),
    ("password", re.compile(r"(?i)(?:密码|口令|password|passwd|pwd)\s*[:：=为是]\s*\S{4,}")),
    ("private_key", re.compile(r"-----BEGIN\s+[A-Z ]*PRIVATE KEY-----[\s\S]+?-----END\s+[A-Z ]*PRIVATE KEY-----")),
    ("token_literal", re.compile(r"\b(?:sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{12,}|ghp_[A-Za-z0-9]{20,}|xoxb-[A-Za-z0-9\-]{20,})\b")),
    ("verification_code", re.compile(r"(?i)(?:验证码|verification code|otp)\s*[:：是=]\s*\d{4,8}")),
    ("id_card", re.compile(r"\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b")),
    ("bank_card", re.compile(r"\b(?:62|4\d|5[1-5]|3[47])\d{13,17}\b")),
    ("payment_password", re.compile(r"(?i)(?:支付密码|支付口令|pay[_ -]?password)\s*[:：=为是]\s*\S{4,}")),
]

_MEDIUM_PATTERNS: list[tuple[str, Pattern[str]]] = [
    # 中国手机号（11 位，1 开头）
    ("cn_mobile", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    # 邮箱
    ("email", re.compile(r"\b[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\b")),
    # URL 上的 token/key 参数
    ("url_token", re.compile(r"(?i)[?&](?:token|access_token|key|api_key|auth)=[\w\-.~+/]+")),
    # 中国详细地址：省市区/县 + 街道/号
    ("cn_address", re.compile(r"[一-鿿]{2,}(?:省|自治区|市)[一-鿿]{2,}(?:市|区|县|自治县|旗)[一-鿿0-9]{2,}(?:街|路|巷|号|弄|栋|幢|室)[一-鿿0-9\-]{0,20}")),
    # 座机号（可选区号）
    ("cn_landline", re.compile(r"(?<!\d)(?:0\d{2,3}[- ]?)?\d{7,8}(?!\d)")),
]

# 兼容旧 API：write_gate.sensitivity_level 已在使用
_LEGACY_HIGH_PATTERNS = _HIGH_PATTERNS  # alias


@dataclass(frozen=True)
class ScanResult:
    """脱敏扫描结果。"""

    level: str            # none | medium | high
    redacted: str         # 脱敏后的文本；level=none 时 == 原文
    matches: tuple[tuple[str, str], ...]  # ((kind, level), ...)  仅元信息，不含匹配值

    @property
    def has_secret(self) -> bool:
        return self.level == "high"

    @property
    def needs_redact(self) -> bool:
        return self.level != "none"


def scan(text: str | None) -> ScanResult:
    """扫描文本并返回脱敏结果。空文本视为 none。"""
    if not text:
        return ScanResult("none", text or "", ())
    out = str(text)
    matches: list[tuple[str, str]] = []
    level = "none"
    for kind, pattern in _HIGH_PATTERNS:
        if pattern.search(out):
            level = "high"
            matches.append((kind, "high"))
            out = pattern.sub(lambda m, k=kind: f"[REDACTED:{k}]", out)
    for kind, pattern in _MEDIUM_PATTERNS:
        if pattern.search(out):
            if level != "high":
                level = "medium"
            matches.append((kind, "medium"))
            out = pattern.sub(lambda m, k=kind: f"[REDACTED:{k}]", out)
    return ScanResult(level=level, redacted=out, matches=tuple(matches))


def detect_level(text: str | None) -> str:
    """轻量入口：只返回 none/medium/high。"""
    return scan(text).level


def redact(text: str | None) -> str:
    """轻量入口：只返回脱敏后文本。"""
    return scan(text).redacted


def hash_secret(text: str | None) -> str:
    """把 high 敏感原文压成 16 位摘要（审计/去重用，不能反解）。"""
    if not text:
        return ""
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:16]


def redact_evidence(evidence: dict | None) -> dict | None:
    """脱敏 evidence dict：medium 替换 excerpt/locator，high 只留 hash。

    None/非 dict 原样返回；不修改传入对象，返回新的 dict。
    """
    if not isinstance(evidence, dict):
        return evidence
    excerpt = evidence.get("excerpt")
    if not excerpt:
        return dict(evidence)
    result = dict(evidence)
    scanned = scan(excerpt)
    if scanned.level == "high":
        result["excerpt"] = "[REDACTED:high-sensitivity-content]"
        result["excerpt_hash"] = hash_secret(excerpt)
        result["sensitivity_level"] = "high"
    elif scanned.level == "medium":
        result["excerpt"] = scanned.redacted
        result["sensitivity_level"] = "medium"
    return result


def redact_payload_for_trace(payload):
    """LangFuse span input/output 上报前的脱敏包装。

    - str：过 scan 后返回脱敏文本
    - list/dict：递归处理值（字段名保留原样）
    - 其它标量：原样返回
    深度上限 6 层，防止环状引用；超深处截断为 "[TRUNCATED:depth]"。
    """
    return _walk_redact(payload, depth=0)


_MAX_TRACE_DEPTH = 6


def _walk_redact(value, depth: int):
    if depth > _MAX_TRACE_DEPTH:
        return "[TRUNCATED:depth]"
    if isinstance(value, str):
        result = scan(value)
        return result.redacted if result.needs_redact else value
    if isinstance(value, dict):
        return {k: _walk_redact(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        walked = [_walk_redact(v, depth + 1) for v in value]
        return type(value)(walked) if isinstance(value, tuple) else walked
    return value
