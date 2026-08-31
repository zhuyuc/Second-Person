"""M1: 项目工作区端到端契约测试。

覆盖：
- migration 044 应用后表结构正确
- POST /projects 正常 / 路径不存在 / 系统目录 / 幂等
- GET /projects (active / archived / all)
- PATCH /projects/{id} 修改 title
- archive / unarchive / purge 三态流转
- purge 前必须 archived
- 归档联动会话
- 无项目 shell/sandbox 兼容行为（无项目会话 create 不带 project_id）
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.container import AppContainer
from app.main import create_app, get_container


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    async def _noop_startup(self):
        return None

    async def _noop_shutdown(self):
        return None

    monkeypatch.setattr(AppContainer, "startup", _noop_startup)
    monkeypatch.setattr(AppContainer, "shutdown", _noop_shutdown)

    app = create_app(tmp_path)
    with TestClient(app) as test_client:
        yield test_client
    get_container().db.close()


@pytest.fixture
def project_dir(tmp_path: Path):
    """项目根：一个真实存在的临时目录。"""
    p = tmp_path / "sample-project"
    p.mkdir()
    return p


# ---------------------------------------------------------------------------
# migration
# ---------------------------------------------------------------------------

def test_migration_creates_projects_tables(client: TestClient):
    """044 迁移应用后必备表 + 字段存在。"""
    db = get_container().db
    row = db.query_one("SELECT name FROM sqlite_master WHERE type='table' "
                       "AND name='projects'")
    assert row is not None
    row = db.query_one("SELECT name FROM sqlite_master WHERE type='table' "
                       "AND name='session_policy_events'")
    assert row is not None
    row = db.query_one("SELECT name FROM sqlite_master WHERE type='table' "
                       "AND name='fs_observations'")
    assert row is not None
    # sessions 表加了 project_id / archived
    cols = {r["name"] for r in db.query_all("PRAGMA table_info(sessions)")}
    assert "project_id" in cols
    assert "archived" in cols
    assert "archived_source" in cols
    assert "sandbox_mode" in cols
    # memories/raw_docs/local_dirs/图谱
    for tbl in ("memories", "raw_docs", "local_dirs", "memory_entities",
                "memory_entity_links", "memory_links"):
        cols = {r["name"] for r in db.query_all(f"PRAGMA table_info({tbl})")}
        assert "project_id" in cols, f"{tbl} 缺 project_id"
    # graph_layout 保持 entity_id 单列 PK（project_id 已通过 entity_id 哈希隔离）
    cols = {r["name"] for r in db.query_all("PRAGMA table_info(graph_layout)")}
    assert "project_id" in cols


# ---------------------------------------------------------------------------
# 项目 CRUD
# ---------------------------------------------------------------------------

def test_create_project_ok(client: TestClient, project_dir: Path):
    resp = client.post("/api/projects",
                       json={"path": str(project_dir)})
    assert resp.status_code == 200
    d = resp.json()["data"]
    assert d["id"].startswith("proj_")
    assert d["title"] == project_dir.name
    assert d["status"] == "active"
    assert d["sandbox_mode"] == "workspace-write"


def test_create_project_path_not_exist(client: TestClient, tmp_path: Path):
    resp = client.post("/api/projects",
                       json={"path": str(tmp_path / "not_exist")})
    assert resp.status_code == 400


def test_create_project_rejects_system_dir(client: TestClient):
    # 磁盘根（Windows）/ 根（POSIX）
    import os
    root = "C:/" if os.name == "nt" else "/"
    resp = client.post("/api/projects", json={"path": root})
    assert resp.status_code == 400


def test_create_project_idempotent(client: TestClient, project_dir: Path):
    a = client.post("/api/projects",
                    json={"path": str(project_dir)}).json()["data"]
    b = client.post("/api/projects",
                    json={"path": str(project_dir)}).json()["data"]
    assert a["id"] == b["id"]


def test_list_projects_default_active(client: TestClient, project_dir: Path,
                                      tmp_path: Path):
    p2 = tmp_path / "proj2"; p2.mkdir()
    client.post("/api/projects", json={"path": str(project_dir)})
    client.post("/api/projects", json={"path": str(p2)})
    lst = client.get("/api/projects").json()["data"]
    assert len(lst) == 2
    ids = {p["id"] for p in lst}
    assert len(ids) == 2


def test_patch_project_title(client: TestClient, project_dir: Path):
    pid = client.post("/api/projects",
                      json={"path": str(project_dir)}).json()["data"]["id"]
    resp = client.patch(f"/api/projects/{pid}", json={"title": "重命名后"})
    assert resp.status_code == 200
    assert resp.json()["data"]["title"] == "重命名后"


def test_create_session_with_project_id(client: TestClient, project_dir: Path):
    pid = client.post("/api/projects",
                      json={"path": str(project_dir)}).json()["data"]["id"]
    resp = client.post("/api/chat/session/create", json={"project_id": pid})
    assert resp.status_code == 200
    sid = resp.json()["data"]["session_id"]
    # session_count 反映到项目视图
    lst = client.get("/api/projects").json()["data"]
    p = [x for x in lst if x["id"] == pid][0]
    assert p["session_count"] == 1
    # 会话列表带 project_id 字段
    sessions = client.get("/api/chat/sessions?page_size=500").json()["data"]["list"]
    s = [x for x in sessions if x["session_id"] == sid][0]
    assert s["project_id"] == pid
    assert s["archived"] is False


def test_create_session_no_project_backward_compat(client: TestClient):
    """无 project_id 建会话行为等价现有版本。"""
    resp = client.post("/api/chat/session/create")
    assert resp.status_code == 200
    sid = resp.json()["data"]["session_id"]
    sessions = client.get("/api/chat/sessions?page_size=500").json()["data"]["list"]
    s = [x for x in sessions if x["session_id"] == sid][0]
    assert s["project_id"] is None


def test_create_session_project_not_found(client: TestClient):
    resp = client.post("/api/chat/session/create",
                       json={"project_id": "proj_ffffffff"})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 归档 / 恢复 / 永久删除
# ---------------------------------------------------------------------------

def test_archive_project_cascades_to_sessions(client: TestClient, project_dir: Path):
    pid = client.post("/api/projects",
                      json={"path": str(project_dir)}).json()["data"]["id"]
    sid = client.post("/api/chat/session/create",
                      json={"project_id": pid}).json()["data"]["session_id"]
    r = client.post(f"/api/projects/{pid}/archive").json()["data"]
    assert r["archived_sessions"] == 1
    # 项目不再出现在 active 列表
    active = client.get("/api/projects").json()["data"]
    assert not any(p["id"] == pid for p in active)
    # 出现在 archived 列表
    archived = client.get("/api/projects?status=archived").json()["data"]
    assert any(p["id"] == pid for p in archived)
    # 会话不再出现在 sessions 默认列表（archived=0 过滤）
    sessions = client.get("/api/chat/sessions?page_size=500").json()["data"]["list"]
    assert not any(s["session_id"] == sid for s in sessions)


def test_unarchive_restores_project_sessions(client: TestClient, project_dir: Path):
    pid = client.post("/api/projects",
                      json={"path": str(project_dir)}).json()["data"]["id"]
    client.post("/api/chat/session/create", json={"project_id": pid})
    client.post(f"/api/projects/{pid}/archive")
    r = client.post(f"/api/projects/{pid}/unarchive").json()["data"]
    assert r["restored_sessions"] == 1
    active = client.get("/api/projects").json()["data"]
    assert any(p["id"] == pid for p in active)


def test_purge_requires_archived_state(client: TestClient, project_dir: Path):
    pid = client.post("/api/projects",
                      json={"path": str(project_dir)}).json()["data"]["id"]
    resp = client.delete(f"/api/projects/{pid}")
    assert resp.status_code == 409


def test_purge_deletes_everything(client: TestClient, project_dir: Path):
    """永久删除：会话 + 记忆 + 图谱 + 知识全清；项目目录不动。"""
    db = get_container().db
    pid = client.post("/api/projects",
                      json={"path": str(project_dir)}).json()["data"]["id"]
    sid = client.post("/api/chat/session/create",
                      json={"project_id": pid}).json()["data"]["session_id"]
    # 手工插入一条项目记忆 + 图谱 + 知识 raw_doc，模拟真实数据
    db.execute(
        "INSERT INTO memories(id, title, summary, domain, confidence, "
        "lifecycle, source_type, md_path, project_id) "
        "VALUES('mem_test1', 't', 's', 'general', 'strong', 'active', "
        "'memory', 'memory/mem_test1.md', ?)", (pid,))
    db.execute(
        "INSERT INTO memory_entities(entity_id, entity_name, project_id) "
        "VALUES('ent_x', 'x', ?)", (pid,))
    db.execute(
        "INSERT INTO raw_docs(id, filename, file_path, file_size, "
        "imported_at, project_id) VALUES('doc_test1', 'a.md', "
        "'/tmp/a.md', 100, '2026-08-30T00:00:00', ?)", (pid,))

    # 必须先归档
    client.post(f"/api/projects/{pid}/archive")
    resp = client.delete(f"/api/projects/{pid}")
    assert resp.status_code == 200
    r = resp.json()["data"]
    assert r["deleted_sessions"] == 1
    assert r["deleted_memories"] == 1
    assert r["deleted_docs"] == 1

    # 全部真删
    assert not db.query_one("SELECT 1 FROM projects WHERE id=?", (pid,))
    assert not db.query_one("SELECT 1 FROM sessions WHERE session_id=?", (sid,))
    assert not db.query_one("SELECT 1 FROM memories WHERE id='mem_test1'")
    assert not db.query_one(
        "SELECT 1 FROM memory_entities WHERE entity_id='ent_x'")
    assert not db.query_one("SELECT 1 FROM raw_docs WHERE id='doc_test1'")
    # 项目目录**依然存在**（关键：永远不删用户目录）
    assert project_dir.exists()


# ---------------------------------------------------------------------------
# 目录浏览
# ---------------------------------------------------------------------------

def test_browse_empty_returns_suggestions_and_drives(client: TestClient):
    d = client.get("/api/projects/browse").json()["data"]
    assert d["current"] is None
    assert "drives" in d
    assert "suggestions" in d


def test_browse_lists_subdirectories(client: TestClient, tmp_path: Path):
    (tmp_path / "sub1").mkdir()
    (tmp_path / "sub2").mkdir()
    (tmp_path / "file.txt").write_text("ignored", encoding="utf-8")
    d = client.get(f"/api/projects/browse?path={tmp_path}").json()["data"]
    names = {e["name"] for e in d["entries"]}
    assert "sub1" in names and "sub2" in names
    assert "file.txt" not in names  # 只列目录


def test_browse_marks_loaded_projects(client: TestClient, tmp_path: Path):
    p = tmp_path / "loaded"; p.mkdir()
    client.post("/api/projects", json={"path": str(p)})
    d = client.get(f"/api/projects/browse?path={tmp_path}").json()["data"]
    loaded = [e for e in d["entries"] if e["is_loaded"]]
    assert len(loaded) == 1
    assert loaded[0]["name"] == "loaded"


def test_browse_mkdir(client: TestClient, tmp_path: Path):
    resp = client.post("/api/projects/browse/mkdir",
                       json={"parent": str(tmp_path), "name": "new-folder"})
    assert resp.status_code == 200
    assert (tmp_path / "new-folder").is_dir()


def test_browse_mkdir_rejects_illegal_name(client: TestClient, tmp_path: Path):
    resp = client.post("/api/projects/browse/mkdir",
                       json={"parent": str(tmp_path), "name": "bad<name>"})
    # pydantic 校验先命中 → 422
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 沙箱档位
# ---------------------------------------------------------------------------

def test_sandbox_mode_accepts_no_project_session(client: TestClient):
    """v6：非项目会话也能切档位（沙箱下沉到会话层）。"""
    sid = client.post("/api/chat/session/create").json()["data"]["session_id"]
    resp = client.post(f"/api/chat/session/{sid}/sandbox-mode",
                       json={"mode": "danger-full-access"})
    assert resp.status_code == 200
    assert resp.json()["data"]["mode"] == "danger-full-access"


def test_sandbox_mode_rejects_invalid_mode(client: TestClient):
    """非法档位在 pydantic 层就被拒（422），不需要走到 handler。"""
    sid = client.post("/api/chat/session/create").json()["data"]["session_id"]
    resp = client.post(f"/api/chat/session/{sid}/sandbox-mode",
                       json={"mode": "unknown-mode"})
    assert resp.status_code == 422


def test_sandbox_mode_set_get(client: TestClient, project_dir: Path):
    pid = client.post("/api/projects",
                      json={"path": str(project_dir)}).json()["data"]["id"]
    sid = client.post("/api/chat/session/create",
                      json={"project_id": pid}).json()["data"]["session_id"]
    # 默认档位来自项目
    d = client.get(f"/api/chat/session/{sid}/sandbox-mode").json()["data"]
    assert d["mode"] == "workspace-write"
    assert d["source"] == "project"
    # 切档
    resp = client.post(f"/api/chat/session/{sid}/sandbox-mode",
                       json={"mode": "read-only", "reason": "谨慎"})
    assert resp.status_code == 200
    d = client.get(f"/api/chat/session/{sid}/sandbox-mode").json()["data"]
    assert d["mode"] == "read-only"
    assert d["source"] == "session"
    assert len(d["history"]) >= 1


def test_no_project_session_defaults_workspace_write(client: TestClient):
    """v6：非项目会话默认 workspace-write（不再是 legacy-workspace 特殊档）。"""
    sid = client.post("/api/chat/session/create").json()["data"]["session_id"]
    d = client.get(f"/api/chat/session/{sid}/sandbox-mode").json()["data"]
    assert d["mode"] == "workspace-write"
    assert d["source"] == "default"
