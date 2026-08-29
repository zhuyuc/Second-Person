"""跨会话搜索契约：三路命中（title/user/assistant）+ 聚合 + 高亮。

覆盖：
1. scope=title 只命中会话标题
2. scope=user 只命中用户消息，assistant 消息不返回
3. scope=assistant 只命中 AI 回复
4. scope=all 汇总三路，snippet_html 含 <mark>
5. 空查询 / 无命中 返回空
6. list_sessions(keyword=...) 关键字回归（原先 self._fts 缺失 bug）
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


def _seed(store: SessionStore) -> tuple[str, str, str]:
    """三个会话，关键词 "健身房"（3 字，满足 trigram tokenizer 最小长度）：
    - s1 标题 / user / assistant 三路均命中
    - s2 只有 user 命中（标题与 assistant 无关键词）
    - s3 完全无关
    """
    s1 = store.create_session()
    store.rename(s1, "健身房打卡")
    store.append_message(s1, "user", "今天开始去健身房锻炼")
    store.append_message(s1, "assistant", "建议每周三次去健身房，先做基础动作")

    s2 = store.create_session()
    store.rename(s2, "读书笔记")
    store.append_message(s2, "user", "健身房要练力量，理论书也得看")
    store.append_message(s2, "assistant", "推荐一本运动生理学入门")

    s3 = store.create_session()
    store.rename(s3, "无关会话")
    store.append_message(s3, "user", "今天天气不错")
    store.append_message(s3, "assistant", "适合出门散步")
    return s1, s2, s3


def test_search_title_scope(tmp_path: Path):
    store, db = _mk_store(tmp_path)
    try:
        s1, s2, s3 = _seed(store)
        r = store.search_conversations("健身房", scope="title")
        sids = {s["session_id"] for s in r["sessions"]}
        # s1 标题含健身房 → 命中；s2 标题无健身房 → 不命中；s3 无关
        assert s1 in sids
        assert s2 not in sids
        assert s3 not in sids
        row = next(s for s in r["sessions"] if s["session_id"] == s1)
        assert row["title_hit"] is True
        assert "<mark>健身房</mark>" in row["title_html"]
        # title scope 不走消息 FTS
        assert row["hits"] == []
    finally:
        db.close()


def test_search_user_scope_excludes_assistant(tmp_path: Path):
    store, db = _mk_store(tmp_path)
    try:
        s1, s2, s3 = _seed(store)
        r = store.search_conversations("健身房", scope="user")
        # s1 和 s2 的 user 消息都提到"健身房"
        sids = {s["session_id"] for s in r["sessions"]}
        assert s1 in sids and s2 in sids
        assert s3 not in sids
        for s in r["sessions"]:
            for h in s["hits"]:
                assert h["role"] == "user", "user scope 不应返回 assistant"
                assert "<mark>" in h["snippet_html"]
    finally:
        db.close()


def test_search_assistant_scope(tmp_path: Path):
    store, db = _mk_store(tmp_path)
    try:
        s1, s2, s3 = _seed(store)
        r = store.search_conversations("健身房", scope="assistant")
        sids = {s["session_id"] for s in r["sessions"]}
        # 只有 s1 的 assistant 回复提到"健身房"
        assert s1 in sids
        assert s2 not in sids
        assert s3 not in sids
        for s in r["sessions"]:
            for h in s["hits"]:
                assert h["role"] == "assistant"
    finally:
        db.close()


def test_search_all_scope_aggregates(tmp_path: Path):
    store, db = _mk_store(tmp_path)
    try:
        s1, s2, _ = _seed(store)
        r = store.search_conversations("健身房", scope="all")
        sids = {s["session_id"] for s in r["sessions"]}
        assert s1 in sids and s2 in sids
        row_s1 = next(s for s in r["sessions"] if s["session_id"] == s1)
        # s1 同时命中 title / user / assistant，hits 至少 2 条
        assert row_s1["title_hit"] is True
        assert row_s1["hit_count"] >= 2
        assert any(h["role"] == "user" for h in row_s1["hits"])
        assert any(h["role"] == "assistant" for h in row_s1["hits"])
    finally:
        db.close()


def test_search_empty_and_no_hit(tmp_path: Path):
    store, db = _mk_store(tmp_path)
    try:
        _seed(store)
        assert store.search_conversations("", scope="all") == {
            "query": "", "total_sessions": 0, "sessions": []}
        # 只有标点：fts_escape 分词后为空 → 消息路空；title LIKE 也不命中
        r = store.search_conversations("!!!", scope="all")
        assert r["total_sessions"] == 0
        r2 = store.search_conversations("量子隧穿", scope="all")
        assert r2["total_sessions"] == 0
    finally:
        db.close()


def test_list_sessions_keyword_regression(tmp_path: Path):
    """回归 list_sessions(keyword=...) 里 self._fts 缺失导致 AttributeError 的 bug。"""
    store, db = _mk_store(tmp_path)
    try:
        s1, s2, _ = _seed(store)
        # 关键字走 conversations_fts，s1 和 s2 都能命中"健身房"
        r = store.list_sessions(keyword="健身房")
        sids = {s["session_id"] for s in r["list"]}
        assert s1 in sids
        assert s2 in sids
        # 空关键字降级为空结果，不抛异常
        r_empty = store.list_sessions(keyword="!!!")
        assert r_empty == {"total": 0, "list": []}
    finally:
        db.close()
