"""P1-C 空/占位候选拦截 + P1-D 用户否认信号 契约。"""
from __future__ import annotations

from pathlib import Path

from infrastructure.db import Database
from memory.write_gate import (
    MemoryWriteGate, has_denial_signal, is_well_formed,
)

ROOT = Path(__file__).resolve().parent.parent


class _Cfg(dict):
    def get(self, k, d=None):
        return super().get(k, d)


def test_placeholder_items_rejected():
    # 全部字段占位 → 拦截
    assert not is_well_formed({"title": "untitled", "summary": "",
                                "detail": "", "entities": []})
    # 只有 title 有效但没有实体/第一人称 → 拦截
    assert not is_well_formed({"title": "偏好", "summary": "偏好",
                                "detail": "", "entities": []})
    # 加实体或第一人称 → 通过
    assert is_well_formed({"title": "偏好", "summary": "我偏好直接",
                            "detail": "偏好直接", "entities": []})
    assert is_well_formed({"title": "会议", "summary": "关于产品",
                            "detail": "详情", "entities": ["Alice"]})


def test_denial_signal_detects_zh_and_en():
    assert has_denial_signal("不对，我没说过这个")
    assert has_denial_signal("你记错了，我可不是产品经理")
    assert has_denial_signal("No I didn't say that")
    assert has_denial_signal("that's wrong")
    assert not has_denial_signal("我以后会用 pytest")


def test_suppress_recent_from_denial(tmp_path: Path):
    db = Database(tmp_path / "sp.db")
    db.run_migrations(ROOT / "migrations")
    gate = MemoryWriteGate(db, _Cfg(memory_candidate_min_score=45))
    item = {"title": "偏好", "summary": "我一直偏好直接的沟通",
            "detail": "我一直偏好直接的沟通", "domain": "work",
            "attribution": "verified", "entities": []}
    cid = gate.enqueue(item, session_id="s_denial", message_id=1,
                       evidence={"source_ref": "s_denial:1"})
    assert cid
    # 用户否认 → 抑制
    n = gate.suppress_recent_from_denial("s_denial", minutes=60)
    assert n >= 1
    row = db.query_one("SELECT status FROM memory_write_candidates "
                       "WHERE candidate_id=?", (cid,))
    assert row["status"] == "rejected"
    db.close()
