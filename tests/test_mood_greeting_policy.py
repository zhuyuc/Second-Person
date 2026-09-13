"""寒暄情绪降档 / 自我歉意弱化 / 跳过主动行为 — 功能验收。"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.core import AgentCore
from infrastructure.db import Database
from soul.mood_manager import MoodManager
from soul.mood_turn_policy import (
    GREETING_EXPRESSION_CONSTRAINT,
    is_brief_social_turn,
    should_soften_self_negative,
    user_continues_prior_thread,
)

ROOT = Path(__file__).resolve().parent.parent


class _Cfg:
    def __init__(self, **values):
        self.values = {
            "mood_enabled": True,
            "mood_influence_strength": 0.8,
            "mood_actions_enabled": True,
            **values,
        }

    def get(self, key, default=None):
        return self.values.get(key, default)

    def get_raw(self, key, default=None):
        return self.values.get(key, default)


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "mood_turn.db")
    db.run_migrations(ROOT / "migrations")
    return db


def _seed_mood(db, *, ai_mood="apologetic", ai_intensity=0.7,
               ai_attribution="self", user_mood="concerned",
               user_intensity=0.6):
    from infrastructure.timeutil import now_cst
    now = now_cst().isoformat(timespec="seconds")
    db.execute(
        "INSERT OR REPLACE INTO mood_state("
        "id, user_mood, user_intensity, user_updated_at, user_source, "
        "ai_mood, ai_intensity, ai_updated_at, ai_source, "
        "user_attribution, ai_attribution, active_action, last_peace_event_at) "
        "VALUES(1,?,?,?,?,?,?,?,?,?,?,?,NULL)",
        (user_mood, user_intensity, now, "analysis",
         ai_mood, ai_intensity, now, "analysis",
         "none", ai_attribution, None),
    )


def test_brief_social_and_soften_policy():
    assert is_brief_social_turn("你好")
    assert is_brief_social_turn("谢谢")
    assert is_brief_social_turn("好的")
    assert not is_brief_social_turn("把那个文件再改一下")
    assert user_continues_prior_thread("继续改刚才那个")
    assert should_soften_self_negative(
        ai_mood="apologetic", ai_attribution="self", user_message="你好")
    assert not should_soften_self_negative(
        ai_mood="apologetic", ai_attribution="self",
        user_message="上次你弄错了，继续改")


def test_build_state_softens_apologetic_on_greeting(tmp_path: Path):
    db = _db(tmp_path)
    _seed_mood(db)
    mood = MoodManager(db, _Cfg())
    text = mood.build_state_context(user_message="你好")
    assert text
    assert "轻微暗示" in text or "低" in text
    assert "歉意" not in text  # 弱化为 baseline，不展示歉意标签
    assert "对自己表现的评估" not in text


def test_build_state_keeps_apologetic_when_user_continues(tmp_path: Path):
    db = _db(tmp_path)
    _seed_mood(db)
    mood = MoodManager(db, _Cfg())
    text = mood.build_state_context(user_message="上次没做好，继续改那个文件")
    assert "歉意" in text
    assert "对自己表现的评估" in text


def test_tail_skips_action_and_adds_greeting_constraint(tmp_path: Path):
    db = _db(tmp_path)
    _seed_mood(db, ai_mood="ashamed", ai_intensity=0.8, ai_attribution="self",
               user_mood="sad", user_intensity=0.8)
    # 确保 sessions 表有行供 action ctx
    db.execute(
        "INSERT INTO sessions(session_id, title) VALUES(?,?)",
        ("sess_g", "t"))

    class _MoodDisp:
        def evaluate(self, state, ctx):
            return "comfort_first", "【本轮主动行为：先安抚】不应出现在寒暄"

    core = AgentCore.__new__(AgentCore)
    core.config = _Cfg()
    core.db = db
    core.mood = MoodManager(db, core.config)
    core.mood_action_dispatcher = _MoodDisp()
    core.ctx_entry = type("C", (), {"read_consciousness_hint": lambda self: ""})()
    core.skills = type("S", (), {"list_drafts": lambda self: []})()
    core.lifecycle = type("L", (), {
        "next_low_confirm_candidate": lambda self: None})()
    core._should_ask_low_confirm = lambda *_a, **_k: False  # noqa: E731
    core._low_confirm_asked_sessions = set()
    core._pending_low_confirm = None
    core.providers = None

    # 寒暄：有约束、无主动行为
    tail = core._build_turn_tail_contexts(
        sid="sess_g", onboarding=False, location=None, user_message="你好")
    assert GREETING_EXPRESSION_CONSTRAINT.split("\n", 1)[0] in (
        tail["constraints_context"] or "")
    assert "先安抚" not in (tail["constraints_context"] or "")
    assert tail["mood_context"]

    # 非寒暄且用户低落：可触发主动行为
    tail2 = core._build_turn_tail_contexts(
        sid="sess_g", onboarding=False, location=None,
        user_message="我今天很难过，帮我理一理思路")
    assert "先安抚" in (tail2["constraints_context"] or "")


def test_mood_rules_has_greeting_exception():
    text = (ROOT / "agent" / "prompts" / "mood_rules.md").read_text(
        encoding="utf-8")
    assert "极短寒暄例外" in text
    assert "过意不去" in text
