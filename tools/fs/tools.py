"""7 个 fs 工具（v5 §六 6.3）+ register_fs_tools。

模型可见接口（snake_case + JSON schema）：
- fs_read (path, offset?, limit?)
- fs_read_image (path)
- fs_list (path, depth?)
- fs_glob (pattern, path?)
- fs_grep (regex, path?, glob?, context?)
- fs_write (path, content, expected_version?)
- fs_edit (path, old_string, new_string, replace_all?, expected_version?)

ToolExecutor 在调用前会向 kwargs 注入 `_ws_ctx: WorkspaceContext`（不进 schema）。
所有工具默认注册；工具集是否可见由 SessionCtx.sandbox_mode 决定（read-only 档
剔除 fs_write/fs_edit）。执行层再由 WorkspaceContext.writable_roots/read_roots
做二次围栏——非项目会话默认锚 legacy_workspace + 白名单。
"""
from __future__ import annotations

import base64
import logging
import re
import subprocess
from pathlib import Path

from ..base import ToolRegistry, ToolSpec
from .diff import summary_stats, unified_diff
from .errors import FsError, FsErrorCode
from .ignore import IgnoreMatcher
from .io import (
    DEFAULT_READ_LIMIT_LINES, atomic_write, literal_edit,
    make_version, read_file,
)
from .observation import FsObservationStore
from .resolver import ensure_writable, guard, resolve
from .workspace import WorkspaceContext

logger = logging.getLogger("second_person.fs.tools")

IMAGE_EXT_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif",
}
IMAGE_MAGIC = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),   # 需二次校验 WEBP
]
IMAGE_MAX_BYTES = 10 * 1024 * 1024


def _pop_ctx(kwargs: dict) -> WorkspaceContext:
    ctx = kwargs.pop("_ws_ctx", None)
    if ctx is None:
        raise FsError(FsErrorCode.SANDBOX_DENIED,
                      "缺少工作区上下文（内部错误）")
    return ctx


def _all_read_roots(ctx: WorkspaceContext) -> tuple[Path, ...]:
    """读取允许根：danger-full-access 返回空 tuple 视为「全盘允许」。"""
    return ctx.read_roots


def _check_read_allowed(resolved: Path, ctx: WorkspaceContext,
                        raw_path: str) -> None:
    if ctx.is_danger():
        return  # 全盘可读
    if not ctx.read_roots:
        raise FsError(FsErrorCode.SANDBOX_DENIED,
                      "当前档位不允许读取任何路径", path=raw_path)
    guard(resolved, ctx.read_roots, action="read", raw_path=raw_path)


def _check_write_allowed(resolved: Path, ctx: WorkspaceContext,
                         raw_path: str) -> Path:
    if ctx.is_readonly():
        raise FsError(FsErrorCode.SANDBOX_DENIED,
                      "当前策略为 read-only，禁止写入", path=raw_path)
    if ctx.is_danger():
        # 全盘可写，但仍走一次 canonicalize（防 TOCTOU）
        return resolved
    if not ctx.writable_roots:
        raise FsError(FsErrorCode.SANDBOX_DENIED,
                      "当前档位没有可写目录", path=raw_path)
    return ensure_writable(resolved, ctx.writable_roots, raw_path=raw_path)


def _resolve_input_path(raw_path: str, ctx: WorkspaceContext) -> Path:
    return resolve(raw_path, cwd=ctx.cwd())


# ============================================================================
# 工具实现
# ============================================================================

def _make_fs_read(observations: FsObservationStore, config):
    async def fs_read(path: str, offset: int = 1, limit: int | None = None,
                      **kwargs) -> dict:
        ctx = _pop_ctx(kwargs)
        resolved = _resolve_input_path(path, ctx)
        _check_read_allowed(resolved, ctx, raw_path=path)
        cfg = _fs_cfg(config)
        result = read_file(
            resolved, offset=offset, limit=limit,
            max_line_chars=cfg["max_line"], max_bytes=cfg["max_bytes"],
            read_limit_lines=cfg["read_limit_lines"],
            stream_min=cfg["stream_min"], absolute_max=cfg["absolute_max"])
        # 记录观察 供后续 fs_write/edit 版本乐观锁
        observations.record(ctx.session_id, str(resolved), result["version"])
        return result
    return fs_read


