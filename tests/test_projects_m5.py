"""M5：backup / --rebuild-index / IM adapter 兼容 测试。"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.container import AppContainer
from app.main import create_app, get_container
from memory.recovery import rebuild_projects_from_md


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
    return p


# ---------------------------------------------------------------------------
# M5-2 Backup 包含项目 md
# ---------------------------------------------------------------------------

def test_backup_includes_project_md(
        client: TestClient, project_dir: Path, tmp_path: Path):
    pid = client.post("/api/projects",
                      json={"path": str(project_dir)}).json()["data"]["id"]
    c = get_container()
    md_path = c.projects.data_dir / "projects" / f"{pid}.md"
    assert md_path.exists()
    import asyncio
    result = asyncio.run(c.backup.create(label="m5_test"))
    zpath = Path(c.backup.backups_dir) / result["filename"]
    assert zpath.exists()
    with zipfile.ZipFile(zpath, "r") as z:
        names = z.namelist()
    assert any(n.endswith(f"projects/{pid}.md") for n in names), \
        f"备份包内未见 projects md：{names}"


# ---------------------------------------------------------------------------
# M5-3 --rebuild-index 从 md 重建 projects
# ---------------------------------------------------------------------------

def test_rebuild_projects_from_md_restores_table(
        client: TestClient, project_dir: Path):
    pid = client.post("/api/projects",
                      json={"path": str(project_dir)}).json()["data"]["id"]
    c = get_container()
    # 手工清空 projects 表模拟磁盘 md 保留但 DB 丢失的场景
    c.db.execute("DELETE FROM projects")
    assert c.db.query_one("SELECT COUNT(*) c FROM projects")["c"] == 0
    n = rebuild_projects_from_md(c.db, c.projects.data_dir)
    assert n == 1
    row = c.db.query_one("SELECT id, path FROM projects WHERE id=?", (pid,))
    assert row is not None
    assert row["id"] == pid


def test_rebuild_index_returns_projects_rebuilt(
        client: TestClient, project_dir: Path):
    """rebuild_index 返回值含 projects_rebuilt。"""
    from memory.recovery import rebuild_index
    pid = client.post("/api/projects",
                      json={"path": str(project_dir)}).json()["data"]["id"]
    c = get_container()
    c.db.execute("DELETE FROM projects")
    result = rebuild_index(c.db, c.projects.data_dir)
    assert result.get("projects_rebuilt") == 1
    assert c.db.query_one("SELECT 1 FROM projects WHERE id=?", (pid,))


def test_rebuild_projects_handles_empty_dir(
        client: TestClient, tmp_path: Path):
    c = get_container()
    empty = tmp_path / "empty_data"
    empty.mkdir()
    n = rebuild_projects_from_md(c.db, empty)
    assert n == 0


# ---------------------------------------------------------------------------
# M5-4 IM adapter 归档兼容
# ---------------------------------------------------------------------------

def test_im_resolve_session_skips_archived(client: TestClient):
    """已归档的 platform_sessions 映射 → adapter 新建替代会话，更新映射。"""
    from gateway.platforms.base import BasePlatformAdapter
    c = get_container()
    # 先手动建两个 IM 会话映射
    sid1 = c.sessions.create_session(channel="feishu")
    c.db.execute(
        "INSERT INTO platform_sessions(platform, platform_user_id, session_id, created_at) "
        "VALUES('feishu','user_abc',?, '2026-08-30T00:00:00')", (sid1,))
    # 归档 sid1
    c.sessions.archive_session(sid1, source="manual")
    # 构造最小 adapter 桩来复用 _resolve_session
    stub = BasePlatformAdapter.__new__(BasePlatformAdapter)
    stub.db = c.db
    stub.sessions = c.sessions
    stub.platform_type = "feishu"
    stub.platform_id = "web_default"  # 借用 seed row
    stub.notifier = lambda *a, **k: None
    sid2 = stub._resolve_session("user_abc")
    assert sid2 != sid1  # 新建替代
    # 映射被更新到新会话
    row = c.db.query_one(
        "SELECT session_id FROM platform_sessions WHERE platform='feishu' "
        "AND platform_user_id='user_abc'")
    assert row["session_id"] == sid2
