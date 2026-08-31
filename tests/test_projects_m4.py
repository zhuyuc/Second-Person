"""M4: 项目浏览 API + 目录丢失检测 + 手动会话归档 测试。"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.container import AppContainer
from app.main import create_app, get_container


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
    (p / "hello.py").write_text("print('hi')\n", encoding="utf-8")
    (p / "docs").mkdir()
    (p / "docs" / "README.md").write_text("# doc\n", encoding="utf-8")
    (p / ".gitignore").write_text(".venv\n", encoding="utf-8")
    (p / ".venv").mkdir()
    (p / ".venv" / "foo").write_text("_", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# tree / preview / search API
# ---------------------------------------------------------------------------

def test_project_tree_returns_entries(
        client: TestClient, project_dir: Path):
    pid = client.post("/api/projects",
                      json={"path": str(project_dir)}).json()["data"]["id"]
    d = client.get(f"/api/projects/{pid}/tree").json()["data"]
    names = {e["name"] for e in d["entries"]}
    assert "hello.py" in names
    assert "docs" in names
    assert ".venv" not in names   # 默认 + gitignore 过滤


def test_project_tree_404_on_unknown_project(client: TestClient):
    r = client.get("/api/projects/proj_ffffffff/tree")
    assert r.status_code == 404


def test_project_preview_reads_file(
        client: TestClient, project_dir: Path):
    pid = client.post("/api/projects",
                      json={"path": str(project_dir)}).json()["data"]["id"]
    d = client.get(f"/api/projects/{pid}/preview?path=hello.py").json()["data"]
    assert d["type"] == "file"
    assert "print('hi')" in d["content"]


def test_project_preview_rejects_outside_project(
        client: TestClient, project_dir: Path, tmp_path: Path):
    pid = client.post("/api/projects",
                      json={"path": str(project_dir)}).json()["data"]["id"]
    outside = tmp_path / "outside.txt"
    outside.write_text("leak", encoding="utf-8")
    r = client.get(f"/api/projects/{pid}/preview?path={outside}")
    assert r.status_code == 400


def test_project_search_glob(
        client: TestClient, project_dir: Path):
    pid = client.post("/api/projects",
                      json={"path": str(project_dir)}).json()["data"]["id"]
    d = client.post(f"/api/projects/{pid}/search",
                    json={"q": "**/*.py", "mode": "glob"}).json()["data"]
    files = {Path(m).name for m in d["matches"]}
    assert "hello.py" in files


def test_project_search_grep(
        client: TestClient, project_dir: Path):
    pid = client.post("/api/projects",
                      json={"path": str(project_dir)}).json()["data"]["id"]
    d = client.post(f"/api/projects/{pid}/search",
                    json={"q": "print", "mode": "grep"}).json()["data"]
    assert any("hello.py" in h["file"] for h in d["hits"])


def test_project_tree_rejects_archived(
        client: TestClient, project_dir: Path):
    pid = client.post("/api/projects",
                      json={"path": str(project_dir)}).json()["data"]["id"]
    client.post(f"/api/projects/{pid}/archive")
    r = client.get(f"/api/projects/{pid}/tree")
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# 目录丢失检测
# ---------------------------------------------------------------------------

def test_scan_missing_dirs_returns_missing_only(
        client: TestClient, tmp_path: Path):
    good = tmp_path / "good"; good.mkdir()
    gone = tmp_path / "gone"; gone.mkdir()
    a = client.post("/api/projects", json={"path": str(good)}).json()["data"]
    b = client.post("/api/projects", json={"path": str(gone)}).json()["data"]
    gone.rmdir()
    c = get_container()
    missing = c.projects.scan_missing_dirs()
    assert b["id"] in missing
    assert a["id"] not in missing


def test_project_list_reflects_path_missing(
        client: TestClient, tmp_path: Path):
    p = tmp_path / "vanishing"; p.mkdir()
    pid = client.post("/api/projects", json={"path": str(p)}).json()["data"]["id"]
    p.rmdir()
    lst = client.get("/api/projects").json()["data"]
    entry = [x for x in lst if x["id"] == pid][0]
    assert entry["path_missing"] is True


# ---------------------------------------------------------------------------
# 手动会话归档
# ---------------------------------------------------------------------------

def test_manual_archive_session(
        client: TestClient):
    sid = client.post("/api/chat/session/create").json()["data"]["session_id"]
    r = client.post("/api/chat/session/archive",
                    json={"session_id": sid})
    assert r.status_code == 200
    sessions = client.get("/api/chat/sessions?page_size=500").json()["data"]["list"]
    assert not any(s["session_id"] == sid for s in sessions)


def test_manual_archive_survives_project_unarchive(
        client: TestClient, project_dir: Path):
    """项目归档→恢复：只恢复联动归档；用户手动归档的会话保持归档。"""
    pid = client.post("/api/projects",
                      json={"path": str(project_dir)}).json()["data"]["id"]
    sid1 = client.post("/api/chat/session/create",
                       json={"project_id": pid}).json()["data"]["session_id"]
    sid2 = client.post("/api/chat/session/create",
                       json={"project_id": pid}).json()["data"]["session_id"]
    # 手动归档 sid1
    client.post("/api/chat/session/archive", json={"session_id": sid1})
    # 项目归档 → sid2 联动 archived_source='project'
    client.post(f"/api/projects/{pid}/archive")
    client.post(f"/api/projects/{pid}/unarchive")
    sessions = client.get("/api/chat/sessions?page_size=500").json()["data"]["list"]
    ids = {s["session_id"] for s in sessions}
    assert sid2 in ids           # 联动恢复
    assert sid1 not in ids       # 手动归档保持