def _make_fs_read_image():
    async def fs_read_image(path: str, **kwargs) -> dict:
        ctx = _pop_ctx(kwargs)
        resolved = _resolve_input_path(path, ctx)
        _check_read_allowed(resolved, ctx, raw_path=path)
        if not resolved.exists() or not resolved.is_file():
            raise FsError(FsErrorCode.NOT_FOUND, f"图片不存在：{resolved}",
                          path=path)
        stat = resolved.stat()
        if stat.st_size > IMAGE_MAX_BYTES:
            raise FsError(FsErrorCode.TOO_LARGE,
                          f"图片超过 {IMAGE_MAX_BYTES // 1024 // 1024}MB 限制",
                          path=path)
        ext = resolved.suffix.lower()
        declared_mime = IMAGE_EXT_MIME.get(ext)
        if not declared_mime:
            raise FsError(FsErrorCode.NOT_TEXT,
                          "fs_read_image 仅支持 PNG/JPEG/WebP/GIF",
                          path=path)
        raw = resolved.read_bytes()
        # magic-byte 校验
        magic_ok = False
        for header, mime in IMAGE_MAGIC:
            if raw.startswith(header):
                if mime == declared_mime or (
                        mime == "image/webp" and raw[8:12] == b"WEBP"):
                    magic_ok = True
                    break
        if not magic_ok:
            raise FsError(FsErrorCode.NOT_TEXT,
                          f"图片 magic-byte 不匹配声明的扩展名 {ext}",
                          path=path)
        data_url = f"data:{declared_mime};base64," + \
            base64.b64encode(raw).decode()
        return {
            "path": str(resolved), "type": "image",
            "media_type": declared_mime,
            "size_bytes": stat.st_size, "data_url": data_url,
        }
    return fs_read_image


def _make_fs_list():
    async def fs_list(path: str, depth: int = 1, **kwargs) -> dict:
        ctx = _pop_ctx(kwargs)
        resolved = _resolve_input_path(path, ctx)
        _check_read_allowed(resolved, ctx, raw_path=path)
        if not resolved.is_dir():
            raise FsError(FsErrorCode.NOT_REGULAR_FILE,
                          f"不是目录：{resolved}", path=path)
        depth = max(1, min(3, int(depth or 1)))
        ignore = _make_ignore(ctx)
        entries: list[dict] = []
        for entry in _iter_entries(resolved, depth, ignore):
            entries.append(entry)
            if len(entries) >= 500:
                break
        return {"path": str(resolved), "entries": entries,
                "truncated": len(entries) >= 500}
    return fs_list


def _iter_entries(root: Path, depth: int, ignore: IgnoreMatcher):
    """深度优先遍历，depth 是相对 root 的最大深度。"""
    stack = [(root, 0)]
    while stack:
        dir_path, dir_depth = stack.pop()
        try:
            children = sorted(dir_path.iterdir(),
                               key=lambda p: (not p.is_dir(), p.name.lower()))
        except (OSError, PermissionError):
            continue
        for c in children:
            try:
                if ignore.match(c):
                    continue
                stat = c.stat()
                yield {
                    "name": c.name,
                    "path": str(c),
                    "type": "dir" if c.is_dir() else "file",
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                }
                if c.is_dir() and dir_depth + 1 < depth:
                    stack.append((c, dir_depth + 1))
            except OSError:
                continue


def _make_fs_glob():
    async def fs_glob(pattern: str, path: str | None = None,
                       **kwargs) -> dict:
        ctx = _pop_ctx(kwargs)
        base = _resolve_input_path(path, ctx) if path else ctx.cwd()
        _check_read_allowed(base, ctx, raw_path=str(base))
        if not base.is_dir():
            raise FsError(FsErrorCode.NOT_REGULAR_FILE,
                          f"glob 基路径不是目录：{base}",
                          path=str(base))
        ignore = _make_ignore(ctx)
        matches: list[str] = []
        # 分 rglob 递归 与 单层 glob 两种，pattern 里有 ** 走递归
        iterator = base.rglob(pattern) if "**" in pattern else base.glob(pattern)
        for match in iterator:
            try:
                if ignore.match(match):
                    continue
                matches.append(str(match))
                if len(matches) >= 1000:
                    break
            except OSError:
                continue
        return {"matches": matches, "truncated": len(matches) >= 1000}
    return fs_glob


