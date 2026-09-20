"""视频工坊：资产 CRUD、会话隔离、与 generate_video 共用执行体。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.session_context import SessionStore
from agent.video_workshop import WORKSHOP_TYPES, VideoWorkshopStore, WorkshopError
from infrastructure.db import Database
from infrastructure.video_gen.execute import execute_video_gen
from infrastructure.video_gen.profiles import CLOUD_PROFILE, LOCAL_PROFILE
from infrastructure.video_gen.types import VideoGenResult

ROOT = Path(__file__).resolve().parent.parent


def _mk(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "chat_videos").mkdir(parents=True, exist_ok=True)
    db = Database(data_dir / "sp.db")
    db.run_migrations(ROOT / "migrations")
    return VideoWorkshopStore(db, data_dir), SessionStore(db, data_dir), db, data_dir


def test_workshop_crud_and_types(tmp_path: Path):
    store, _, _, _ = _mk(tmp_path)
    with pytest.raises(WorkshopError):
        store.create("", "广告片")
    with pytest.raises(WorkshopError):
        store.create("有名", "不存在的类型")
    assert "广告片" in WORKSHOP_TYPES
    p = store.create("南天门外", "AI 短剧")
    assert p.status == "draft"
    assert p.title == "南天门外"
    p2 = store.update(p.id, script="孙悟空站在门外", aspect="16:9", duration_sec=5)
    assert p2.script.startswith("孙悟空")
    listed = store.list(q="南天")
    assert len(listed) == 1
    store.delete(p.id)
    with pytest.raises(WorkshopError):
        store.get(p.id)


def test_workshop_channel_excluded_from_list_and_search(tmp_path: Path):
    store, sessions, db, _ = _mk(tmp_path)
    main = sessions.create_session()
    sessions.rename(main, "主对话健身房")
    sessions.append_message(main, "user", "健身房计划")
    ws = sessions.create_session(channel="workshop")
    sessions.rename(ws, "工坊·测试")
    sessions.append_message(ws, "user", "健身房剧本优化")
    sessions.append_message(ws, "assistant", "优化后的健身房脚本")

    sids = {s["session_id"] for s in sessions.list_sessions()["list"]}
    assert main in sids
    assert ws not in sids

    for scope in ("all", "title", "user", "assistant"):
        hits = sessions.search_conversations("健身房", scope=scope)
        hit_ids = {h["session_id"] for h in hits["sessions"]}
        assert ws not in hit_ids, f"workshop 不得出现在 search(scope={scope})"


def test_delete_removes_file_and_session(tmp_path: Path):
    store, sessions, _, data_dir = _mk(tmp_path)
    p = store.create("删片测试", "广告片")
    sid = sessions.create_session(channel="workshop")
    store.update(p.id, session_id=sid, filename="genv_testdelete.mp4",
                 public_url="/chat-videos/genv_testdelete.mp4", status="done")
    fpath = data_dir / "chat_videos" / "genv_testdelete.mp4"
    fpath.write_bytes(b"fake")
    store.delete(p.id, sessions=sessions)
    assert not fpath.exists()
    assert sessions.db.query_one(
        "SELECT 1 FROM sessions WHERE session_id=?", (sid,)) is None


@pytest.mark.asyncio
async def test_execute_video_gen_shared_path():
    """工具与工坊共用 execute_video_gen：成功返回 generated_video 形态。"""
    providers = MagicMock()
    snap = MagicMock()
    snap.provider_id = "p1"
    snap.model_id = "m1"
    snap.provider_type = "cloud"
    snap.input_price = None
    snap.output_price = None
    providers.snapshot_for.return_value = snap

    fake = VideoGenResult(
        filenames=["genv_abc.mp4"],
        public_urls=["/chat-videos/genv_abc.mp4"],
        duration_sec=5,
        size="16:9",
        summary="ok",
    )
    adapter = MagicMock()
    adapter.generate = AsyncMock(return_value=fake)

    with patch("infrastructure.video_gen.execute.video_profile_for",
               return_value=CLOUD_PROFILE), \
         patch("infrastructure.video_gen.execute.get_video_adapter",
               return_value=adapter), \
         patch("infrastructure.video_gen.execute.get_tracer") as gt:
        gt.return_value.generation_start.return_value = None
        out = await execute_video_gen(
            prompt="一只猫在草地上跑",
            providers=providers,
            config={},
            data_dir=".",
            size="16:9",
            duration_sec=5,
        )
    assert out["type"] == "generated_video"
    assert out["filenames"] == ["genv_abc.mp4"]
    adapter.generate.assert_awaited_once()


def test_aspect_options_respect_local_profile():
    from app.routes.workshop import _aspect_options
    opts = _aspect_options(LOCAL_PROFILE)
    values = {o["value"] for o in opts}
    assert "9:16" in values or "16:9" in values
    # 本地不允许凭空出现云端专属 21:9
    assert "21:9" not in values


def test_generate_video_toolspec_unchanged():
    """硬约束：工坊不得改动 generate_video 对外 schema/描述。"""
    src = (ROOT / "tools" / "builtin.py").read_text(encoding="utf-8")
    assert 'ToolSpec(\n        "generate_video"' in src \
        or 'ToolSpec(\r\n        "generate_video"' in src
    # 关键属性名必须仍在注册块中
    block = src.split('"generate_video"', 1)[1].split("registry.register_function", 1)[0]
    for key in ("prompt", "negative_prompt", "size", "duration_sec",
                "n", "motion_hint", "style_hint"):
        assert f'"{key}"' in block
    assert "一次只生成 1 条" in block
    assert "execute_video_gen" in src


def test_workshop_http_api(tmp_path: Path):
    from fastapi.testclient import TestClient
    from app.main import create_app

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "chat_videos").mkdir()
    app = create_app(data_dir)
    client = TestClient(app)

    caps = client.get("/api/workshop/capabilities").json()["data"]
    assert len(caps["types"]) == 6
    assert "supports_i2v" in caps
    assert "refs_for_rewrite_only" in caps

    bad = client.post("/api/workshop/projects", json={"title": "x", "type": "假类型"})
    assert bad.status_code == 422

    created = client.post(
        "/api/workshop/projects",
        json={"title": "HTTP片", "type": "故事短片"},
    ).json()["data"]
    pid = created["id"]

    client.patch(f"/api/workshop/projects/{pid}", json={"script": "清晨的街道"})
    ens = client.post(f"/api/workshop/projects/{pid}/ensure-session").json()["data"]
    sid = ens["session_id"]

    sessions = client.get("/api/chat/sessions").json()["data"]["list"]
    assert sid not in {s["session_id"] for s in sessions}

    # multipart 上传参考图（对齐主对话：不走 JSON dataURL）
    tiny_png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
        b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    up = client.post(
        f"/api/workshop/projects/{pid}/refs",
        files=[("files", ("a.png", tiny_png, "image/png"))],
    )
    assert up.status_code == 200, up.text
    refs = up.json()["data"]["refs"]
    assert len(refs) == 1
    assert refs[0].startswith("/chat-images/wsref_")

    # 无脚本不可出片：先清空
    client.patch(f"/api/workshop/projects/{pid}", json={"script": "   "})
    denied = client.post(f"/api/workshop/projects/{pid}/render")
    assert denied.status_code == 400

    client.delete(f"/api/workshop/projects/{pid}")
    gone = client.get(f"/api/workshop/projects/{pid}")
    assert gone.status_code == 404
    from infrastructure.remote_jobs import set_store
    from app.main import get_container
    set_store(None)
    get_container().db.close()


def test_try_claim_doing_is_atomic(tmp_path: Path):
    store, _, _, _ = _mk(tmp_path)
    p = store.create("抢锁", "广告片")
    store.update(p.id, script="镜头一")
    claimed = store.try_claim_doing(p.id)
    assert claimed.status == "doing"
    with pytest.raises(WorkshopError):
        store.try_claim_doing(p.id)


def test_reclaim_stale_doing(tmp_path: Path):
    store, _, db, _ = _mk(tmp_path)
    p = store.create("卡死", "广告片")
    store.try_claim_doing(p.id)
    db.execute(
        "UPDATE video_projects SET updated_at=? WHERE id=?",
        ("2020-01-01 00:00:00.000000", p.id),
    )
    n = store.reclaim_stale_doing(older_than_minutes=5)
    assert n == 1
    assert store.get(p.id).status == "failed"


def test_delete_blocked_while_doing(tmp_path: Path):
    store, sessions, _, _ = _mk(tmp_path)
    p = store.create("删中", "广告片")
    store.try_claim_doing(p.id)
    with pytest.raises(WorkshopError):
        store.delete(p.id, sessions=sessions)
    store.update(p.id, status="failed")
    store.delete(p.id, sessions=sessions)


def test_refs_persist_like_chat(tmp_path: Path):
    """参考图 multipart / dataURL 均落盘 chat_images，库内只存文件名。"""
    import base64 as b64

    store, _, _, data_dir = _mk(tmp_path)
    p = store.create("大图", "广告片")
    # 1x1 PNG
    tiny_png = b64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    huge_bytes = b"\x00" * 2_000_000
    updated = store.add_ref_uploads(
        p.id,
        [(tiny_png, "image/png"), (huge_bytes, "image/png")],
    )
    assert len(updated.refs_json) == 2
    for fname in updated.refs_json:
        assert not str(fname).startswith("data:")
        assert str(fname).startswith("wsref_")
        assert (data_dir / "chat_images" / fname).is_file()
    d = updated.to_dict()
    assert all(u.startswith("/chat-images/") for u in d["refs"])
    keep = updated.refs_json[0]
    drop = updated.refs_json[1]
    store.update(p.id, refs_json=[keep])
    assert (data_dir / "chat_images" / keep).is_file()
    assert not (data_dir / "chat_images" / drop).exists()

    # 兼容：旧 PATCH 仍可把 dataURL 落盘
    tiny = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    via_patch = store.update(p.id, refs_json=[keep, tiny])
    assert len(via_patch.refs_json) == 2
    assert all(str(f).startswith("wsref_") for f in via_patch.refs_json)


def test_re_render_removes_old_file(tmp_path: Path):
    store, _, _, data_dir = _mk(tmp_path)
    p = store.create("重出", "广告片")
    old = data_dir / "chat_videos" / "genv_old.mp4"
    old.write_bytes(b"old")
    store.update(p.id, script="s", filename="genv_old.mp4",
                 public_url="/chat-videos/genv_old.mp4", status="done")
    store.try_claim_doing(p.id)
    assert not old.exists()
    assert store.get(p.id).filename is None


def test_compile_model_prompt_keeps_picture_drops_commentary():
    from agent.workshop_prompt import compile_model_prompt

    script = """共享设定
