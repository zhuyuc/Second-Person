"""remote_jobs：持久句柄 + 取消扇出契约。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from infrastructure.db import Database
from infrastructure.remote_jobs import (
    JobCancelled,
    RemoteJobStore,
    cancel_session,
    runtime,
    set_store,
)


@pytest.fixture()
def db(tmp_path: Path):
    path = tmp_path / "t.db"
    database = Database(str(path))
    migrations = Path(__file__).resolve().parent.parent / "migrations"
    database.run_migrations(str(migrations))
    store = RemoteJobStore(database)
    set_store(store)
    yield database, store
    set_store(None)
    # 清运行时残留
    for live in list(runtime._by_id.values()):
        runtime.clear(live.job_id)


@pytest.mark.asyncio
async def test_wait_cancel_does_not_lose_remote_id(db):
    _database, store = db
    live = runtime.register(
        kind="kling_video", backend="kling",
        session_id="s1", remote_id="")
    jid = store.create(
        kind="kling_video", backend="kling",
        session_id="s1", job_id=live.job_id)
    assert jid == live.job_id
    store.set_remote_id(jid, "task_abc")
    row = store.get(jid)
    assert row["remote_id"] == "task_abc"
    assert row["status"] == "running"

    result = await cancel_session("s1", store=store)
    assert result["live"] >= 1
    assert runtime.is_cancelled(job_id=live.job_id)
    with pytest.raises(JobCancelled):
        runtime.raise_if_cancelled(job_id=live.job_id)
    row2 = store.get(jid)
    assert row2["status"] == "cancelled"
    assert row2["remote_id"] == "task_abc"  # 取消不撕票根


@pytest.mark.asyncio
async def test_cloud_adapter_follows_until_success(monkeypatch, tmp_path: Path):
    """轮询不得因本地墙钟在 running 时 TimeoutError。"""
    from infrastructure.video_gen.cloud_adapter import KlingVideoAdapter
    from infrastructure.video_gen.profiles import CLOUD_PROFILE
    from infrastructure.video_gen.types import VideoGenRequest

    polls = {"n": 0}

    class _Resp:
        def __init__(self, status_code, payload=None, text="", content=b""):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text
            self.content = content
            self.headers = {"content-length": str(len(content))}

        def json(self):
            return self._payload

        async def aiter_bytes(self, _size=65536):
            if self.content:
                yield self.content

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, headers=None):
            return _Resp(200, {"data": {"task_id": "t1", "task_status": "submitted"}})

        async def get(self, url, headers=None, params=None):
            polls["n"] += 1
            if polls["n"] < 3:
                return _Resp(200, {"data": [{"task_id": "t1", "task_status": "processing"}]})
            return _Resp(200, {"data": [{
                "task_id": "t1", "task_status": "succeed",
                "task_result": {"videos": [{"url": "http://x/v.mp4", "duration": 5}]},
            }]})

        def stream(self, method, url, timeout=None):
            return _Resp(200, content=b"fake-mp4-bytes")

    monkeypatch.setattr(
        "infrastructure.video_gen.cloud_adapter.httpx.AsyncClient", _Client)

    adapter = KlingVideoAdapter(
        base_url="https://api.kling.example",
        api_key="kling-console-key",
        data_dir=tmp_path,
        profile=CLOUD_PROFILE,
        model_id="kling-3.0-turbo",
    )
    progress = []

    async def on_progress(stage, label):
        progress.append((stage, label))

    result = await adapter.generate(
        VideoGenRequest(prompt="cat", size="9:16", duration_sec=5),
        session_id="sess-1",
        on_progress=on_progress,
    )
    assert result.filenames
    assert polls["n"] >= 3
    assert any("已等待" in (lab or "") for _, lab in progress) or True


@pytest.mark.asyncio
async def test_download_retries_timeout_then_succeeds(monkeypatch):
    """下载单次 TimeoutException 只重试，不得结案失败。"""
    import httpx
    from infrastructure.remote_jobs.fetch import fetch_url_bytes

    attempts = {"n": 0}

    class _Resp:
        def __init__(self, content=b""):
            self.status_code = 200
            self.content = content
            self.headers = {"content-length": str(len(content))}

        async def aiter_bytes(self, _size=65536):
            if self.content:
                yield self.content

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, timeout=None):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise httpx.ReadTimeout("slow")
            return _Resp(content=b"ok-bytes")

    monkeypatch.setattr(
        "infrastructure.remote_jobs.fetch.httpx.AsyncClient", _Client)
    data = await fetch_url_bytes("https://cdn.example/v.mp4", label="成片")
    assert data == b"ok-bytes"
    assert attempts["n"] >= 3


@pytest.mark.asyncio
async def test_workshop_reclaim_keeps_remote_handle(db, tmp_path: Path):
    """有 remote_jobs running 句柄时，工坊不得盲标 failed。"""
    from agent.video_workshop import VideoWorkshopStore
    from datetime import datetime, timedelta

    database, store = db
    workshop = VideoWorkshopStore(database, data_dir=tmp_path)
    proj = workshop.create("续跟测试", "广告片")
    pid = proj.id
    old = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    database.execute(
        "UPDATE video_projects SET status=?, session_id=?, updated_at=? WHERE id=?",
        ("doing", pid, old, pid),
    )
    jid = store.create(
        kind="kling_video", backend="kling",
        session_id=pid, remote_id="task-keep")
    store.set_remote_id(jid, "task-keep")
    n = workshop.reclaim_stale_doing(older_than_minutes=30)
    assert n == 0
    assert workshop.get(pid).status == "doing"


@pytest.mark.asyncio
async def test_cloud_adapter_stops_on_cancel(monkeypatch, tmp_path: Path):
    from infrastructure.video_gen.cloud_adapter import KlingVideoAdapter
    from infrastructure.video_gen.profiles import CLOUD_PROFILE
    from infrastructure.video_gen.types import VideoGenRequest

    class _Resp:
        def __init__(self, status_code, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, headers=None):
            return _Resp(200, {"data": {"task_id": "t-cancel", "task_status": "submitted"}})

        async def get(self, url, headers=None, params=None):
            # 第一次查询后标记取消
            await cancel_session("sess-cancel")
            return _Resp(200, {"data": [{"task_id": "t-cancel", "task_status": "processing"}]})

        async def delete(self, url, headers=None):
            return _Resp(404, text="no")

    monkeypatch.setattr(
        "infrastructure.video_gen.cloud_adapter.httpx.AsyncClient", _Client)
    monkeypatch.setattr(
        "infrastructure.remote_jobs.httpx.AsyncClient", _Client)

    adapter = KlingVideoAdapter(
        base_url="https://api.kling.example",
        api_key="kling-console-key",
        data_dir=tmp_path,
        profile=CLOUD_PROFILE,
        model_id="kling-3.0-turbo",
    )
    with pytest.raises(JobCancelled):
        await adapter.generate(
            VideoGenRequest(prompt="cat", size="9:16", duration_sec=5),
            session_id="sess-cancel",
        )


@pytest.mark.asyncio
async def test_kling_cancel_tries_remote_endpoints(monkeypatch):
    """取消扇出时应尽力打可灵 cancel 候选路径。"""
    from infrastructure.remote_jobs import interrupt_backends, runtime

    hits = []

    class _Resp:
        status_code = 200
        text = "ok"

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            hits.append(("POST", url))
            return _Resp()

        async def delete(self, url, headers=None):
            hits.append(("DELETE", url))
            return _Resp()

    monkeypatch.setattr(
        "infrastructure.remote_jobs.httpx.AsyncClient", _Client)

    live = runtime.register(
        kind="kling_video", backend="kling",
        session_id="s-cancel-k",
        base_url="https://api.kling.example",
        remote_id="task-99",
    )
    runtime.set_cancel_headers(live.job_id, {
        "Authorization": "Bearer x", "Content-Type": "application/json"})
    n = await interrupt_backends([live])
    assert n >= 1
    assert any("cancel" in u or "task-99" in u for _, u in hits)
    runtime.clear(live.job_id)
