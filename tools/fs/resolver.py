"""路径归一化 + 围栏检查（v5 §六 A1-A5）。

统一接口：
- resolve(raw_path, cwd) → Path：归一化后的绝对路径（realpath，正斜杠可通过 str 转换）
- guard(resolved, writable_roots, read_roots) → 校验落在允许集合内，否则抛 FsError
- ensure_writable(resolved, writable_roots) → 写入前再次 canonicalize + 检查（防 TOCTOU）
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .errors import FsError, FsErrorCode

# Windows 保留名（大小写不敏感）
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_ILLEGAL_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')


def resolve(raw_path: str, *, cwd: Path | None = None) -> Path:
    """把 raw_path 归一化为绝对 realpath。相对路径以 cwd 为基。

    抛 FsError(INVALID_PATH) 于：空 / 含非法字符 / realpath 失败。
    """
    if not raw_path or not isinstance(raw_path, str):
        raise FsError(FsErrorCode.INVALID_PATH, "路径不能为空")
    expanded = os.path.expanduser(raw_path.strip())
    p = Path(expanded)
    if not p.is_absolute():
        if cwd is None:
            raise FsError(FsErrorCode.INVALID_PATH,
                          f"相对路径且无 cwd 上下文：{raw_path}", path=raw_path)
        p = cwd / p
    # basename 校验 Windows 保留字（仅在最终段生效）
    if os.name == "nt":
        stem = p.name.split(".")[0].upper()
        if stem in _WINDOWS_RESERVED:
            raise FsError(FsErrorCode.INVALID_PATH,
                          f"Windows 保留名：{p.name}", path=raw_path)
    if _ILLEGAL_CHARS.search(p.name):
        raise FsError(FsErrorCode.INVALID_PATH,
                      f"文件名含非法字符：{p.name}", path=raw_path)
    try:
        resolved = Path(os.path.realpath(p))
    except OSError as exc:
        raise FsError(FsErrorCode.INVALID_PATH,
                      f"路径无法解析：{exc}", path=raw_path) from exc
    return resolved


def _within(candidate: Path, root: Path) -> bool:
    """candidate 是否在 root 内（含 root 本身）。用 realpath 后的路径比较。"""
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def guard(resolved: Path, allowed_roots: tuple[Path, ...] | list[Path],
          *, action: str = "access",
          raw_path: str | None = None) -> Path:
    """resolved 必须位于 allowed_roots 任一之内，否则抛 SANDBOX_DENIED。"""
    for root in allowed_roots:
        if _within(resolved, root):
            return resolved
    roots_str = ", ".join(str(r) for r in allowed_roots) or "（无允许根）"
    raise FsError(
        FsErrorCode.SANDBOX_DENIED,
        f"路径越界，拒绝 {action}：{resolved}（允许：{roots_str}）",
        path=raw_path)


def ensure_writable(resolved: Path, writable_roots: tuple[Path, ...] | list[Path],
                    *, raw_path: str | None = None) -> Path:
    """写入前再次 canonicalize 目标 + 校验（防 resolve → open 之间被替换）。

    对于文件不存在的情形，用其 parent 做 realpath 后拼回 basename。
    """
    if resolved.exists():
        fresh = Path(os.path.realpath(resolved))
    else:
        parent = resolved.parent
        try:
            fresh_parent = Path(os.path.realpath(parent))
        except OSError as exc:
            raise FsError(FsErrorCode.INVALID_PATH,
                          f"父目录无法解析：{exc}", path=raw_path) from exc
        fresh = fresh_parent / resolved.name
    return guard(fresh, writable_roots, action="write", raw_path=raw_path)