- 人物识别：青布短打，右手哨棒
- 背景锁定：景阳冈乱石
- 本片主冲突：要活着下冈
- 行为路径：站定再打

**镜 1 · 5 秒**
人物：武松朝右，棒横在身前
背景：枯松
声光：余晖打在棒上
音乐：一记鼓
对白：武松对虎，「好大的虫」
动作：右脚踩实，棒换到身前
冲突：第一扑还没来
"""
    text, mode = compile_model_prompt(script)
    assert mode == "shots"
    assert "青布短打" in text
    assert "景阳冈乱石" in text
    assert "武松朝右" in text
    assert "右脚踩实" in text
    assert "余晖打在棒上" in text
    assert "好大的虫" in text
    assert "要活着下冈" not in text
    assert "一记鼓" not in text
    assert "第一扑还没来" not in text

    raw, raw_mode = compile_model_prompt("一只猫走过门口")
    assert raw_mode == "raw"
    assert raw == "一只猫走过门口"


def test_latest_active_excludes_workshop(tmp_path: Path):
    _, sessions, _, _ = _mk(tmp_path)
    main = sessions.create_session()
    ws = sessions.create_session(channel="workshop")
    sessions.rename(ws, "工坊晚")
    assert sessions.latest_active_session() == main


@pytest.mark.asyncio
async def test_execute_video_gen_passes_resolution():
    providers = MagicMock()
    snap = MagicMock()
    snap.provider_id = "p1"
    snap.model_id = "m1"
    snap.provider_type = "cloud"
    snap.input_price = None
    snap.output_price = None
    providers.snapshot_for.return_value = snap

    fake = VideoGenResult(filenames=["genv_x.mp4"], public_urls=["/chat-videos/genv_x.mp4"])
    adapter = MagicMock()
    adapter.generate = AsyncMock(return_value=fake)

    with patch("infrastructure.video_gen.execute.video_profile_for",
               return_value=CLOUD_PROFILE), \
         patch("infrastructure.video_gen.execute.get_video_adapter",
               return_value=adapter), \
         patch("infrastructure.video_gen.execute.get_tracer") as gt:
        gt.return_value.generation_start.return_value = None
        await execute_video_gen(
            prompt="猫",
            providers=providers,
            config={},
            data_dir=".",
            size="16:9",
            duration_sec=5,
            resolution="480p",
        )
    req = adapter.generate.await_args.args[0]
    assert req.resolution == "480p"


def test_patch_doing_blocked_http(tmp_path: Path):
    from fastapi.testclient import TestClient
    from app.main import create_app, get_container

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "chat_videos").mkdir()
    app = create_app(data_dir)
    client = TestClient(app)
    created = client.post(
        "/api/workshop/projects",
        json={"title": "锁写", "type": "故事短片"},
    ).json()["data"]
    pid = created["id"]
    client.patch(f"/api/workshop/projects/{pid}", json={"script": "有脚本"})
    get_container().workshop.try_claim_doing(pid)
    blocked = client.patch(f"/api/workshop/projects/{pid}", json={"script": "改稿"})
    assert blocked.status_code == 409
    from infrastructure.remote_jobs import set_store
    set_store(None)
    get_container().db.close()
