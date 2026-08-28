"""记忆写入治理：门禁、候选证据、确认和过期。"""
from __future__ import annotations

import asyncio
from pathlib import Path

from infrastructure.db import Database
from memory.write_gate import MemoryWriteGate, fingerprint, sensitivity_level

ROOT = Path(__file__).resolve().parent.parent


class _Config(dict):
    def get(self, key, default=None):
        return super().get(key, default)


def _item(text="用户偏好直接的项目沟通"):
    # 含实体保证过 is_well_formed（P1-C），不影响原有评分/证据/敏感断言
    return {"title": "沟通偏好", "summary": text, "detail": text,
            "domain": "work", "attribution": "verified",
            "entities": ["用户"], "stability": 0.9,
            "reuse": 0.9, "user_specificity": 0.9, "explicitness": 0.2}


def test_gate_rejects_session_and_sensitive_content():
    cfg = _Config(memory_write_strictness="normal")
    gate = MemoryWriteGate(None, cfg)
    session = gate.evaluate({**_item("这次先用方案二"), "channel": "session_only"})
    assert session.status == "rejected"
    assert sensitivity_level("请记住我的 API key 是 sk-abcdefghijklmnopqrstuvwxyz") == "high"
    sensitive = gate.evaluate({**_item("API key 是 sk-abcdefghijklmnopqrstuvwxyz")}, explicit=True)
    assert sensitive.allowed is False


def test_candidate_requires_evidence_then_can_confirm(tmp_path: Path):
    async def scenario():
        db = Database(tmp_path / "palace.db")
        db.run_migrations(ROOT / "migrations")
        cfg = _Config(memory_write_strictness="loose",
                      memory_min_evidence_cross_session=2, memory_candidate_ttl_days=7)
        gate = MemoryWriteGate(db, cfg)
        item = _item()
        cid = gate.enqueue(item, "memory", session_id="s1", message_id=1,
                           evidence={"source_ref": "s1:1"})
        assert cid
        row = db.query_one("SELECT status,evidence_count,stability,reuse,user_specificity "
                           "FROM memory_write_candidates WHERE candidate_id=?", (cid,))
        assert row["status"] == "pending"
        assert row["evidence_count"] == 1
        assert row["stability"] > 0
        assert row["reuse"] > 0
        assert row["user_specificity"] > 0

        cid2 = gate.enqueue(item, "memory", session_id="s2", message_id=2,
                            evidence={"source_ref": "s2:2"})
        assert cid2 == cid
        row = db.query_one("SELECT status,evidence_count FROM memory_write_candidates WHERE candidate_id=?", (cid,))
        assert row["evidence_count"] == 2
        assert row["status"] in {"pending", "approved"}

        assert gate.confirm(cid)
        row = db.query_one("SELECT status,confirmed_at FROM memory_write_candidates WHERE candidate_id=?", (cid,))
        assert row["status"] == "approved"
        assert row["confirmed_at"]
        db.close()

    asyncio.run(scenario())


def test_candidate_fingerprint_is_stable():
    assert fingerprint(_item()) == fingerprint({**_item(), "detail": "  用户偏好直接的项目沟通  "})


def test_repeated_evidence_reference_does_not_inflate_count(tmp_path: Path):
    db = Database(tmp_path / "palace.db")
    db.run_migrations(ROOT / "migrations")
    gate = MemoryWriteGate(db, _Config(memory_write_strictness="loose",
                                       memory_min_evidence_cross_session=2))
    item = _item()
    cid = gate.enqueue(item, session_id="s1", evidence={"source_ref": "s1:1"})
    assert cid
    gate.enqueue(item, session_id="s1", evidence={"source_ref": "s1:1"})
    row = db.query_one("SELECT evidence_count FROM memory_write_candidates WHERE candidate_id=?", (cid,))
    assert row["evidence_count"] == 1
    db.close()


def test_negative_feedback_requires_confirmation(tmp_path: Path):
    db = Database(tmp_path / "palace.db")
    db.run_migrations(ROOT / "migrations")
    gate = MemoryWriteGate(db, _Config(memory_write_strictness="loose"))
    # 用第一人称文本确保规则派生的 user_specificity 上限较高，score 能过 min_score，
    # 才能走到 negative_count 判定分支。规则化再评分（T1-A）后，
    # 无第一人称信号的文本会被 user_specificity_cap=0.25 直接压分。
    decision = gate.evaluate(_item("我一直偏好直接的项目沟通"),
                             negative_count=2, evidence_count=2)
    assert decision.status == "pending"
    assert "负反馈" in decision.reason
    db.close()
