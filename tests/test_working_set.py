"""工作集 / 文件卡片 / 编辑态记忆降级验收。"""
from __future__ import annotations

from pathlib import Path

from agent.turn_events import TurnEventStore
from agent.working_set import (
    FileCardStore,
    append_recall_stub,
    build_working_set_turn,
    is_file_edit_intent,
    recall_context_for_compact,
    render_file_cards,
    render_working_set,
    should_demote_memory,
)
from infrastructure.db import Database
from tools.fs.observation import FsObservationStore

ROOT = Path(__file__).resolve().parent.parent


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "ws.db")
    db.run_migrations(ROOT / "migrations")
    return db


def test_observation_list_recent_and_last_op(tmp_path: Path):
    db = _db(tmp_path)
    obs = FsObservationStore(db)
    obs.record("s1", "/a.py", "v1", last_op="read")
    obs.record("s1", "/b.py", "v2", last_op="edit")
    rows = obs.list_recent("s1", limit=8)
    assert len(rows) == 2
    assert rows[0]["target_key"] == "/b.py"
    assert rows[0]["last_op"] == "edit"


def test_file_cards_trim_and_spill(tmp_path: Path):
    db = _db(tmp_path)
    cards = FileCardStore(db)
    for i in range(15):
        cards.append("s1", f"/f{i}.py", "read", version=f"v{i}")
    recent = cards.list_recent("s1")
    assert len(recent) == 12
    assert recent[0]["path"] == "/f3.py"
    cards.append("s1", "/spill.txt", "spill", spill_path="/spill.txt")
    recent2 = cards.list_recent("s1")
    assert recent2[-1]["op"] == "spill"
    assert recent2[-1]["spill_path"] == "/spill.txt"


def test_build_working_set_emits_every_turn(tmp_path: Path):
    db = _db(tmp_path)
    obs = FsObservationStore(db)
    cards = FileCardStore(db)
    obs.record("s1", "/demo.html", "1:2:3", last_op="edit")
    cards.append("s1", "/demo.html", "edit", version="1:2:3")

    t1 = build_working_set_turn(
        observations=obs, cards=cards, session_id="s1",
        user_message="继续改那个文件")
    assert t1.working_set_emit and "[文件工作集]" in t1.working_set_emit
    assert "/demo.html" in t1.working_set_emit
    assert t1.file_cards_emit and "edit" in t1.file_cards_emit
    assert t1.demote_memory is True

    t2 = build_working_set_turn(
        observations=obs, cards=cards, session_id="s1",
        user_message="再改一下标题")
    # 跨轮必须再次注入，不能静默
    assert t2.working_set_emit is not None
    assert "/demo.html" in t2.working_set_emit


def test_demote_skips_on_recall_intent():
    entries = [{"last_op": "edit", "target_key": "/a.py"}]
    assert should_demote_memory("继续改", entries) is True
    assert should_demote_memory("你还记得我上次说的吗", entries) is False
    assert is_file_edit_intent("接着改那个 html") is True


def test_model_messages_includes_working_set_tail(tmp_path: Path):
    db = _db(tmp_path)
    store = TurnEventStore(db)
    turn = store.start_turn("sess_ws", reasoning_effort="high", max_steps=4)
    turn_id = turn["id"]
    store.append(turn_id, "user.message", actor="user", model_visible=True,
                 payload={"content": "继续改"})
    store.append(turn_id, "context.working_set", actor="host", model_visible=True,
                 payload={"content": "[文件工作集]\n- edit `/x.py` (v=1)"})
    store.append(turn_id, "context.file_cards", actor="host", model_visible=True,
                 payload={"content": "[文件工作痕迹]\n- spill `/tmp/a.txt`"})
    messages, _ = store.model_messages(turn_id)
    contents = [m["content"] for m in messages if m["role"] == "user"]
    assert any("[文件工作集]" in c for c in contents)
    assert any("[文件工作痕迹]" in c for c in contents)


def test_render_file_cards_budget():
    cards = [
        {"path": f"/very/long/path/file_{i}.py", "op": "read",
         "version": "v" * 20}
        for i in range(20)
    ]
    text = render_file_cards(cards, budget=200)
    assert text is not None
    assert len(text) <= 200
    assert text.startswith("[文件工作痕迹]")


def test_append_recall_stub():
    out = append_recall_stub("## 文件与代码\n- `/a.py`", "[文件工作集]\n- `/a.py`")
    assert "## 可召回定位" in out
    assert "[文件工作集]" in out
    assert render_working_set([]) is None


def test_recall_context_for_compact(tmp_path: Path):
    db = _db(tmp_path)
    obs = FsObservationStore(db)
    cards = FileCardStore(db)
    obs.record("s1", "/demo.html", "1", last_op="edit")
    cards.append("s1", "/demo.html", "edit", version="1")
    turn = build_working_set_turn(
        observations=obs, cards=cards, session_id="s1",
        user_message="继续改")
    recall = recall_context_for_compact(turn)
    assert recall and "/demo.html" in recall
    assert recall_context_for_compact(None) is None
