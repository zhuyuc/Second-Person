"""
情绪管理器（产品文档 §情绪模块 / 双源：用户情绪 + AI 自身情绪）。

- 定位：纯状态管理器——存储/衰减/融合/注入文本生成，不发起任何 LLM 调用
  （情感判定由 AgentCore 在 turn 完成后异步发起，结果经 apply() 落库）
- 状态：mood_state 全局单例行（id=1）+ mood_history 变化留痕
- 生命周期：生成（apply 首次写入）→ 更新（平滑融合防跳变）→ 消失（惰性衰减）
- 衰减：惰性计算（半衰期 mood_decay_hours，默认 2h），无定时任务；
  有效强度 ≤ DECAY_FLOOR 视为 neutral，不注入
- 融合：β = 0.4×置信度 + 0.3×min(1, 距上次更新/30min) + 0.3；
  当轮判定为 neutral 时旧情绪仅向基线回落（不覆盖）
- 情绪标签自由文本存储，无枚举约束（全放开设计）
"""
from __future__ import annotations

import logging
from datetime import datetime

from infrastructure.prompt_loader import PROMPTS
from infrastructure.timeutil import now_cst

logger = logging.getLogger("second_person.mood")

DECAY_FLOOR = 0.05     # 有效强度低于该值视为 neutral（不注入）
_FUSE_TIME_WINDOW = 30 * 60  # 融合时间因子满窗（秒）

# 情绪标签中文化映射：mood_state 存英文标签（判定/存储稳定），注入前翻译为
# 中文——避免英文标签进入 system prompt 形成英文语言环境、诱导模型英文推理
MOOD_CN = {
    "neutral": "平静", "joy": "喜悦", "pleased": "满足", "excited": "兴奋",
    "warm": "温暖", "grateful": "感激", "angry": "愤怒", "irritated": "烦躁",
    "frustrated": "受挫", "sad": "悲伤", "melancholy": "忧郁",
    "compassionate": "怜惜", "fearful": "不安", "anxious": "焦虑",
    "cautious": "谨慎", "affectionate": "喜爱", "caring": "关怀",
    "trusting": "信任", "disgusted": "厌恶", "disdainful": "鄙夷",
    "curious": "好奇", "aspiring": "进取", "competitive": "好胜",
    "hopeful": "满怀希望", "lonely": "孤独", "proud": "自豪",
    "relieved": "如释重负", "guilty": "愧疚", "ashamed": "羞愧",
    "surprised": "惊讶", "confused": "困惑", "bored": "无聊",
    "tired": "疲惫", "eager": "渴望", "determined": "坚定",
    "playful": "玩味", "sarcastic": "嘲讽", "calm": "沉稳",
    "composed": "沉着", "humble": "谦逊", "concerned": "关切",
}


def _mood_cn(label: str) -> str:
    """英文情绪标签 → 中文（未知标签原样保留，不丢信息）。"""
    return MOOD_CN.get(str(label).strip().lower(), str(label))


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


