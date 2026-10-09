"""词典/标点快信号 —— 超时或 provider=lexicon 时的降级通道。

不发起 LLM；无显著信号时返回 None。标签仅为七情。
"""
from __future__ import annotations

# 强负面 / 正面 / 焦虑关键词（中文硬信号）→ 七情
_ANGER = (
    "气死", "愤怒", "生气", "混蛋", "去死", "烦死", "受够", "全毁了",
    "太过分", "恼火", "火大", "暴怒", "该死", "垃圾",
    "受不了", "崩溃", "无语", "搞砸", "又失败", "又挂了", "白费",
    "白干", "白忙", "失望透顶",
)
_SAD = (
    "难过", "伤心", "心痛", "哭了", "想哭", "绝望", "无助", "心碎",
)
_FEAR = (
    "焦虑", "好慌", "慌死", "担心死", "紧张死", "不安", "害怕",
    "恐惧", "心里没底",
)
_DISGUST = (
    "恶心", "厌恶", "讨厌死", "嫌弃", "反感", "恶心死",
)
_LOVE = (
    "爱你", "喜欢你", "好想你", "抱抱", "心疼你",
)
_DESIRE = (
    "好想搞清楚", "必须搞定", "我偏要", "太好奇了",
)
_JOY = (
    "太棒了", "太好了", "开心", "高兴", "谢谢你", "感动",
    "太赞了", "完美", "厉害", "牛逼", "真棒", "好开心",
)


def detect_lexicon(user_message: str) -> dict | None:
    """从用户消息提取强情绪信号。

    返回 {mood, intensity, confidence, source="lexicon"}；无信号返回 None。
    """
    text = (user_message or "").strip()
    if not text:
        return None

    lower = text.lower()
    excl = text.count("!") + text.count("！")
    excl_boost = min(0.2, excl * 0.05)

    def _hit(words: tuple[str, ...]) -> bool:
        return any(w in text or w in lower for w in words)

    mood: str | None = None
    intensity = 0.0
    confidence = 0.0

    if _hit(_ANGER):
        mood, intensity, confidence = "anger", 0.85, 0.9
    elif _hit(_DISGUST):
        mood, intensity, confidence = "disgust", 0.75, 0.85
    elif _hit(_SAD):
        mood, intensity, confidence = "sorrow", 0.7, 0.8
    elif _hit(_FEAR):
        mood, intensity, confidence = "fear", 0.7, 0.8
    elif _hit(_LOVE):
        mood, intensity, confidence = "love", 0.7, 0.8
    elif _hit(_DESIRE):
        mood, intensity, confidence = "desire", 0.7, 0.8
    elif _hit(_JOY):
        mood, intensity, confidence = "joy", 0.7, 0.8

    # 纯感叹密度：无关键词时弱提示（仍可能 None）
    if mood is None and excl >= 3 and len(text) <= 40:
        mood, intensity, confidence = "anger", 0.55, 0.55

    if mood is None:
        return None

    intensity = max(0.0, min(1.0, intensity + excl_boost))
    confidence = max(0.0, min(1.0, confidence + excl_boost * 0.5))
    return {
        "mood": mood,
        "intensity": round(intensity, 4),
        "confidence": round(confidence, 4),
        "source": "lexicon",
    }
