"""M3: fs 工具族 + 沙箱四档 单元测试。

覆盖：
- fs_read 分页 + 版本乐观锁
- fs_write / fs_edit 版本冲突 (FS_STALE_VERSION)
- fs_edit 唯一/多匹配/replace_all
- 二进制文件拒（NOT_TEXT）
- 路径越界拒（SANDBOX_DENIED）
- 三档权能矩阵（read-only / workspace-write / danger-full-access）—— 项目/非项目共用
- WorkspaceResolver：项目会话 vs 无项目 vs session_policy_events 覆盖
- fs_list 应用 .gitignore
- fs_glob / fs_grep 命中
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.container import AppContainer
from app.main import create_app, get_container
from tools.fs.errors import FsError, FsErrorCode
from tools.fs.io import atomic_write, literal_edit, make_version, read_file
from tools.fs.resolver import guard, resolve


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    async def _noop(self):
        return None
    monkeypatch.setattr(AppContainer, "startup", _noop)
    monkeypatch.setattr(AppContainer, "shutdown", _noop)
    app = create_app(tmp_path)
    with TestClient(app) as tc:
        yield tc
    get_container().db.close()


@pytest.fixture
def project_dir(tmp_path: Path):
    p = tmp_path / "proj"
    p.mkdir()
    (p / "hello.py").write_text("print('hi')\nprint('bye')\n", encoding="utf-8")
    (p / "docs").mkdir()
    (p / "docs" / "README.md").write_text("# Doc\n", encoding="utf-8")
    (p / ".gitignore").write_text(".venv\nsecret.txt\n", encoding="utf-8")
    (p / ".venv").mkdir()
    (p / ".venv" / "foo.py").write_text("noop", encoding="utf-8")
    (p / "secret.txt").write_text("SECRET", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# io.py 纯函数
# ---------------------------------------------------------------------------

def test_read_file_pagination(project_dir: Path):
    result = read_file(project_dir / "hello.py")
    assert result["type"] == "file"
    assert result["total_lines"] == 2
    assert "print('hi')" in result["content"]
    assert "共 2 行" in result["content"]


def test_read_file_offset_limit(tmp_path: Path):
    p = tmp_path / "long.txt"
    p.write_text("\n".join(f"line{i}" for i in range(1, 21)), encoding="utf-8")
    result = read_file(p, offset=5, limit=3)
    assert "line5" in result["content"]
    assert "line7" in result["content"]
    assert "line8" not in result["content"]


def test_read_file_binary_rejected(tmp_path: Path):
    p = tmp_path / "bin.dat"
    p.write_bytes(b"\x00\x01\x02\x03garbage")
    with pytest.raises(FsError) as exc:
        read_file(p)
    assert exc.value.code == FsErrorCode.NOT_TEXT


def test_atomic_write_creates_and_replaces(tmp_path: Path):
    p = tmp_path / "out.txt"
    atomic_write(p, "hello")
    assert p.read_text(encoding="utf-8") == "hello"
    atomic_write(p, "world")
    assert p.read_text(encoding="utf-8") == "world"


def test_literal_edit_unique_ok(tmp_path: Path):
    p = tmp_path / "e.txt"
    p.write_text("foo bar baz", encoding="utf-8")
    n, before, after = literal_edit(p, "bar", "BAR")
    assert n == 1
    assert after == "foo BAR baz"


def test_literal_edit_ambiguous_rejects(tmp_path: Path):
    p = tmp_path / "e.txt"
    p.write_text("foo foo foo", encoding="utf-8")
    with pytest.raises(FsError) as exc:
        literal_edit(p, "foo", "bar")
    assert exc.value.code == FsErrorCode.AMBIGUOUS_EDIT


def test_literal_edit_replace_all(tmp_path: Path):
    p = tmp_path / "e.txt"
    p.write_text("foo foo foo", encoding="utf-8")
    n, _, after = literal_edit(p, "foo", "bar", replace_all=True)
    assert n == 3
    assert after == "bar bar bar"


def test_literal_edit_no_match(tmp_path: Path):
    p = tmp_path / "e.txt"
    p.write_text("hello", encoding="utf-8")
    with pytest.raises(FsError) as exc:
        literal_edit(p, "world", "!!!")
    assert exc.value.code == FsErrorCode.NO_MATCH


def test_make_version_changes_on_write(tmp_path: Path):
    p = tmp_path / "v.txt"
    p.write_text("a", encoding="utf-8")
    v1 = make_version(p)
    import time; time.sleep(0.01)
    atomic_write(p, "b")
    v2 = make_version(p)
    assert v1 != v2


# ---------------------------------------------------------------------------
# resolver.py 围栏
# ---------------------------------------------------------------------------

def test_resolve_absolute_path_ok(tmp_path: Path):
    p = tmp_path / "x.txt"
    p.write_text("_", encoding="utf-8")
    got = resolve(str(p))
    assert got == Path(p).resolve()


def test_resolve_relative_needs_cwd(tmp_path: Path):
    with pytest.raises(FsError) as exc:
        resolve("relative.txt")
    assert exc.value.code == FsErrorCode.INVALID_PATH


def test_guard_within_root(tmp_path: Path):
    (tmp_path / "child.txt").write_text("_", encoding="utf-8")
    guard(tmp_path / "child.txt", [tmp_path])


def test_guard_out_of_root_rejects(tmp_path: Path):
    (tmp_path / "child.txt").write_text("_", encoding="utf-8")
    other = tmp_path.parent
    with pytest.raises(FsError) as exc:
        guard(other, [tmp_path])
    assert exc.value.code == FsErrorCode.SANDBOX_DENIED


# ---------------------------------------------------------------------------
# 沙箱四档 + WorkspaceResolver
# ---------------------------------------------------------------------------

def test_workspace_resolver_no_project_defaults_workspace_write(
        client: TestClient):
    """v6：非项目会话默认 workspace-write（不再是 legacy-workspace 特殊档）。"""
    c = get_container()
    sid = c.sessions.create_session()
    ws = c.workspace_resolver.resolve(sid)
    assert ws.sandbox_mode == "workspace-write"
    assert ws.project_id is None
    assert ws.project_root is None
    # writable_roots 落在 data/workspace/ + 白名单
    assert len(ws.writable_roots) >= 1


def test_workspace_resolver_no_project_can_switch_to_readonly(
        client: TestClient):
    """v6：非项目会话也能切档位（不再返回 400）。"""
    c = get_container()
    sid = c.sessions.create_session()
    r = client.post(f"/api/chat/session/{sid}/sandbox-mode",
                    json={"mode": "read-only"})
    assert r.status_code == 200
    ws = c.workspace_resolver.resolve(sid)
    assert ws.sandbox_mode == "read-only"
    assert ws.writable_roots == ()


def test_workspace_resolver_no_project_can_switch_to_danger(
        client: TestClient):
    """v6：非项目会话可开 danger-full-access，风险自负。"""
    c = get_container()
    sid = c.sessions.create_session()
    r = client.post(f"/api/chat/session/{sid}/sandbox-mode",
                    json={"mode": "danger-full-access"})
    assert r.status_code == 200
    ws = c.workspace_resolver.resolve(sid)
    assert ws.sandbox_mode == "danger-full-access"
    assert ws.shell_enabled is True
    assert ws.shell_cwd is not None


def test_legacy_workspace_mode_normalizes_to_workspace_write(
        client: TestClient):
    """老 legacy-workspace 模式值应归一到 workspace-write（migration 兜底）。"""
    c = get_container()
    sid = c.sessions.create_session()
    # 直接写老档位到 sessions.sandbox_mode（模拟未迁移的老数据）
    c.db.execute("UPDATE sessions SET sandbox_mode='legacy-workspace' "
                  "WHERE session_id=?", (sid,))
    ws = c.workspace_resolver.resolve(sid)
    assert ws.sandbox_mode == "workspace-write"


def test_workspace_resolver_project_default_workspace_write(
        client: TestClient, project_dir: Path):
    pid = client.post("/api/projects",
                      json={"path": str(project_dir)}).json()["data"]["id"]
    sid = client.post("/api/chat/session/create",
                      json={"project_id": pid}).json()["data"]["session_id"]
    c = get_container()
    ws = c.workspace_resolver.resolve(sid)
    assert ws.sandbox_mode == "workspace-write"
    assert ws.project_id == pid
    assert ws.project_root == Path(project_dir).resolve()


def test_workspace_resolver_session_event_overrides(
        client: TestClient, project_dir: Path):
    pid = client.post("/api/projects",
                      json={"path": str(project_dir)}).json()["data"]["id"]
    sid = client.post("/api/chat/session/create",
                      json={"project_id": pid}).json()["data"]["session_id"]
    client.post(f"/api/chat/session/{sid}/sandbox-mode",
                json={"mode": "read-only"})
    c = get_container()
    ws = c.workspace_resolver.resolve(sid)
    assert ws.sandbox_mode == "read-only"


def test_workspace_resolver_archived_project_degrades_readonly(
        client: TestClient, project_dir: Path):
    pid = client.post("/api/projects",
                      json={"path": str(project_dir)}).json()["data"]["id"]
    sid = client.post("/api/chat/session/create",
                      json={"project_id": pid}).json()["data"]["session_id"]
    client.post(f"/api/projects/{pid}/archive")
    c = get_container()
    ws = c.workspace_resolver.resolve(sid)
    assert ws.sandbox_mode == "read-only"


# ---------------------------------------------------------------------------
# fs 工具端到端
# ---------------------------------------------------------------------------

def _fs_read(c, sid, path, **kw):
    tool = c.registry.get("fs_read")
    ctx = c.workspace_resolver.resolve(sid)
    return asyncio.run(tool.run(path=path, _ws_ctx=ctx, **kw))


def _fs_write(c, sid, path, content, **kw):
    tool = c.registry.get("fs_write")
    ctx = c.workspace_resolver.resolve(sid)
    return asyncio.run(tool.run(path=path, content=content, _ws_ctx=ctx, **kw))


def _fs_edit(c, sid, path, old, new, **kw):
    tool = c.registry.get("fs_edit")
    ctx = c.workspace_resolver.resolve(sid)
    return asyncio.run(tool.run(path=path, old_string=old, new_string=new,
                                 _ws_ctx=ctx, **kw))


def test_fs_read_success_records_observation(
        client: TestClient, project_dir: Path):
    pid = client.post("/api/projects",
                      json={"path": str(project_dir)}).json()["data"]["id"]
    sid = client.post("/api/chat/session/create",
                      json={"project_id": pid}).json()["data"]["session_id"]
    c = get_container()
    result = _fs_read(c, sid, str(project_dir / "hello.py"))
    assert "print('hi')" in result["content"]
    version = c.fs_observations.get(sid, str(project_dir / "hello.py").replace("\\", "\\"))
    # 允许 realpath 差异；用工具返回的 path 精确校验
    obs = c.fs_observations.get(sid, result["path"])
    assert obs == result["version"]


def test_fs_write_creates_new_file(
        client: TestClient, project_dir: Path):
    pid = client.post("/api/projects",
                      json={"path": str(project_dir)}).json()["data"]["id"]
    sid = client.post("/api/chat/session/create",
                      json={"project_id": pid}).json()["data"]["session_id"]
    c = get_container()
    r = _fs_write(c, sid, str(project_dir / "new.txt"), "hello")
    assert r["action"] == "created"
    assert (project_dir / "new.txt").read_text(encoding="utf-8") == "hello"


def test_fs_write_out_of_project_denied(
        client: TestClient, project_dir: Path, tmp_path: Path):
    outside = tmp_path / "outside.txt"
    pid = client.post("/api/projects",
                      json={"path": str(project_dir)}).json()["data"]["id"]
    sid = client.post("/api/chat/session/create",
                      json={"project_id": pid}).json()["data"]["session_id"]
    c = get_container()
    with pytest.raises(FsError) as exc:
        _fs_write(c, sid, str(outside), "leak")
    assert exc.value.code == FsErrorCode.SANDBOX_DENIED


def test_fs_write_readonly_mode_denied(
        client: TestClient, project_dir: Path):
    pid = client.post("/api/projects",
                      json={"path": str(project_dir)}).json()["data"]["id"]
    sid = client.post("/api/chat/session/create",
                      json={"project_id": pid}).json()["data"]["session_id"]
    client.post(f"/api/chat/session/{sid}/sandbox-mode",
                json={"mode": "read-only"})
    c = get_container()
    with pytest.raises(FsError) as exc:
        _fs_write(c, sid, str(project_dir / "x.txt"), "no")
    assert exc.value.code == FsErrorCode.SANDBOX_DENIED


def test_fs_edit_requires_prior_read_in_project(
        client: TestClient, project_dir: Path):
    """workspace-write 档下：edit 前未 read → FS_NOT_OBSERVED。"""
    pid = client.post("/api/projects",
                      json={"path": str(project_dir)}).json()["data"]["id"]
    sid = client.post("/api/chat/session/create",
                      json={"project_id": pid}).json()["data"]["session_id"]
    c = get_container()
    with pytest.raises(FsError) as exc:
        _fs_edit(c, sid, str(project_dir / "hello.py"), "hi", "HI")
    assert exc.value.code == FsErrorCode.NOT_OBSERVED


def test_fs_edit_after_read_success(
        client: TestClient, project_dir: Path):
    pid = client.post("/api/projects",
                      json={"path": str(project_dir)}).json()["data"]["id"]
    sid = client.post("/api/chat/session/create",
                      json={"project_id": pid}).json()["data"]["session_id"]
    c = get_container()
    _fs_read(c, sid, str(project_dir / "hello.py"))
    r = _fs_edit(c, sid, str(project_dir / "hello.py"), "hi", "HI")
    assert r["replacements"] == 1
    assert (project_dir / "hello.py").read_text(
        encoding="utf-8").startswith("print('HI')")


def test_fs_write_stale_version_after_external_modify(
        client: TestClient, project_dir: Path):
    """read → 外部修改 → write 时 FS_STALE_VERSION。"""
    pid = client.post("/api/projects",
                      json={"path": str(project_dir)}).json()["data"]["id"]
    sid = client.post("/api/chat/session/create",
                      json={"project_id": pid}).json()["data"]["session_id"]
    c = get_container()
    target = project_dir / "hello.py"
    _fs_read(c, sid, str(target))
    import time; time.sleep(0.01)
    # 外部改
    target.write_text("EXTERNAL", encoding="utf-8")
    with pytest.raises(FsError) as exc:
        _fs_write(c, sid, str(target), "OVERRIDE")
    assert exc.value.code == FsErrorCode.STALE_VERSION


def test_fs_list_respects_gitignore(
        client: TestClient, project_dir: Path):
    pid = client.post("/api/projects",
                      json={"path": str(project_dir)}).json()["data"]["id"]
    sid = client.post("/api/chat/session/create",
                      json={"project_id": pid}).json()["data"]["session_id"]
    c = get_container()
    tool = c.registry.get("fs_list")
    ctx = c.workspace_resolver.resolve(sid)
    r = asyncio.run(tool.run(path=str(project_dir), depth=2, _ws_ctx=ctx))
    names = {e["name"] for e in r["entries"]}
    assert "hello.py" in names
    assert ".venv" not in names   # 默认忽略
    assert "secret.txt" not in names   # 项目 .gitignore


def test_fs_glob_finds_python_files(
        client: TestClient, project_dir: Path):
    pid = client.post("/api/projects",
                      json={"path": str(project_dir)}).json()["data"]["id"]
    sid = client.post("/api/chat/session/create",
                      json={"project_id": pid}).json()["data"]["session_id"]
    c = get_container()
    tool = c.registry.get("fs_glob")
    ctx = c.workspace_resolver.resolve(sid)
    r = asyncio.run(tool.run(pattern="**/*.py", path=str(project_dir), _ws_ctx=ctx))
    files = {Path(m).name for m in r["matches"]}
    assert "hello.py" in files


# ---------------------------------------------------------------------------
# Prompt 注入 + 工具注册
# ---------------------------------------------------------------------------

def test_fs_tools_registered(client: TestClient):
    c = get_container()
    for name in ("fs_read", "fs_read_image", "fs_list", "fs_glob",
                  "fs_grep", "fs_write", "fs_edit"):
        assert c.registry.has(name), f"{name} 未注册"
        assert c.registry.get(name).spec.needs_workspace is True


def test_old_file_tools_still_registered(client: TestClient):
    """M3 §6.6：file_read/file_write/shell_exec 保留兼容。"""
    c = get_container()
    for name in ("file_read", "file_write", "shell_exec"):
        assert c.registry.has(name), f"{name} 应保留"
