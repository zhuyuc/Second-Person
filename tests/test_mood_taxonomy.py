"""七情标签归一与 System One 解析。"""
from __future__ import annotations

from soul.mood_taxonomy import (
    CANONICAL_MOODS, intensity_from_score, normalize_mood,
)
from infrastructure.system_one.client import parse_mood_answers


def test_seven_emotions_canonical():
    assert CANONICAL_MOODS == {
        "neutral", "joy", "anger", "sorrow", "fear", "love", "disgust", "desire",
    }
    assert normalize_mood("喜") == "joy"
    assert normalize_mood("恶") == "disgust"
    assert normalize_mood("angry") == "anger"
    assert normalize_mood("frustrated") == "anger"
    assert normalize_mood("anxious") == "fear"
    assert normalize_mood("curious") == "desire"
    assert normalize_mood("自省") == "neutral"  # 非七情，回退


def test_intensity_from_score_bands():
    assert intensity_from_score(0) == 0.15
    assert intensity_from_score(1) == 0.35
    assert intensity_from_score(2) == 0.75


def test_parse_mood_answers_system_one():
    parsed = parse_mood_answers({
        "answers": {
            "mood": {
                "type": "choice",
                "choice": "anger",
                "probabilities": {"anger": 0.8, "joy": 0.1},
                "confidence": 0.9,
            },
            "intensity": {
                "type": "score",
                "score": 2.0,
                "confidence": 0.7,
            },
        },
    })
    assert parsed is not None
    assert parsed["mood"] == "anger"
    assert parsed["intensity"] == 0.75
    assert parsed["confidence"] == 0.7