def _make_fs_grep():
    def _run_ripgrep(regex: str, base: Path, glob: str | None,
                      context: int) -> list[dict] | None:
        """尝试用系统 rg。找不到或异常 → 返 None，走 Python 兜底。"""
        rg = _find_ripgrep()
        if not rg:
            return None
        cmd = [rg, "-n", "-B", str(context), "-A", str(context)]
        if glob:
            cmd += ["-g", glob]
        cmd += ["--", regex, str(base)]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30,
                encoding="utf-8", errors="replace")
        except (subprocess.TimeoutExpired, OSError):
            return None
        if proc.returncode not in (0, 1):
            return None
        hits: list[dict] = []
        for line in proc.stdout.splitlines():
            m = re.match(r"^(.*?):(\d+)[:-](.*)$", line)
            if m:
                hits.append({
                    "file": m.group(1),
                    "line": int(m.group(2)),
                    "text": m.group(3),
                })
                if len(hits) >= 200:
                    break
        return hits

    def _run_python_grep(regex: str, base: Path, glob: str | None,
                          context: int, ignore: IgnoreMatcher) -> list[dict]:
        try:
            pattern = re.compile(regex)
        except re.error as exc:
            raise FsError(FsErrorCode.INVALID_PATH,
                          f"grep 正则非法：{exc}", path=str(base)) from exc
        hits: list[dict] = []
        iterator = base.rglob(glob) if glob else base.rglob("*")
        for f in iterator:
            if not f.is_file() or ignore.match(f):
                continue
            try:
                with open(f, "r", encoding="utf-8", errors="replace") as fp:
                    lines = fp.readlines()
            except OSError:
                continue
            for i, line in enumerate(lines, 1):
                if pattern.search(line):
                    hits.append({"file": str(f), "line": i,
                                  "text": line.rstrip("\n")})
                    if len(hits) >= 200:
                        return hits
        return hits

    async def fs_grep(regex: str, path: str | None = None,
                      glob: str | None = None, context: int = 2,
                      **kwargs) -> dict:
        ctx = _pop_ctx(kwargs)
        base = _resolve_input_path(path, ctx) if path else ctx.cwd()
        _check_read_allowed(base, ctx, raw_path=str(base))
        if not base.exists():
            raise FsError(FsErrorCode.NOT_FOUND, f"路径不存在：{base}",
                          path=str(base))
        context = max(0, min(5, int(context or 2)))
        ignore = _make_ignore(ctx)
        hits = _run_ripgrep(regex, base, glob, context)
        if hits is None:
            hits = _run_python_grep(regex, base, glob, context, ignore)
        return {"hits": hits, "truncated": len(hits) >= 200}
    return fs_grep


def _make_fs_write(observations: FsObservationStore):
    async def fs_write(path: str, content: str,
                        expected_version: str | None = None,
                        **kwargs) -> dict:
        ctx = _pop_ctx(kwargs)
        resolved = _resolve_input_path(path, ctx)
        target = _check_write_allowed(resolved, ctx, raw_path=path)
        # v6：沙箱下沉到会话层——所有档位统一严格校验，不再有 legacy 例外
        action = "created" if not target.exists() else "replaced"
        before = ""
        if target.exists():
            try:
                before = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                before = ""
            # 版本校验：缺 expected_version 且 session 已 observe 过 → 用 session 记录做校验
            current = make_version(target)
            check = expected_version or observations.get(
                ctx.session_id, str(target))
            if check and check != current:
                raise FsError(FsErrorCode.STALE_VERSION,
                              "文件已被外部修改", path=path)
        atomic_write(target, content)
        new_version = make_version(target)
        observations.record(ctx.session_id, str(target), new_version)
        diff = unified_diff(before, content, path=target.name)
        stats = summary_stats(before, content)
        return {
            "path": str(target), "action": action,
            "version": new_version,
            "diff": {
                "before": _clip(before), "after": _clip(content),
                "unified": diff, **stats,
            },
        }
    return fs_write


def _make_fs_edit(observations: FsObservationStore):
    async def fs_edit(path: str, old_string: str, new_string: str,
                       replace_all: bool = False,
                       expected_version: str | None = None,
                       **kwargs) -> dict:
        ctx = _pop_ctx(kwargs)
        if old_string == new_string:
            raise FsError(FsErrorCode.INVALID_PATH,
                          "old_string 与 new_string 完全相同", path=path)
        resolved = _resolve_input_path(path, ctx)
        target = _check_write_allowed(resolved, ctx, raw_path=path)
        # v6：所有档位统一严格校验（必须有观察或 expected_version）
        current = make_version(target) if target.exists() else None
        check = expected_version or observations.get(
            ctx.session_id, str(target))
        if current is None:
            raise FsError(FsErrorCode.NOT_FOUND,
                          f"文件不存在：{target}", path=path)
        if not check:
            raise FsError(FsErrorCode.NOT_OBSERVED,
                          "未先 fs_read 该文件", path=path)
        if check != current:
            raise FsError(FsErrorCode.STALE_VERSION,
                          "文件已被外部修改", path=path)
        replacements, before, after = literal_edit(
            target, old_string, new_string, replace_all=replace_all)
        atomic_write(target, after)
        new_version = make_version(target)
        observations.record(ctx.session_id, str(target), new_version)
        return {
            "path": str(target), "action": "edited",
            "replacements": replacements,
            "version": new_version,
            "diff": {
                "before": _clip(before), "after": _clip(after),
                "unified": unified_diff(before, after, path=target.name),
                **summary_stats(before, after),
            },
        }
    return fs_edit


# ============================================================================
# 辅助
# ============================================================================

