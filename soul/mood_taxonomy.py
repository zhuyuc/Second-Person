"""七情情绪标签（喜怒哀惧爱恶欲）+ 强度档 + 旧标签迁移。

权威集合：neutral + 七情。历史细标签经 LEGACY_MOOD_MAP 归一后入库/注入。
"""
from __future__ import annotations

# 七情 + 平静（存储用英文 key，注入用中文）
CANONICAL_MOODS: frozenset[str] = frozenset({
    "neutral", "joy", "anger", "sorrow", "fear", "love", "disgust", "desire",
})

MOOD_CN: dict[str, str] = {
    "neutral": "平静",
    "joy": "喜",
    "anger": "怒",
    "sorrow": "哀",
    "fear": "惧",
    "love": "爱",
    "disgust": "恶",
    "desire": "欲",
}

MOOD_CN_REVERSE: dict[str, str] = {v: k for k, v in MOOD_CN.items()}

# System One choice 的 criteria（中文说明，便于本地决策模型理解）
MOOD_CHOICE_CRITERIA: dict[str, str] = {
    "neutral": "无明显情绪，平静或中性陈述",
    "joy": "喜：喜悦、高兴、满足、兴奋、感激、自豪、如释重负等",
    "anger": "怒：愤怒、烦躁、受挫、愤慨、恼火等",
    "sorrow": "哀：悲伤、忧郁、受伤、孤独、愧疚、疲惫、怜惜等",
    "fear": "惧：害怕、焦虑、担忧、谨慎、警觉、困惑、防御等",
    "love": "爱：喜爱、关怀、信任、亲昵、温暖亲近等",
    "disgust": "恶：厌恶、鄙夷、嫌弃、反感等",
    "desire": "欲：好奇、渴望、进取、好胜、坚定想做成某事等",
}

# Jev score 三档（与展示「高/中/低」对齐）
INTENSITY_SCORE_LEVELS: tuple[str, ...] = ("低", "中", "高")
# 各档代表强度（落在 _intensity_band 的低/中/高区间中位附近）
INTENSITY_FROM_LEVEL_INDEX: tuple[float, ...] = (0.15, 0.35, 0.75)

NEGATIVE_MOODS: frozenset[str] = frozenset({
    "anger", "sorrow", "fear", "disgust",
})
POSITIVE_MOODS: frozenset[str] = frozenset({
    "joy", "love", "desire",
})
# 归因 other 时不向对方传染的对立负面
CONTAGION_BLOCKED_MOODS: frozenset[str] = frozenset({
    "anger", "disgust",
})
# 寒暄轮弱化的「自我负面」（七情下归入哀）
SELF_NEGATIVE_MOODS: frozenset[str] = frozenset({"sorrow"})

# 旧细标签 / 别名 → 七情
LEGACY_MOOD_MAP: dict[str, str] = {
    "neutral": "neutral",
    "joy": "joy", "pleased": "joy", "excited": "joy", "warm": "joy",
    "grateful": "joy", "proud": "joy", "relieved": "joy", "hopeful": "joy",
    "eager": "joy", "playful": "joy", "calm": "joy", "composed": "joy",
    "peaceful": "joy", "surprised": "joy",
    "angry": "anger", "irritated": "anger", "frustrated": "anger",
    "indignant": "anger", "sarcastic": "anger",
    "sad": "sorrow", "melancholy": "sorrow", "hurt": "sorrow",
    "lonely": "sorrow", "remorseful": "sorrow", "apologetic": "sorrow",
    "ashamed": "sorrow", "guilty": "sorrow", "self_critical": "sorrow",
    "tired": "sorrow", "bored": "sorrow", "compassionate": "sorrow",
    "fearful": "fear", "anxious": "fear", "cautious": "fear", "wary": "fear",
    "defensive": "fear", "concerned": "fear", "confused": "fear",
    "humble": "fear",
    "affectionate": "love", "caring": "love", "trusting": "love",
    "disgusted": "disgust", "disdainful": "disgust",
    "curious": "desire", "aspiring": "desire", "competitive": "desire",
    "determined": "desire",
    # 已是七情 key
    "anger": "anger", "sorrow": "sorrow", "fear": "fear",
    "love": "love", "disgust": "disgust", "desire": "desire",
    # 中文
    "平静": "neutral", "喜": "joy", "怒": "anger", "哀": "sorrow",
    "惧": "fear", "爱": "love", "恶": "disgust", "欲": "desire",
    "喜悦": "joy", "愤怒": "anger", "悲伤": "sorrow", "厌恶": "disgust",
}


def normalize_mood(label: str | None, *, default: str = "neutral") -> str:
    """任意标签 → 七情 canonical；未知回退 default。"""
    raw = str(label or "").strip().lower()
    if not raw:
        return default
    if raw in CANONICAL_MOODS:
        return raw
    mapped = LEGACY_MOOD_MAP.get(raw) or LEGACY_MOOD_MAP.get(str(label or "").strip())
    if mapped in CANONICAL_MOODS:
        return mapped
    # 中文原文（未 lower 的）
    cn = MOOD_CN_REVERSE.get(str(label or "").strip())
    if cn in CANONICAL_MOODS:
        return cn
    return default


def mood_cn(label: str | None) -> str:
    """英文七情 → 中文；未知原样。"""
    key = normalize_mood(label, default="")
    if key:
        return MOOD_CN.get(key, str(label))
    return str(label or "")


def intensity_from_score(score: float | int | None,
                         n_levels: int | None = None) -> float:
    """Jev score（0-index 期望档）→ [0,1] 强度。"""
    levels = n_levels or len(INTENSITY_SCORE_LEVELS)
    try:
        s = float(score if score is not None else 0.0)
    except (TypeError, ValueError):
        s = 0.0
    if levels <= 1:
        return INTENSITY_FROM_LEVEL_INDEX[0]
    idx = int(round(s))
    idx = max(0, min(levels - 1, idx))
    if idx < len(INTENSITY_FROM_LEVEL_INDEX):
        return INTENSITY_FROM_LEVEL_INDEX[idx]
    return max(0.0, min(1.0, idx / (levels - 1)))


def clamp01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))
