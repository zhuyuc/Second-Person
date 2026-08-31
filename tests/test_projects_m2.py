"""M2: 记忆 / 知识 / 图谱 严格项目隔离测试。

覆盖：
- Retriever._score_candidates 按 project_id 硬过滤（本项目 + 全局 / 仅全局 / 排除归档）
- Distiller.resolve_memory_project_id 归属规则（全局 domain / scope 覆盖 / 跟随会话）
- entity_id 项目化：同名不同项目 → 不同 entity_id
- Memory API 按 project_id 过滤（/memory/list, /memory/graph）
- AgentCore 传 project_id 给 Retriever
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.container import AppContainer
from app.main import create_app, get_container
from memory.distiller import GLOBAL_DOMAINS, resolve_memory_project_id
from memory.naming import entity_id


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    async def _noop_startup(self):
        return None

    async def _noop_shutdown(self):
        return None

    monkeypatch.setattr(AppContainer, "startup", _noop_startup)
    monkeypatch.setattr(AppContainer, "shutdown", _noop_shutdown)

    app = create_app(tmp_path)
    with TestClient(app) as tc:
        yield tc
    get_container().db.close()


@pytest.fixture
def project_dir(tmp_path: Path):
    p = tmp_path / "proj"
    p.mkdir()
    return p


def _seed_memory(db, mid, project_id, domain="general", lifecycle="active"):
    """在测试数据库中直接插入一条记忆（跳过 Distiller 复杂链路）。"""
    db.execute(
        "INSERT INTO memories(id, title, summary, domain, confidence, "
        "lifecycle, source_type, md_path, project_id) "
        "VALUES(?, ?, ?, ?, 'strong', ?, 'memory', ?, ?)",
        (mid, f"记忆-{mid}", f"summary-{mid}", domain, lifecycle,
         f"memory/{mid}.md", project_id))
    db.execute(
        "INSERT INTO memories_fts(memory_id, project_id, title, summary, detail, domain) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        (mid, project_id, f"记忆-{mid}", f"summary-{mid}", "", domain))


# ---------------------------------------------------------------------------
# Distiller 归属规则（纯函数，无需 fixture）
# ---------------------------------------------------------------------------

def test_distiller_global_domain_forces_null_project():
    for domain in GLOBAL_DOMAINS:
        assert resolve_memory_project_id(
            {"domain": domain}, "proj_a1b2c3d4") is None


def test_distiller_technical_domain_follows_session_project():
    assert resolve_memory_project_id(
        {"domain": "technical"}, "proj_a1b2c3d4") == "proj_a1b2c3d4"


def test_distiller_no_session_project_defaults_global():
    assert resolve_memory_project_id({"domain": "technical"}, None) is None


def test_distiller_explicit_scope_global_overrides_domain():
    assert resolve_memory_project_id(
        {"domain": "technical", "scope": "global"}, "proj_x") is None


def test_distiller_explicit_scope_project_overrides_domain_whitelist():
    assert resolve_memory_project_id(
        {"domain": "preference", "scope": "project"}, "proj_x") == "proj_x"


# ---------------------------------------------------------------------------
# entity_id 项目化
# ---------------------------------------------------------------------------

def test_entity_id_same_name_different_projects_distinct():
    a = entity_id("李四", project_id="proj_a1")
    b = entity_id("李四", project_id="proj_b2")
    assert a != b


def test_entity_id_no_project_backward_compat():
    """无 project_id 时行为与旧版本字节一致（对全局实体历史 id 稳定）。"""
    global_id = entity_id("李四")
    global_id_v2 = entity_id("李四", project_id=None)
    assert global_id == global_id_v2


def test_entity_id_disambiguator_still_works():
    a = entity_id("张三", disambiguator="客户")
    b = entity_id("张三", disambiguator="同事")
    assert a != b


# ---------------------------------------------------------------------------
# Retriever 硬过滤
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retriever_isolates_project_memories(
        client: TestClient, project_dir: Path, tmp_path: Path):
    """项目 A 会话检索时，只见项目 A + 全局，不见项目 B。"""
    p2 = tmp_path / "proj_b"; p2.mkdir()
    a = client.post("/api/projects",
                    json={"path": str(project_dir)}).json()["data"]
    b = client.post("/api/projects",
                    json={"path": str(p2)}).json()["data"]
    c = get_container()

    _seed_memory(c.db, "mem_a_only", a["id"])
    _seed_memory(c.db, "mem_b_only", b["id"])
    _seed_memory(c.db, "mem_global", None)

    # 用 hybrid_presearch 直接测（走 FTS 匹配 title/summary）
    pre = await c.retriever.hybrid_presearch(
        "记忆", project_id=a["id"])
    ids = {cand.memory_id for cand in pre.candidates}
    assert "mem_a_only" in ids
    assert "mem_global" in ids
    assert "mem_b_only" not in ids


@pytest.mark.asyncio
async def test_retriever_no_project_sees_only_global(
        client: TestClient, project_dir: Path):
    a = client.post("/api/projects",
                    json={"path": str(project_dir)}).json()["data"]
    c = get_container()
    _seed_memory(c.db, "mem_a", a["id"])
    _seed_memory(c.db, "mem_global", None)

    pre = await c.retriever.hybrid_presearch("记忆", project_id=None)
    ids = {cand.memory_id for cand in pre.candidates}
    assert "mem_global" in ids
    assert "mem_a" not in ids


@pytest.mark.asyncio
async def test_retriever_excludes_archived_project_memories(
        client: TestClient, project_dir: Path):
    """归档项目的记忆一律不出现在检索里（即使处于本项目会话中）。"""
    a = client.post("/api/projects",
                    json={"path": str(project_dir)}).json()["data"]
    c = get_container()
    _seed_memory(c.db, "mem_a", a["id"])
    client.post(f"/api/projects/{a['id']}/archive")

    # 归档后即使传 project_id=a 也检索不到
    pre = await c.retriever.hybrid_presearch("记忆", project_id=a["id"])
    ids = {cand.memory_id for cand in pre.candidates}
    assert "mem_a" not in ids


# ---------------------------------------------------------------------------
# Memory API 过滤
# ---------------------------------------------------------------------------

def test_memory_list_filters_by_project(
        client: TestClient, project_dir: Path, tmp_path: Path):
    p2 = tmp_path / "b"; p2.mkdir()
    a = client.post("/api/projects",
                    json={"path": str(project_dir)}).json()["data"]
    b = client.post("/api/projects",
                    json={"path": str(p2)}).json()["data"]
    c = get_container()
    _seed_memory(c.db, "mem_a1", a["id"])
    _seed_memory(c.db, "mem_b1", b["id"])
    _seed_memory(c.db, "mem_g1", None)

    # 缺省 → 全部
    r = client.post("/api/memory/list", json={}).json()["data"]
    ids = {m["id"] for m in r["list"]}
    assert {"mem_a1", "mem_b1", "mem_g1"} <= ids

    # global 只 → 只全局
    r = client.post("/api/memory/list",
                    json={"project_id": "global"}).json()["data"]
    ids = {m["id"] for m in r["list"]}
    assert "mem_g1" in ids
    assert "mem_a1" not in ids and "mem_b1" not in ids

    # 项目 A + with_global 默认 → A + 全局
    r = client.post("/api/memory/list", json={
        "project_id": a["id"]}).json()["data"]
    ids = {m["id"] for m in r["list"]}
    assert {"mem_a1", "mem_g1"} <= ids
    assert "mem_b1" not in ids

    # 项目 A + only → 仅 A
    r = client.post("/api/memory/list", json={
        "project_id": a["id"], "project_scope": "only"}).json()["data"]
    ids = {m["id"] for m in r["list"]}
    assert "mem_a1" in ids
    assert "mem_b1" not in ids and "mem_g1" not in ids


def test_graph_endpoint_filters_by_project(
        client: TestClient, project_dir: Path):
    a = client.post("/api/projects",
                    json={"path": str(project_dir)}).json()["data"]
    c = get_container()
    c.db.execute(
        "INSERT INTO memory_entities(entity_id, entity_name, project_id) "
        "VALUES('ent_proj_a1', '张三', ?)", (a["id"],))
    c.db.execute(
        "INSERT INTO memory_entities(entity_id, entity_name, project_id) "
        "VALUES('ent_global_1', '全局实体', NULL)")

    # 缺省 → 全部
    r = client.get("/api/memory/graph").json()["data"]
    ids = {n["entity_id"] for n in r["nodes"]}
    assert "ent_proj_a1" in ids and "ent_global_1" in ids

    # 仅全局
    r = client.get("/api/memory/graph?project_id=global").json()["data"]
    ids = {n["entity_id"] for n in r["nodes"]}
    assert "ent_global_1" in ids
    assert "ent_proj_a1" not in ids

    # 项目 A + only
    r = client.get(
        f"/api/memory/graph?project_id={a['id']}&project_scope=only"
    ).json()["data"]
    ids = {n["entity_id"] for n in r["nodes"]}
    assert "ent_proj_a1" in ids
    assert "ent_global_1" not in ids


# ---------------------------------------------------------------------------
# AgentCore 传 project_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_agent_core_passes_session_project_id(
        client: TestClient, project_dir: Path):
    """会话所在项目会传入 Retriever.retrieve 的 project_id 参数。"""
    a = client.post("/api/projects",
                    json={"path": str(project_dir)}).json()["data"]
    sid = client.post("/api/chat/session/create",
                      json={"project_id": a["id"]}).json()["data"]["session_id"]
    c = get_container()

    captured = {}

    async def fake_retrieve(query, *, session_id=None, context_text=None, project_id=None):
        captured["project_id"] = project_id
        captured["session_id"] = session_id
        from memory.retriever import RetrievalResult
        return RetrievalResult()

    # 桩：单元测试不接真实模型；snapshot_for/load_recovery_context 全部 stub
    c.retriever.retrieve = fake_retrieve
    c.providers.snapshot_for = lambda _role: {"model_id": "test", "context_window": 4096}
    c.sessions.load_recovery_context = lambda _sid: []
    await c.core._runtime_context(
        session_id=sid, turn_id="turn_x", message="测试", onboarding=False)
    assert captured["project_id"] == a["id"]
    assert captured["session_id"] == sid
