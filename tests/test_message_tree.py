"""消息树形结构（编辑/重新生成分支化）回归测试。

覆盖：
- 037 迁移：新增字段 parent_id / version_group_id / is_active
- append_message：自动推断 parent_id、自动填充 version_group_id
- get_messages：仅返回活跃分支 + 兄弟版本信息
- switch_version：切换版本 + 递归激活/停用下游
- load_recovery_context：仅加载活跃分支消息
- 向后兼容：历史数据 is_active=NULL 也可正常查询
"""
from __future__ import annotations

from pathlib import Path

import pytest

from infrastructure.db import Database
from agent.session_context import SessionStore

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def store(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    db.run_migrations(ROOT / "migrations")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    s = SessionStore(db, data_dir)
    try:
        yield s
    finally:
        db.close()


def _sid(store: SessionStore) -> str:
    return store.create_session()


def test_new_session_starts_with_new_chat_placeholder(store: SessionStore):
    sid = store.create_session()

    session = store.list_sessions(page_size=1)["list"][0]

    assert session["session_id"] == sid
    assert session["title"] == "新对话"
    assert session["title_source"] == "auto"


class TestMigrationSchema:
    def test_tree_columns_exist(self, store: SessionStore):
        cols = {r["name"] for r in store.db.query_all(
            "PRAGMA table_info(conversations)")}
        assert "parent_id" in cols
        assert "version_group_id" in cols
        assert "is_active" in cols

    def test_tree_indexes_exist(self, store: SessionStore):
        indexes = {r["name"] for r in store.db.query_all(
            "PRAGMA index_list(conversations)")}
        assert "idx_conv_parent" in indexes
        assert "idx_conv_vgroup" in indexes


class TestAppendMessage:
    def test_auto_parent_id(self, store: SessionStore):
        sid = _sid(store)
        m1 = store.append_message(sid, "user", "hello")
        m2 = store.append_message(sid, "assistant", "hi")
        m3 = store.append_message(sid, "user", "bye")
        r1 = store.db.query_one("SELECT parent_id FROM conversations WHERE id=?", (m1,))
        r2 = store.db.query_one("SELECT parent_id FROM conversations WHERE id=?", (m2,))
        r3 = store.db.query_one("SELECT parent_id FROM conversations WHERE id=?", (m3,))
        assert r1["parent_id"] is None
        assert r2["parent_id"] == m1
        assert r3["parent_id"] == m2

    def test_auto_version_group_id(self, store: SessionStore):
        sid = _sid(store)
        m1 = store.append_message(sid, "user", "hello")
        r = store.db.query_one("SELECT version_group_id FROM conversations WHERE id=?", (m1,))
        assert r["version_group_id"] == m1

    def test_explicit_parent_and_version_group(self, store: SessionStore):
        sid = _sid(store)
        m1 = store.append_message(sid, "user", "hello")
        m2 = store.append_message(sid, "assistant", "hi")
        m3 = store.append_message(sid, "user", "edited hello",
                                  parent_id=None, version_group_id=m1)
        r = store.db.query_one(
            "SELECT parent_id, version_group_id FROM conversations WHERE id=?", (m3,))
        assert r["parent_id"] is None
        assert r["version_group_id"] == m1

    def test_is_active_default(self, store: SessionStore):
        sid = _sid(store)
        m1 = store.append_message(sid, "user", "hello")
        r = store.db.query_one("SELECT is_active FROM conversations WHERE id=?", (m1,))
        assert r["is_active"] == 1


class TestGetMessages:
    def test_only_active_returned(self, store: SessionStore):
        sid = _sid(store)
        m1 = store.append_message(sid, "user", "hello")
        m2 = store.append_message(sid, "assistant", "hi")
        store.db.execute("UPDATE conversations SET is_active=0 WHERE id=?", (m2,))
        msgs = store.get_messages(sid)
        assert len(msgs) == 1
        assert msgs[0]["id"] == m1

    def test_null_is_active_treated_as_active(self, store: SessionStore):
        sid = _sid(store)
        m1 = store.append_message(sid, "user", "hello")
        store.db.execute("UPDATE conversations SET is_active=NULL WHERE id=?", (m1,))
        msgs = store.get_messages(sid)
        assert len(msgs) == 1

    def test_sibling_info(self, store: SessionStore):
        sid = _sid(store)
        m1 = store.append_message(sid, "user", "v1")
        m2 = store.append_message(sid, "user", "v2", version_group_id=m1,
                                  parent_id=None)
        store.db.execute("UPDATE conversations SET is_active=0 WHERE id=?", (m1,))
        msgs = store.get_messages(sid)
        assert len(msgs) == 1
        msg = msgs[0]
        assert msg["sibling_count"] == 2
        assert msg["has_branches"] is True


class TestBranchWorkflow:
    """模拟完整的编辑分支工作流。"""

    def _build_linear(self, store, sid):
        m1 = store.append_message(sid, "user", "u1")
        m2 = store.append_message(sid, "assistant", "a1")
        m3 = store.append_message(sid, "user", "u2")
        m4 = store.append_message(sid, "assistant", "a2")
        return m1, m2, m3, m4

    def test_edit_creates_branch(self, store: SessionStore):
        sid = _sid(store)
        m1, m2, m3, m4 = self._build_linear(store, sid)

        # 编辑 m1 → 创建新用户消息 m5（同 version_group = m1），停用旧链
        store.db.execute(
            "UPDATE conversations SET is_active=0 WHERE version_group_id=?", (m1,))
        store.db.execute(
            "UPDATE conversations SET is_active=0 WHERE id IN (?,?)", (m2, m3))
        store.db.execute(
            "UPDATE conversations SET is_active=0 WHERE id=?", (m4,))
        m5 = store.append_message(sid, "user", "u1-edited",
                                  parent_id=None, version_group_id=m1)
        m6 = store.append_message(sid, "assistant", "a1-new", parent_id=m5)

        msgs = store.get_messages(sid)
        contents = [m["content"] for m in msgs]
        assert "u1-edited" in contents
        assert "a1-new" in contents
        assert "u1" not in contents
        assert "a1" not in contents

    def test_switch_version_activates_correct_branch(self, store: SessionStore):
        sid = _sid(store)
        m1, m2, m3, m4 = self._build_linear(store, sid)

        # 编辑 m1 → 分支
        store.db.execute("UPDATE conversations SET is_active=0 WHERE id=?", (m1,))
        store.db.execute("UPDATE conversations SET is_active=0 WHERE id=?", (m2,))
        store.db.execute("UPDATE conversations SET is_active=0 WHERE id=?", (m3,))
        store.db.execute("UPDATE conversations SET is_active=0 WHERE id=?", (m4,))
        m5 = store.append_message(sid, "user", "u1-v2",
                                  parent_id=None, version_group_id=m1)
        m6 = store.append_message(sid, "assistant", "a1-v2", parent_id=m5)

        # 切回原版本 m1
        store.switch_version(sid, m1, m1)
        msgs = store.get_messages(sid)
        contents = [m["content"] for m in msgs]
        assert "u1" in contents
        assert "a1" in contents
        assert "u2" in contents
        assert "a2" in contents
        assert "u1-v2" not in contents

        # 再切回编辑版本 m5
        store.switch_version(sid, m1, m5)
        msgs = store.get_messages(sid)
        contents = [m["content"] for m in msgs]
        assert "u1-v2" in contents
        assert "a1-v2" in contents
        assert "u1" not in contents


class TestLoadRecoveryContext:
    def test_only_active_branch_in_context(self, store: SessionStore):
        sid = _sid(store)
        m1 = store.append_message(sid, "user", "original")
        m2 = store.append_message(sid, "assistant", "resp-original")
        m3 = store.append_message(sid, "user", "follow-up")
        m4 = store.append_message(sid, "assistant", "resp-follow")

        # 编辑 m1，停用旧链
        for mid in (m1, m2, m3, m4):
            store.db.execute(
                "UPDATE conversations SET is_active=0 WHERE id=?", (mid,))
        m5 = store.append_message(sid, "user", "edited",
                                  parent_id=None, version_group_id=m1)
        m6 = store.append_message(sid, "assistant", "new-resp", parent_id=m5)

        ctx = store.load_recovery_context(sid)
        ctx_contents = [m["content"] for m in ctx if m["role"] != "system"]
        assert "edited" in ctx_contents
        assert "new-resp" in ctx_contents
        assert "original" not in ctx_contents
        assert "resp-original" not in ctx_contents

    def test_backward_compat_null_is_active(self, store: SessionStore):
        sid = _sid(store)
        m1 = store.append_message(sid, "user", "old-msg")
        m2 = store.append_message(sid, "assistant", "old-resp")
        store.db.execute("UPDATE conversations SET is_active=NULL WHERE id=?", (m1,))
        store.db.execute("UPDATE conversations SET is_active=NULL WHERE id=?", (m2,))
        ctx = store.load_recovery_context(sid)
        ctx_contents = [m["content"] for m in ctx if m["role"] != "system"]
        assert "old-msg" in ctx_contents
        assert "old-resp" in ctx_contents


class TestDeactivateActivateDownstream:
    def test_deactivate_downstream(self, store: SessionStore):
        sid = _sid(store)
        m1 = store.append_message(sid, "user", "root")
        m2 = store.append_message(sid, "assistant", "child1")
        m3 = store.append_message(sid, "user", "child2")
        store._deactivate_downstream(m1)
        for mid in (m2, m3):
            r = store.db.query_one("SELECT is_active FROM conversations WHERE id=?", (mid,))
            assert r["is_active"] == 0
        r1 = store.db.query_one("SELECT is_active FROM conversations WHERE id=?", (m1,))
        assert r1["is_active"] == 1

    def test_activate_downstream(self, store: SessionStore):
        sid = _sid(store)
        m1 = store.append_message(sid, "user", "root")
        m2 = store.append_message(sid, "assistant", "child")
        m3 = store.append_message(sid, "user", "grandchild")
        for mid in (m2, m3):
            store.db.execute(
                "UPDATE conversations SET is_active=0 WHERE id=?", (mid,))
        store._activate_downstream(m1, sid)
        for mid in (m2, m3):
            r = store.db.query_one("SELECT is_active FROM conversations WHERE id=?", (mid,))
            assert r["is_active"] == 1
