"""
项目工作区路由（v5 §四）。

- GET    /projects?status=active|archived|all
- POST   /projects  {path, title?, sandbox_mode?}
- PATCH  /projects/{id}
- POST   /projects/{id}/archive
- POST   /projects/{id}/unarchive
- DELETE /projects/{id}    （仅 archived）
- POST   /projects/{id}/relocate  {new_path}
- GET    /projects/browse?path=<abs>
- POST   /projects/browse/mkdir  {parent, name}
- POST   /chat/session/{sid}/sandbox-mode  {mode, reason?}
- GET    /chat/session/{sid}/sandbox-mode

响应格式统一 {code, data}；错误抛 HTTPException（fastapi 拦截返 {code, message}）。
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent.projects import (
    ProjectError, ProjectStore, VALID_SANDBOX_MODES,
)
from infrastructure.timeutil import now_cst

logger = logging.getLogger("second_person.routes.projects")
router = APIRouter()


def _c():
    from app.main import get_container
    return get_container()


# ============================================================================
# Pydantic models
# ============================================================================

class ProjectCreateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str = Field(..., min_length=1)
    title: str | None = None
    sandbox_mode: str = "workspace-write"

    @field_validator("sandbox_mode")
    @classmethod
    def _mode_valid(cls, v: str) -> str:
        if v not in VALID_SANDBOX_MODES:
            raise ValueError(f"非法沙箱档位：{v}")
        return v


class ProjectPatchRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str | None = None
    display_order: int | None = None
    sandbox_mode: str | None = None
    ignore_extra: list | None = None

    @field_validator("sandbox_mode")
    @classmethod
    def _mode_valid(cls, v):
        if v is not None and v not in VALID_SANDBOX_MODES:
            raise ValueError(f"非法沙箱档位：{v}")
        return v


class ProjectRelocateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    new_path: str = Field(..., min_length=1)


class MkdirRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    parent: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def _name_ok(cls, v: str) -> str:
        if re.search(r'[<>:"|?*/\\\x00-\x1f]', v):
            raise ValueError("文件名含非法字符")
        # Windows 保留字
        stem = v.split(".")[0].upper()
        if stem in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                    *(f"LPT{i}" for i in range(1, 10))}:
            raise ValueError(f"Windows 保留字：{v}")
        return v


class SandboxModeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    mode: str
    reason: str | None = None

    @field_validator("mode")
    @classmethod
    def _mode_valid(cls, v: str) -> str:
        if v not in VALID_SANDBOX_MODES:
            raise ValueError(f"非法沙箱档位：{v}")
        return v


# ============================================================================
# 项目 CRUD
# ============================================================================

def _project_view(store: ProjectStore, proj) -> dict:
    d = proj.to_dict()
    d["session_count"] = store.session_count(proj.id)
    d["path_missing"] = store.path_missing(proj)
    return d


@router.get("/projects")
async def list_projects(status: str = "active"):
    if status not in ("active", "archived", "all"):
        raise HTTPException(400, "status 必须是 active/archived/all")
    store: ProjectStore = _c().projects
    projects = store.list(status)
    return {"code": 200, "data": [_project_view(store, p) for p in projects]}


@router.post("/projects")
async def create_project(body: ProjectCreateRequest):
    store: ProjectStore = _c().projects
    try:
        proj = store.create(body.path, title=body.title,
                            sandbox_mode=body.sandbox_mode)
    except ProjectError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"code": 200, "data": _project_view(store, proj)}


# --- 目录浏览：必须在 /projects/{project_id} 之前声明，
#     否则 FastAPI 会把 "browse" 当作 project_id 命中 GET /projects/{id}
@router.get("/projects/browse")
async def browse_placeholder(path: str = ""):
    return await _browse_impl(path)


@router.post("/projects/browse/mkdir")
async def browse_mkdir_placeholder(body: MkdirRequest):
    return await _browse_mkdir_impl(body)


@router.post("/projects/browse/native")
async def browse_native():
    """弹出**系统原生文件夹对话框**（v5.1 §DSH 对齐）。

    Second-Person 是本地单用户 app，server 就在用户机器上，所以可以直接
    tkinter.filedialog.askdirectory() 弹原生对话框。返回 {path} 或 {path: null}。
    对话框自身跑在独立线程 + Tk 主循环，不阻塞事件循环。
    """
    import asyncio as _aio
    return await _aio.to_thread(_open_native_folder_dialog)


def _open_native_folder_dialog() -> dict:
    """线程内运行：拉起 Tk 根窗口（hidden）→ 弹原生 askdirectory → 关根窗口。"""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        raise HTTPException(500, "tkinter 不可用，无法弹出系统对话框")

    # 默认起点：last_browse_parent → ~/Documents/project → ~
    config = _c().config
    initial = None
    if hasattr(config, "get_raw"):
        initial = config.get_raw("project", {}).get("last_browse_parent")
    if not initial or not Path(initial).is_dir():
        for cand in ("~/Documents/project", "D:/project", "~"):
            p = Path(os.path.expanduser(cand))
            if p.is_dir():
                initial = str(p)
                break

    root = tk.Tk()
    try:
        root.withdraw()             # 隐藏空 Tk 主窗口
        root.attributes("-topmost", True)  # 对话框置顶，避免藏在浏览器后面
        picked = filedialog.askdirectory(
            title="选择项目目录", initialdir=initial or str(Path.home()),
            mustexist=True, parent=root)
    finally:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass
    if not picked:
        return {"code": 200, "data": {"path": None, "cancelled": True}}
    display = str(Path(picked)).replace("\\", "/")
    # 记住父目录方便下次
    try:
        parent = str(Path(picked).parent).replace("\\", "/")
        raw = config.get_raw("project", {})
        raw["last_browse_parent"] = parent
        config.set_raw("project", raw)
    except Exception:  # noqa: BLE001
        pass
    return {"code": 200, "data": {"path": display,
                                    "name": Path(picked).name,
                                    "cancelled": False}}


@router.get("/projects/{project_id}")
async def get_project(project_id: str):
    store: ProjectStore = _c().projects
    proj = store.get(project_id)
    if not proj:
        raise HTTPException(404, f"项目不存在：{project_id}")
    return {"code": 200, "data": _project_view(store, proj)}


@router.patch("/projects/{project_id}")
async def patch_project(project_id: str, body: ProjectPatchRequest):
    store: ProjectStore = _c().projects
    try:
        proj = store.patch(
            project_id, title=body.title, display_order=body.display_order,
            sandbox_mode=body.sandbox_mode, ignore_extra=body.ignore_extra)
    except ProjectError as exc:
        msg = str(exc)
        code = 404 if "不存在" in msg else 400
        raise HTTPException(code, msg) from exc
    return {"code": 200, "data": _project_view(store, proj)}


@router.post("/projects/{project_id}/archive")
async def archive_project(project_id: str):
    store: ProjectStore = _c().projects
    try:
        result = store.archive(project_id)
    except ProjectError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"code": 200, "data": result}


@router.post("/projects/{project_id}/unarchive")
async def unarchive_project(project_id: str):
    store: ProjectStore = _c().projects
    try:
        result = store.unarchive(project_id)
    except ProjectError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"code": 200, "data": result}


@router.delete("/projects/{project_id}")
async def purge_project(project_id: str):
    store: ProjectStore = _c().projects
    try:
        result = store.purge(project_id)
    except ProjectError as exc:
        msg = str(exc)
        if "不存在" in msg:
            code = 404
        elif "先归档" in msg:
            code = 409
        else:
            code = 400
        raise HTTPException(code, msg) from exc
    # 系统通知：告知已彻底删除
    try:
        _c().notifications.push(
            "project_purged",
            f"项目已永久删除：sessions={result['deleted_sessions']} "
            f"messages={result['deleted_messages']} "
            f"memories={result['deleted_memories']} docs={result['deleted_docs']}")
    except Exception:  # noqa: BLE001
        pass
    return {"code": 200, "data": result}


@router.post("/projects/{project_id}/relocate")
async def relocate_project(project_id: str, body: ProjectRelocateRequest):
    store: ProjectStore = _c().projects
    try:
        proj = store.relocate(project_id, body.new_path)
    except ProjectError as exc:
        msg = str(exc)
        code = 404 if "不存在" in msg else (
            409 if "已加载" in msg else 400)
        raise HTTPException(code, msg) from exc
    return {"code": 200, "data": _project_view(store, proj)}


# ============================================================================
# 目录浏览
# ============================================================================

MAX_BROWSE_ENTRIES = 500


def _browse_entries(path: Path) -> list[dict]:
    entries = []
    try:
        for child in path.iterdir():
            try:
                if not child.is_dir():
                    continue
                stat = child.stat()
                entries.append({
                    "name": child.name,
                    "path": str(child).replace("\\", "/"),
                    "is_dir": True,
                    "has_git": (child / ".git").exists(),
                    "mtime": now_cst().fromtimestamp(stat.st_mtime).isoformat(
                        timespec="seconds"),
                })
            except (OSError, PermissionError):
                continue
    except (OSError, PermissionError) as exc:
        raise HTTPException(403, f"目录不可访问：{exc}") from exc
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return entries[:MAX_BROWSE_ENTRIES]


def _breadcrumbs(path: Path) -> list[dict]:
    parts = []
    p = path
    while True:
        parts.append({"name": p.name or str(p),
                      "path": str(p).replace("\\", "/")})
        if p.parent == p:
            break
        p = p.parent
    return list(reversed(parts))


def _suggestions() -> list[dict]:
    config = _c().config
    raw_list = config.get_raw("project", {}).get(
        "browse_default_start",
        ["~/Documents/GitHub", "~/Documents/project", "~/Desktop",
         "D:/project", "D:/workspace"]) if hasattr(config, "get_raw") else \
        ["~/Documents", "~/Desktop"]
    last = config.get_raw("project", {}).get("last_browse_parent") \
        if hasattr(config, "get_raw") else None
    if last:
        raw_list = [last, *raw_list]
    # 按 realpath 去重：~/Desktop 与 C:/Users/xxx/Desktop 是同一目录
    seen: set[str] = set()
    out = []
    for raw in raw_list:
        try:
            p = Path(os.path.expanduser(raw)).resolve()
            if not p.is_dir():
                continue
            key = str(p).replace("\\", "/").lower()
            if key in seen:
                continue
            seen.add(key)
            display = str(p).replace("\\", "/")
            out.append({"name": p.name or display, "path": display})
        except OSError:
            continue
    return out


def _drives() -> list[str]:
    if os.name != "nt":
        return ["/"]
    drives = []
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZAB":
        drive = f"{letter}:/"
        if Path(drive).exists():
            drives.append(drive)
    return drives


async def _browse_impl(path: str = ""):
    store: ProjectStore = _c().projects
    if not path:
        return {"code": 200, "data": {
            "current": None, "parents": [], "entries": [],
            "suggestions": _suggestions(), "drives": _drives(),
        }}
    try:
        expanded = os.path.expanduser(path.strip())
        resolved = Path(os.path.realpath(expanded))
    except OSError as exc:
        raise HTTPException(400, f"路径解析失败：{exc}") from exc
    if not resolved.exists():
        raise HTTPException(404, f"路径不存在：{path}")
    if not resolved.is_dir():
        raise HTTPException(400, f"不是目录：{path}")
    # 只拒真正的系统目录（Windows/Program Files 等）；磁盘根、用户目录允许浏览，
    # 用户点进去看子目录才能找到项目所在文件夹。
    from agent.projects import is_system_dir
    display = str(resolved).replace("\\", "/")
    key = display.lower()
    if is_system_dir(key):
        raise HTTPException(403, f"拒绝浏览系统目录：{display}")

    # 已加载判定：build path_key set 一次
    loaded = {p.path_key: p.id for p in store.list("all")}
    entries = _browse_entries(resolved)
    for e in entries:
        e["is_loaded"] = e["path"].lower() in loaded
        e["loaded_project_id"] = loaded.get(e["path"].lower())

    # 持久化 last_browse_parent
    try:
        config = _c().config
        raw = config.get_raw("project", {})
        raw["last_browse_parent"] = display
        config.set_raw("project", raw)
    except Exception:  # noqa: BLE001
        pass

    return {"code": 200, "data": {
        "current": display,
        "parents": _breadcrumbs(resolved),
        "entries": entries,
        "suggestions": [],
        "drives": _drives(),
    }}


async def _browse_mkdir_impl(body: MkdirRequest):
    parent = Path(os.path.expanduser(body.parent))
    if not parent.is_dir():
        raise HTTPException(404, f"父目录不存在：{body.parent}")
    target = parent / body.name
    if target.exists():
        raise HTTPException(409, f"目录已存在：{target}")
    try:
        target.mkdir(parents=False, exist_ok=False)
    except OSError as exc:
        raise HTTPException(400, f"创建失败：{exc}") from exc
    return {"code": 200, "data": {"path": str(target).replace("\\", "/")}}


# ============================================================================
# 会话内沙箱档位
# ============================================================================

@router.post("/chat/session/{session_id}/sandbox-mode")
async def set_sandbox_mode(session_id: str, body: SandboxModeRequest):
    """Switch the effective sandbox mode of this session.

    v6：沙箱下沉到会话层——项目会话与非项目会话共用同一档位表
    (read-only / workspace-write / danger-full-access)。
    """
    from tools.fs.policy import VALID_MODES
    c = _c()
    row = c.db.query_one(
        "SELECT project_id FROM sessions WHERE session_id=?", (session_id,))
    if not row:
        raise HTTPException(404, f"会话不存在：{session_id}")
    if body.mode not in VALID_MODES:
        raise HTTPException(
            400, f"非法档位：{body.mode}（合法值：{', '.join(VALID_MODES)}）")
    now = now_cst().isoformat(timespec="seconds")
    payload = json.dumps({"mode": body.mode, "reason": body.reason},
                         ensure_ascii=False)
    with c.db.transaction() as conn:
        conn.execute(
            "UPDATE sessions SET sandbox_mode=? WHERE session_id=?",
            (body.mode, session_id))
        conn.execute(
            "INSERT INTO session_policy_events(session_id, event_type, "
            "payload, created_at) VALUES(?, 'sandbox_mode_change', ?, ?)",
            (session_id, payload, now))
    return {"code": 200, "data": {"mode": body.mode, "effective_at": now}}


# ============================================================================
# 项目文件浏览（M4 §四 4.7 @文件面板 / 前端预览）
# ============================================================================

@router.get("/projects/{project_id}/tree")
async def project_tree(project_id: str, path: str = "", depth: int = 1):
    """列出项目内目录（应用忽略规则）；path 缺省为项目根。"""
    import asyncio as _aio
    c = _c()
    proj = c.projects.get(project_id)
    if not proj:
        raise HTTPException(404, f"项目不存在：{project_id}")
    if proj.status != "active":
        raise HTTPException(409, "项目已归档")
    from tools.fs.errors import FsError
    tool = c.registry.get("fs_list")
    if not tool:
        raise HTTPException(500, "fs_list 未注册")
    from tools.fs.workspace import WorkspaceContext
    from tools.fs.policy import SandboxPolicy
    # 直接构造只读 policy（不依赖具体会话），避免建临时会话副作用
    _p = Path(proj.path).resolve()
    policy = SandboxPolicy(
        mode="read-only", project_id=project_id,
        project_root=_p, writable_roots=(), read_roots=(_p,))
    ctx = WorkspaceContext.from_policy("__browse__", policy)
    target = path or str(_p)
    try:
        result = await _aio.wait_for(
            tool.run(path=target, depth=depth, _ws_ctx=ctx), timeout=10.0)
    except FsError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"code": 200, "data": result}


@router.get("/projects/{project_id}/preview")
async def project_preview(project_id: str, path: str,
                          offset: int = 1, limit: int | None = None):
    """预览项目内某个文件（分页）。仅读；不写观察记录。"""
    import asyncio as _aio
    c = _c()
    proj = c.projects.get(project_id)
    if not proj:
        raise HTTPException(404, f"项目不存在：{project_id}")
    if proj.status != "active":
        raise HTTPException(409, "项目已归档")
    from tools.fs.errors import FsError
    from tools.fs.io import read_file
    from tools.fs.resolver import guard, resolve
    _p = Path(proj.path).resolve()
    try:
        resolved = resolve(path, cwd=_p)
        guard(resolved, [_p], action="preview", raw_path=path)
        result = await _aio.wait_for(
            _aio.to_thread(read_file, resolved,
                           offset=offset, limit=limit), timeout=10.0)
    except FsError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"code": 200, "data": result}


class ProjectSearchRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    q: str = Field(..., min_length=1)
    mode: str = "glob"      # glob / grep
    glob: str | None = None
    limit: int = 100


@router.post("/projects/{project_id}/search")
async def project_search(project_id: str, body: ProjectSearchRequest):
    """基于 fs_glob / fs_grep 的项目内搜索。"""
    import asyncio as _aio
    c = _c()
    proj = c.projects.get(project_id)
    if not proj:
        raise HTTPException(404, f"项目不存在：{project_id}")
    if proj.status != "active":
        raise HTTPException(409, "项目已归档")
    from tools.fs.errors import FsError
    from tools.fs.workspace import WorkspaceContext
    from tools.fs.policy import SandboxPolicy
    _p = Path(proj.path).resolve()
    policy = SandboxPolicy(
        mode="read-only", project_id=project_id,
        project_root=_p, writable_roots=(), read_roots=(_p,))
    ctx = WorkspaceContext.from_policy("__search__", policy)
    if body.mode == "grep":
        tool = c.registry.get("fs_grep")
        try:
            result = await _aio.wait_for(
                tool.run(regex=body.q, path=str(_p), glob=body.glob,
                          _ws_ctx=ctx), timeout=15.0)
        except FsError as exc:
            raise HTTPException(400, str(exc)) from exc
        hits = result.get("hits", [])[: body.limit]
        return {"code": 200, "data": {"hits": hits}}
    # glob
    tool = c.registry.get("fs_glob")
    try:
        result = await _aio.wait_for(
            tool.run(pattern=body.q, path=str(_p), _ws_ctx=ctx),
            timeout=10.0)
    except FsError as exc:
        raise HTTPException(400, str(exc)) from exc
    matches = result.get("matches", [])[: body.limit]
    return {"code": 200, "data": {"matches": matches}}


@router.get("/chat/session/{session_id}/sandbox-mode")
async def get_sandbox_mode(session_id: str):
    """Return the effective sandbox mode for any session (project or not).

    v6：非项目会话不再固定 legacy-workspace，同样走三档解析。
    """
    from tools.fs.policy import normalize_mode, DEFAULT_MODE
    c = _c()
    row = c.db.query_one(
        "SELECT project_id, sandbox_mode FROM sessions WHERE session_id=?",
        (session_id,))
    if not row:
        raise HTTPException(404, f"会话不存在：{session_id}")
    project_id = row["project_id"]
    if row["sandbox_mode"]:
        mode = normalize_mode(row["sandbox_mode"])
        source = "session"
    elif project_id:
        proj = c.projects.get(project_id)
        mode = normalize_mode(proj.sandbox_mode) if proj else DEFAULT_MODE
        source = "project"
    else:
        mode = DEFAULT_MODE
        source = "default"
    history = c.db.query_all(
        "SELECT event_type, payload, created_at FROM session_policy_events "
        "WHERE session_id=? ORDER BY id DESC LIMIT 20", (session_id,))
    return {"code": 200, "data": {
        "mode": mode, "source": source,
        "history": [{"type": h["event_type"],
                     "payload": json.loads(h["payload"]),
                     "created_at": h["created_at"]} for h in history]}}
