"""底层文件 I/O：编码、二进制、流读、原子写、字面 edit（v5 §六 B1-B10）。"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

from .errors import FsError, FsErrorCode

# 上限（可从 config 覆写；此处为默认）
DEFAULT_READ_LIMIT_LINES = 2000
DEFAULT_READ_MAX_LINE_CHARS = 2000
DEFAULT_READ_MAX_BYTES = 51_200            # 单次 read 累计字节
DEFAULT_READ_STREAM_MIN = 10 * 1024 * 1024  # ≥10MB 走流
READ_MAX_BYTES_ABSOLUTE = 100 * 1024 * 1024  # 硬上限 100MB


def is_binary(sample: bytes) -> bool:
    """前 8KB 有 NUL 字节即认为二进制（快速启发式，与 git 一致）。"""
    return b"\x00" in sample[:8192]


def _decode_text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        # 兼容 UTF-8-BOM / UTF-16 / GBK 常见场景，都以 replace 兜底
        for enc in ("utf-8-sig", "utf-16", "gb18030"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")


def _detect_line_ending(sample: str) -> str:
    """检测文件换行风格，写入时保持一致。默认 \\n。"""
    if "\r\n" in sample:
        return "\r\n"
    return "\n"


def read_file(resolved: Path, *, offset: int = 1, limit: int | None = None,
              max_line_chars: int = DEFAULT_READ_MAX_LINE_CHARS,
              max_bytes: int = DEFAULT_READ_MAX_BYTES,
              read_limit_lines: int = DEFAULT_READ_LIMIT_LINES,
              stream_min: int = DEFAULT_READ_STREAM_MIN,
              absolute_max: int = READ_MAX_BYTES_ABSOLUTE) -> dict:
    """带行号 + 分页的文本读取。返回 dict envelope。

    抛：FS_NOT_FOUND / FS_NOT_REGULAR_FILE / FS_TOO_LARGE / FS_NOT_TEXT
    """
    if not resolved.exists():
        raise FsError(FsErrorCode.NOT_FOUND, f"文件不存在：{resolved}",
                      path=str(resolved))
    if not resolved.is_file():
        raise FsError(FsErrorCode.NOT_REGULAR_FILE,
                      f"不是普通文件：{resolved}", path=str(resolved))
    stat = resolved.stat()
    if stat.st_size > absolute_max:
        raise FsError(FsErrorCode.TOO_LARGE,
                      f"文件超过绝对上限（{stat.st_size} > {absolute_max}）",
                      path=str(resolved))

    if limit is None or limit > read_limit_lines:
        limit = read_limit_lines
    if offset < 1:
        offset = 1

    # 前 8KB 判二进制
    with open(resolved, "rb") as fp:
        head = fp.read(8192)
    if is_binary(head):
        raise FsError(FsErrorCode.NOT_TEXT,
                      "文件为二进制，请用 fs_read_image 或 shell 命令处理",
                      path=str(resolved))

    # 流读 vs 全量
    if stat.st_size >= stream_min:
        lines_iter = _stream_lines(resolved)
    else:
        text = _decode_text(resolved.read_bytes())
        lines_iter = iter(text.splitlines(keepends=False))

    selected: list[tuple[int, str]] = []
    byte_count = 0
    total_lines = 0
    truncated = False
    end_line = offset - 1
    for idx, line in enumerate(lines_iter, start=1):
        total_lines = idx
        if idx < offset:
            continue
        if len(selected) >= limit:
            # 继续数总行数以给出准确的 footer
            continue
        if len(line) > max_line_chars:
            line = line[:max_line_chars] + f" ... (line truncated to {max_line_chars} chars)"
        line_bytes = len(line.encode("utf-8", errors="ignore")) + 12  # 行号前缀开销
        if byte_count + line_bytes > max_bytes:
            truncated = True
            break
        selected.append((idx, line))
        byte_count += line_bytes
        end_line = idx

    version = _make_version(resolved)
    content = "\n".join(f"{idx:>6}\t{text}" for idx, text in selected)
    if not selected:
        footer = f"(空区间：offset={offset} 超过文件总行数 {total_lines})"
    elif truncated or (total_lines > end_line):
        footer = (f"(输出截断，显示 {selected[0][0]}-{end_line} 行"
                  f"（共 {total_lines} 行）。使用 offset={end_line + 1} 继续)")
    else:
        footer = f"(文件末尾 - 共 {total_lines} 行)"

    return {
        "path": str(resolved),
        "type": "file",
        "version": version,
        "content": content + "\n\n" + footer,
        "total_lines": total_lines,
        "truncated": truncated,
    }


def _stream_lines(path: Path):
    with open(path, "rb") as fp:
        for raw in fp:
            yield _decode_text(raw.rstrip(b"\r\n"))


def _make_version(path: Path) -> str:
    """乐观锁 version：ino:size:mtime_ns（跨平台稳定）。"""
    st = path.stat()
    return f"{st.st_ino}:{st.st_size}:{st.st_mtime_ns}"


def make_version(path: Path) -> str:
    return _make_version(path)


def atomic_write(resolved: Path, content: str,
                 *, existing_line_ending: str | None = None) -> None:
    """原子写：写 .tmp.{uuid} 后 os.replace，Windows 上也原子。

    换行风格保留：若原文件存在，按其原风格写；否则用系统默认。
    """
    resolved.parent.mkdir(parents=True, exist_ok=True)
    if existing_line_ending is None and resolved.exists():
        # 探测前 4KB
        try:
            with open(resolved, "rb") as fp:
                sample = fp.read(4096)
            existing_line_ending = _detect_line_ending(
                _decode_text(sample))
        except OSError:
            existing_line_ending = "\n"
    if existing_line_ending and existing_line_ending != "\n":
        content = content.replace("\r\n", "\n").replace("\n", existing_line_ending)

    tmp = resolved.parent / f".{resolved.name}.tmp.{uuid.uuid4().hex[:8]}"
    try:
        with open(tmp, "wb") as fp:
            fp.write(content.encode("utf-8"))
        os.replace(tmp, resolved)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def literal_edit(resolved: Path, old_string: str, new_string: str,
                 *, replace_all: bool = False) -> tuple[int, str, str]:
    """字面匹配替换。返回 (次数, before_content, after_content)。

    抛：
      FS_NOT_FOUND / FS_NOT_TEXT
      FS_NO_MATCH：old_string 未找到
      FS_AMBIGUOUS_EDIT：old_string 匹配多处但未指定 replace_all
    """
    if not resolved.exists():
        raise FsError(FsErrorCode.NOT_FOUND, f"文件不存在：{resolved}",
                      path=str(resolved))
    if not resolved.is_file():
        raise FsError(FsErrorCode.NOT_REGULAR_FILE,
                      f"不是普通文件：{resolved}", path=str(resolved))
    raw = resolved.read_bytes()
    if is_binary(raw[:8192]):
        raise FsError(FsErrorCode.NOT_TEXT,
                      "二进制文件不可编辑", path=str(resolved))
    before = _decode_text(raw)
    count = before.count(old_string)
    if count == 0:
        raise FsError(FsErrorCode.NO_MATCH,
                      "old_string 未在文件中找到", path=str(resolved))
    if count > 1 and not replace_all:
        raise FsError(FsErrorCode.AMBIGUOUS_EDIT,
                      f"old_string 匹配 {count} 处，请提供更长上下文或加 replace_all=true",
                      path=str(resolved))
    if replace_all:
        after = before.replace(old_string, new_string)
        replacements = count
    else:
        after = before.replace(old_string, new_string, 1)
        replacements = 1
    return replacements, before, after
