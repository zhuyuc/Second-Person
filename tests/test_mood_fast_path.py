"""情绪快路径：词典 / 展示融合 / Flash 调用约束 / 超时降级 / peace+decline。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from infrastructure.db import Database
from soul.mood_fast import detect_user_pulse
from soul.mood_fast_signal import detect_lexicon
from soul.mood_manager import MoodManager
from soul.mood_peace import detect_peace_event

ROOT = Path(__file__).resolve().parent.parent


class _Cfg:
    def __init__(self, **values):
        self.values = {
            "mood_enabled": True,
            "mood_influence_strength": 0.8,
            "mood_actions_enabled": True,
            "mood_fast_path_enabled": True,
            "mood_fast_path_provider": "flash",
            "mood_fast_path_timeout_ms": 800,
            "mood_fast_path_min_confidence": 0.55,
            "mood_fast_path_thinking": False,
            "mood_decay_hours": 2.0,
            **values,
        }

    def get(self, key, default=None):
        return self.values.get(key, default)


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "mood_fast.db")
    db.run_migrations(ROOT / "migrations")
    return db


def _seed_user(db, *, mood="angry", intensity=0.8):
    from infrastructure.timeutil import now_cst
    now = now_cst().isoformat(timespec="seconds")
    db.execute(
        "INSERT OR REPLACE INTO mood_state("
        "id, user_mood, user_intensity, user_updated_at, user_source, "
        "ai_mood, ai_intensity, ai_updated_at, ai_source, "
        "user_attribution, ai_attribution, active_action, last_peace_event_at) "
        "VALUES(1,?,?,?,?,?,?,?,?,?,?,?,NULL)",
        (mood, intensity, now, "analysis",
         "neutral", 0.0, now, "analysis",
         "none", "none", None),
    )


def test_mood_fast_slot_registered():
    from infrastructure.provider_modality import SLOT_MODALITY, slot_modality
    from infrastructure.provider_registry import TASK_SLOTS
    assert "mood_fast" in TASK_SLOTS
    slot = TASK_SLOTS["mood_fast"]
    assert slot.fallback == ("agent", "chat")
    assert slot.lightweight is True
    assert slot_modality("mood_fast") == "text"
    assert SLOT_MODALITY["mood_fast"] == "text"


def test_lexicon_detect_strong_anger():
    hit = detect_lexicon("气死了！！又全毁了")
    assert hit is not None
    assert hit["mood"] == "angry"
    assert hit["intensity"] >= 0.8
    assert hit["confidence"] >= 0.8
    assert hit["source"] == "lexicon"
    assert detect_lexicon("今天天气怎么样") is None


def test_fuse_display_pulse_inertia_and_spike(tmp_path: Path):
    db = _db(tmp_path)
    _seed_user(db, mood="angry", intensity=0.8)
    mood = MoodManager(db, _Cfg())

    # 低置信 → 用 S
    low = mood.fuse_display_pulse({
        "mood": "joy", "intensity": 0.9, "confidence": 0.2, "source": "flash"})
    assert low["mood"] == "angry"
    assert low["intensity"] >= 0.7

    # 尖峰：P 明显更高
    spike = mood.fuse_display_pulse({
        "mood": "frustrated", "intensity": 0.95, "confidence": 0.9,
        "source": "flash"})
    assert spike["mood"] == "frustrated"
    assert spike["intensity"] == pytest.approx(0.95)

    # 惯性：P 近中性、S 仍高
    inert = mood.fuse_display_pulse({
        "mood": "neutral", "intensity": 0.0, "confidence": 0.9,
        "source": "flash"})
    assert inert["mood"] == "angry"
    # β=0.35 → 0.35*0 + 0.65*0.8 = 0.52
    assert 0.45 <= inert["intensity"] <= 0.7


def test_detect_user_pulse_thinking_off_and_lexicon_provider():
    captured: dict = {}

    class _LLM:
        async def chat(self, _snap, _prompt, **kwargs):
            captured.update(kwargs)
            return {
                "content": '{"mood":"angry","intensity":0.82,"confidence":0.91}'}

    class _Providers:
        def snapshot_for(self, _slot):
            return object()

    async def run():
        pulse = await detect_user_pulse(
            _LLM(), _Providers(), _Cfg(),
            user_message="气死了", session_id="s1")
        assert pulse["source"] == "flash"
        assert pulse["mood"] == "angry"
        assert captured.get("source") == "mood_fast"
        assert captured.get("json_mode") is True
        assert captured.get("extra_body") == {"thinking_enabled": False}

        lex = await detect_user_pulse(
            _LLM(), _Providers(),
            _Cfg(mood_fast_path_provider="lexicon"),
            user_message="气死了！！全毁了")
        assert lex["source"] == "lexicon"
        assert lex["mood"] == "angry"

    asyncio.run(run())


def test_detect_user_pulse_timeout_falls_back_to_lexicon():
    class _SlowLLM:
        async def chat(self, *_a, **_k):
            await asyncio.sleep(2.0)
            return {"content": '{"mood":"joy","intensity":1,"confidence":1}'}

    class _Providers:
        def snapshot_for(self, _slot):
            return object()

    async def run():
        pulse = await detect_user_pulse(
            _SlowLLM(), _Providers(),
            _Cfg(mood_fast_path_timeout_ms=100),
            user_message="气死了！！又全毁了")
        assert pulse["source"] == "lexicon"
        assert pulse["mood"] == "angry"

    asyncio.run(run())


def test_peace_event_and_natural_decline_smoke(tmp_path: Path):
    assert detect_peace_event("对不起，刚才太冲了") == "user_apology"
    assert detect_peace_event("ok", "是我搞错了，抱歉") == "ai_admission"

    db = _db(tmp_path)
    _seed_user(db, mood="angry", intensity=0.9)
    mood = MoodManager(db, _Cfg())
    result = mood.apply_v2(
        user_res={"mood": "angry", "intensity": 0.5, "confidence": 0.8,
                  "attribution": "other"},
        ai_res={"mood": "anxious", "intensity": 0.6, "confidence": 0.7,
                "attribution": "self"},
        peace_event="user_apology",
    )
    assert result["peace_event_applied"] is True
    # user_apology：AI 负面侧 → relieved
    assert result["ai_mood"] == "relieved"

    db.execute(
        "INSERT INTO sessions(session_id, title) VALUES(?,?)", ("sess_nd", "t"))
    # 写入足够中性历史以满足 natural_decline 门槛
    from infrastructure.timeutil import now_cst
    now = now_cst().isoformat(timespec="seconds")
    for _ in range(4):
        db.execute(
            "INSERT INTO mood_history(scope,mood,intensity,source,note,create_time) "
            "VALUES(?,?,?,?,?,?)",
            ("user", "neutral", 0.0, "analysis", "", now))
        db.execute(
            "INSERT INTO mood_history(scope,mood,intensity,source,note,create_time) "
            "VALUES(?,?,?,?,?,?)",
            ("ai", "neutral", 0.0, "analysis", "", now))
    before = db.query_one("SELECT user_intensity FROM mood_state WHERE id=1")
    mood.natural_decline("sess_nd")
    after = db.query_one("SELECT user_intensity FROM mood_state WHERE id=1")
    # 有中性历史且无 trigger 时强度应进一步下降（或至少不升高）
    assert after["user_intensity"] <= before["user_intensity"]