class MoodManager:
    def __init__(self, db, config):
        self.db = db
        self.config = config

    # ---- 读取与注入 --------------------------------------------------------
    def build_hint(self) -> str:
        """生成情绪注入段（system prompt 追加用）。

        收敛式理解优化方案 §3.1：情绪永远在场。即使 neutral/低强度也返回
        轻量提示，强度决定浓淡——不再用空串做"跳过情绪"的开关。
        """
        if not self.config.get("mood_enabled", True):
            return ""
        if self.config.get("mood_influence_strength", 0.5) <= 0:
            return ""
        row = self.db.query_one("SELECT * FROM mood_state WHERE id=1")
        if not row:
            return ""
        kwargs: dict[str, object] = {}
        any_active = False
        for scope in ("user", "ai"):
            mood = row[f"{scope}_mood"]
            intensity = self._decay(row[f"{scope}_intensity"],
                                    row[f"{scope}_updated_at"])
            if mood != "neutral" and intensity > DECAY_FLOOR:
                kwargs[f"{scope}_mood"] = _mood_cn(mood)
                kwargs[f"{scope}_intensity"] = round(intensity, 2)
                kwargs[f"{scope}_time_hint"] = self._time_hint(
                    row[f"{scope}_updated_at"])
                any_active = True
            else:
                # 收敛式优化：低强度时仍注入"平静"标签，保证情绪始终在场
                kwargs[f"{scope}_mood"] = "平静"
                kwargs[f"{scope}_intensity"] = 0
                kwargs[f"{scope}_time_hint"] = ""
        # 即使双方都是"平静"也返回注入段（低强度轻调制），不再返回空串跳过
        return PROMPTS.render("agent/prompts/mood", **kwargs)

    # ---- 更新（融合 + 落库 + 留痕） ----------------------------------------
    def apply(self, user_res: dict, ai_res: dict) -> dict:
        """应用一轮情感判定结果。返回 {scope}_changed/{scope}_mood/... 供事件广播。"""
        self._ensure_row()
        row = self.db.query_one("SELECT * FROM mood_state WHERE id=1")
        now = now_cst().isoformat(timespec="seconds")
        out: dict = {"user_changed": False, "ai_changed": False}
        for scope, res in (("user", user_res), ("ai", ai_res)):
            fresh_mood = str(res.get("mood") or "neutral").strip() or "neutral"
            fresh_intensity = _clamp01(float(res.get("intensity") or 0.0))
            confidence = _clamp01(float(res.get("confidence") or 0.0))
            old_mood = row[f"{scope}_mood"]
            old_intensity = self._decay(row[f"{scope}_intensity"],
                                        row[f"{scope}_updated_at"])
            if old_intensity < DECAY_FLOOR:
                old_mood, old_intensity = "neutral", 0.0
            new_mood, new_intensity = self._fuse(
                old_mood, old_intensity, fresh_mood, fresh_intensity,
                confidence, row[f"{scope}_updated_at"])
            changed = new_mood != old_mood \
                or abs(new_intensity - old_intensity) > 0.01
            self.db.execute(
                f"UPDATE mood_state SET {scope}_mood=?, {scope}_intensity=?, "
                f"{scope}_updated_at=?, {scope}_source=? WHERE id=1",
                (new_mood, round(new_intensity, 4), now,
                 "analysis" if fresh_intensity > 0 else "decay"))
            if changed:
                self.db.execute(
                    "INSERT INTO mood_history(scope,mood,intensity,source,"
                    "note,create_time) VALUES(?,?,?,?,?,?)",
                    (scope, new_mood, round(new_intensity, 4), "analysis",
                     str(res.get("note") or "")[:200], now))
            out[f"{scope}_changed"] = changed
            out[f"{scope}_mood"] = new_mood
            out[f"{scope}_intensity"] = round(new_intensity, 4)
        return out

    def reset(self, scope: str | None = None) -> None:
        """重置指定（'user'/'ai'）或全部情绪回 neutral 基线。"""
        self._ensure_row()
        now = now_cst().isoformat(timespec="seconds")
        for s in ("user", "ai"):
            if scope and s != scope:
                continue
            self.db.execute(
                f"UPDATE mood_state SET {s}_mood='neutral', {s}_intensity=0, "
                f"{s}_updated_at=?, {s}_source='reset' WHERE id=1", (now,))

    # ---- 内部：衰减 / 融合 / 时间提示 --------------------------------------
    def _decay(self, intensity: float, updated_at: str | None) -> float:
        """惰性衰减：intensity × 0.5^(dt/半衰期)。"""
        half = float(self.config.get("mood_decay_hours", 2.0) or 2.0)
        if half <= 0:
            half = 2.0
        try:
            dt = (now_cst() - datetime.fromisoformat(updated_at)).total_seconds()
        except (ValueError, TypeError):
            return 0.0
        if dt <= 0:
            return _clamp01(intensity)
        return _clamp01(intensity * 0.5 ** (dt / (half * 3600)))

    @staticmethod
    def _fuse(old_mood: str, old_intensity: float, fresh_mood: str,
              fresh_intensity: float, confidence: float,
              old_updated_at: str | None) -> tuple[str, float]:
        """平滑融合：标签取有效强度高者；fresh=neutral 时仅向基线回落。"""
        if fresh_mood == "neutral" or fresh_intensity <= 0:
            if old_mood == "neutral":
                return "neutral", 0.0
            # 最小回落 20% 保证不卡住（即使置信度为 0 也渐进回落）
            return old_mood, max(0.0, old_intensity * (1 - 0.2 - 0.4 * confidence))
        try:
            dt = max(0.0, (now_cst() - datetime.fromisoformat(
                old_updated_at)).total_seconds())
        except (ValueError, TypeError):
            dt = _FUSE_TIME_WINDOW
        beta = _clamp01(0.4 * confidence
                        + 0.3 * min(1.0, dt / _FUSE_TIME_WINDOW) + 0.3)
        new_intensity = old_intensity * (1 - beta) + fresh_intensity * beta
        new_mood = fresh_mood if fresh_intensity >= old_intensity else old_mood
        return new_mood, new_intensity

    @staticmethod
    def _time_hint(updated_at: str | None) -> str:
        try:
            dt = max(0.0, (now_cst() - datetime.fromisoformat(
                updated_at)).total_seconds())
        except (ValueError, TypeError):
            return ""
        if dt < 60:
            return "，刚刚更新"
        if dt < 3600:
            return f"，约 {int(dt // 60)} 分钟前"
        return f"，约 {int(dt // 3600)} 小时前"

    def _ensure_row(self) -> None:
        now = now_cst().isoformat(timespec="seconds")
        self.db.execute(
            "INSERT OR IGNORE INTO mood_state(id,user_mood,user_intensity,"
            "user_updated_at,ai_mood,ai_intensity,ai_updated_at) "
            "VALUES(1,'neutral',0,?, 'neutral',0,?)", (now, now))
