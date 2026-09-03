"""侧边会话（channel='aside'）隔离契约：

覆盖四条隔离边界与级联删除：
1. aside 会话不出现在 list_sessions（默认 + keyword 两个分支）
2. aside 会话内容不出现在 search_conversations（title/user/assistant/all）
3. aside 会话本身可正常读写消息（后台留痕，能力等同主会话）
4. 删除主会话时，其派生的 aside 会话级联删除；非派生的 aside 不受影响
"""
from __future__ import annotations

from pathlib import Path

from infrastructure.db import Database
from agent.session_context import SessionStore

ROOT = Path(__file__).resolve().parent.parent


def _mk_store(tmp_path: Path) -> tuple[SessionStore, Database]:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(data_dir / "sp.db")
    db.run_migrations(ROOT / "migrations")
    return SessionStore(db, data_dir), db


def _seed(store: SessionStore) -> tuple[str, str]:
    """一个主会话 + 一个由它派生的 aside 会话，都含关键词"健身房"。"""
    main = store.create_session()
    store.rename(main, "健身房主线")
    store.append_message(main, "user", "今天去健身房锻炼")
    store.append_message(main, "assistant", "建议每周三次去健身房")

    aside = store.create_session(channel="aside", from_session=main)
    store.append_message(aside, "user", "健身房这段划词想追问一下")
    store.append_message(aside, "assistant", "健身房相关的补充解释")
    return main, aside


def test_aside_excluded_from_list(tmp_path: Path):
    store, db = _mk_store(tmp_path)
    try:
        main, aside = _seed(store)
        r = store.list_sessions()
        sids = {s["session_id"] for s in r["list"]}
        assert main in sids
        assert aside not in sids, "aside 会话不得进入会话列表"
    finally:
        db.close()


def test_aside_excluded_from_list_keyword(tmp_path: Path):
    store, db = _mk_store(tmp_path)
    try:
        main, aside = _seed(store)
        r = store.list_sessions(keyword="健身房")
        sids = {s["session_id"] for s in r["list"]}
        assert main in sids
        assert aside not in sids, "aside 内容不得被列表关键词搜索带出"
    finally:
        db.close()


def test_aside_excluded_from_search(tmp_path: Path):
    store, db = _mk_store(tmp_path)
    try:
        main, aside = _seed(store)
        for scope in ("all", "user", "assistant", "title"):
            r = store.search_conversations("健身房", scope=scope)
            sids = {s["session_id"] for s in r["sessions"]}
            assert aside not in sids, f"aside 不得出现在 search(scope={scope})"
    finally:
        db.close()


def test_aside_messages_persist(tmp_path: Path):
    """aside 会话后台留痕：消息可正常读回（能力等同主会话）。"""
    store, db = _mk_store(tmp_path)
    try:
        _, aside = _seed(store)
        msgs = store.get_messages(aside)
        contents = [m["content"] for m in msgs]
        assert any("划词想追问" in c for c in contents)
        assert any("补充解释" in c for c in contents)
    finally:
        db.close()


def test_delete_main_cascades_aside(tmp_path: Path):
    store, db = _mk_store(tmp_path)
    try:
        main, aside = _seed(store)
        # 另建一个独立 aside（from 其它会话），不应被本次删除波及
        other_main = store.create_session()
        other_aside = store.create_session(channel="aside", from_session=other_main)
        store.append_message(other_aside, "user", "独立侧边")

        store.delete_session(main)

        assert db.query_one(
            "SELECT 1 FROM sessions WHERE session_id=?", (aside,)) is None, \
            "主会话删除应级联删除其派生 aside"
        assert db.query_one(
            "SELECT COUNT(*) n FROM conversations WHERE session_id=?",
            (aside,))["n"] == 0, "级联删除应清理 aside 的消息"
        assert db.query_one(
            "SELECT 1 FROM sessions WHERE session_id=?", (other_aside,)) is not None, \
            "非派生的 aside 不应被波及"
    finally:
        db.close()