def _clip(s: str, limit: int = 4000) -> str:
    return s if len(s) <= limit else s[:limit] + f"\n... (truncated to {limit} chars)"


def _find_ripgrep() -> str | None:
    """探测系统 rg 二进制；找到返回路径，否则 None。"""
    from shutil import which
    return which("rg")


def _fs_cfg(config) -> dict:
    fs_raw = config.get_raw("fs", {}) if hasattr(config, "get_raw") else {}
    return {
        "read_limit_lines": int(fs_raw.get(
            "read_limit_lines", DEFAULT_READ_LIMIT_LINES)),
        "max_line": int(fs_raw.get("read_max_line_chars", 2000)),
        "max_bytes": int(fs_raw.get("read_max_bytes", 51_200)),
        "stream_min": int(fs_raw.get("read_stream_min_size", 10 * 1024 * 1024)),
        "absolute_max": int(fs_raw.get(
            "read_max_bytes_absolute", 100 * 1024 * 1024)),
    }


_ignore_cache: dict[str, IgnoreMatcher] = {}


def _make_ignore(ctx: WorkspaceContext) -> IgnoreMatcher:
    root = ctx.project_root or (ctx.writable_roots[0] if ctx.writable_roots
                                 else Path.cwd())
    key = str(root)
    cached = _ignore_cache.get(key)
    # 简单缓存：项目会话稳定，进程存活期内可复用；实际生产可加 TTL
    if cached is not None:
        return cached
    matcher = IgnoreMatcher(root)
    _ignore_cache[key] = matcher
    return matcher


# ============================================================================
# 注册
# ============================================================================

def register_fs_tools(registry: ToolRegistry, *, observation_store, config) -> None:
    """向 ToolRegistry 注册全部 7 个 fs 工具。tool.spec.needs_workspace=True。"""
    fs_read = _make_fs_read(observation_store, config)
    fs_read_image = _make_fs_read_image()
    fs_list = _make_fs_list()
    fs_glob = _make_fs_glob()
    fs_grep = _make_fs_grep()
    fs_write = _make_fs_write(observation_store)
    fs_edit = _make_fs_edit(observation_store)

    def _spec(name, description, params, needs_ws=True):
        s = ToolSpec(name, description, params)
        s.needs_workspace = needs_ws
        return s

    registry.register_function(_spec(
        "fs_read", "读取项目内文本文件；返回带行号的分页内容。"
        "写入/编辑前必须先调用。",
        {"type": "object", "properties": {
            "path": {"type": "string", "description": "文件路径（相对项目根或绝对）"},
            "offset": {"type": "integer", "description": "起始行（1 base），默认 1"},
            "limit": {"type": "integer", "description": "最多返回行数，默认 2000"},
        }, "required": ["path"]}), fs_read)

    registry.register_function(_spec(
        "fs_read_image", "读取图片文件（PNG/JPEG/WebP/GIF）；返回 dataURL 供模型多模态处理。",
        {"type": "object", "properties": {
            "path": {"type": "string"},
        }, "required": ["path"]}), fs_read_image)

    registry.register_function(_spec(
        "fs_list", "列出目录内容（默认单层，最深 3 层）；应用 .gitignore 与默认忽略规则。",
        {"type": "object", "properties": {
            "path": {"type": "string"},
            "depth": {"type": "integer", "description": "1-3，默认 1"},
        }, "required": ["path"]}), fs_list)

    registry.register_function(_spec(
        "fs_glob", "按 glob pattern 查找文件（支持 **）；上限 1000。",
        {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "如 **/*.py"},
            "path": {"type": "string", "description": "基路径，默认项目根"},
        }, "required": ["pattern"]}), fs_glob)

    registry.register_function(_spec(
        "fs_grep", "在项目内正则搜索文本；优先调用系统 ripgrep，兜底 Python 实现。",
        {"type": "object", "properties": {
            "regex": {"type": "string"},
            "path": {"type": "string"},
            "glob": {"type": "string", "description": "限定文件类型，如 *.py"},
            "context": {"type": "integer", "description": "上下文行数 0-5，默认 2"},
        }, "required": ["regex"]}), fs_grep)

    registry.register_function(_spec(
        "fs_write", "全量创建或覆写文件；无参 expected_version 时依赖会话观察记录做乐观锁。"
        "写入前必须先 fs_read。read-only 档拒绝。",
        {"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "expected_version": {"type": "string", "description": "上次 fs_read 返回的 version"},
        }, "required": ["path", "content"]}), fs_write)

    registry.register_function(_spec(
        "fs_edit", "字面替换文件片段。old_string 必须唯一或加 replace_all=true。"
        "编辑前必须 fs_read。",
        {"type": "object", "properties": {
            "path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean"},
            "expected_version": {"type": "string"},
        }, "required": ["path", "old_string", "new_string"]}), fs_edit)
