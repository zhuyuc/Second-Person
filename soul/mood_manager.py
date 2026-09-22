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
from datetime import datetime, timedelta

from infrastructure.prompt_loader import PROMPTS
from infrastructure.timeutil import now_cst

from . import _mood_constants as _mood

logger = logging.getLogger("second_person.mood")

DECAY_FLOOR = 0.05     # 有效强度低于该值视为 neutral（不注入）
_FUSE_TIME_WINDOW = 30 * 60  # 融合时间因子满窗（秒）

# 情绪标签中文化映射：mood_state 存英文标签（判定/存储稳定），注入前翻译为
# 中文——避免英文标签进入模型上下文形成英文语言环境、诱导模型英文推理
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
    # v2 新增（双向情绪系统：归因/传染/平复用标签）
    "indignant": "愤慨", "hurt": "受伤", "remorseful": "懊悔",
    "apologetic": "歉意", "defensive": "防御", "self_critical": "自责",
    "peaceful": "安宁", "wary": "警觉",
}

MOOD_CN_REVERSE = {v: k for k, v in MOOD_CN.items()}


# v2 情绪分类常量（传染算法 + 平复事件 + 主动行为判定用）
NEGATIVE_MOODS = {"angry", "irritated", "frustrated", "indignant", "hurt",
                  "sad", "melancholy", "anxious", "ashamed", "defensive",
                  "remorseful", "apologetic", "self_critical", "fearful",
                  "disgusted", "disdainful", "guilty", "lonely", "bored",
                  "tired", "confused", "wary"}

POSITIVE_MOODS = {"joy", "pleased", "excited", "warm", "grateful", "proud",
                  "affectionate", "curious", "eager", "relieved", "hopeful",
                  "determined", "playful", "calm", "composed", "trusting",
                  "caring", "compassionate", "aspiring", "competitive",
                  "humble", "peaceful"}

# 对立面情绪：归因 other 时对方负面不传染（对方觉得是你的问题，
# 不会让你也同样愤怒），己方独立走防御路径
CONTAGION_BLOCKED_MOODS = {"angry", "irritated", "frustrated", "indignant",
                           "disgusted", "disdainful"}


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
    def build_rules(self) -> str:
        """稳定的情绪表达规则（进 system 静态前缀，跨轮字节不变）。"""
        if not self.config.get("mood_enabled", True):
            return ""
        if float(self.config.get("mood_influence_strength", 0.5)) <= 0:
            return ""
        try:
            return PROMPTS.load_raw("agent/prompts/mood_rules").strip()
        except Exception:  # noqa: BLE001
            logger.debug("加载 mood_rules 失败", exc_info=True)
            return ""

    def get_decayed_user_state(self) -> dict:
        """库内用户态 S（读取时惰性衰减）；无行则中性。"""
        self._ensure_row()
        row = self.db.query_one("SELECT * FROM mood_state WHERE id=1")
        intensity = self._decay(row["user_intensity"], row["user_updated_at"])
        mood = row["user_mood"] or "neutral"
        if intensity < DECAY_FLOOR:
            mood, intensity = "neutral", 0.0
        return {
            "mood": mood,
            "intensity": intensity,
            "attribution": row["user_attribution"] or "none",
            "updated_at": row["user_updated_at"],
        }

    def get_decayed_ai_state(self) -> dict:
        """库内 AI 态（读取时惰性衰减）；注入与主动行为用。"""
        self._ensure_row()
        row = self.db.query_one("SELECT * FROM mood_state WHERE id=1")
        intensity = self._decay(row["ai_intensity"], row["ai_updated_at"])
        mood = row["ai_mood"] or "neutral"
        if intensity < DECAY_FLOOR:
            mood, intensity = "neutral", 0.0
        return {
            "mood": mood,
            "intensity": intensity,
            "attribution": row["ai_attribution"] or "none",
            "updated_at": row["ai_updated_at"],
        }

    def fuse_display_pulse(self, pulse: dict | None) -> dict:
        """本轮展示融合 S ⊕ P → D（不写 mood_state）。

        规则（产品方案 §5）：
        1. 低置信 → D = S
        2. P 强度明显高于 S → 标签 P，intensity = max(S, P)
        3. P 近中性而 S 仍高 → intensity = β·P + (1-β)·S，标签保持 S
        4. 其它：标签/强度取 P
        """
        s = self.get_decayed_user_state()
        if not pulse or pulse.get("source") == "state":
            return dict(s)
        try:
            confidence = _clamp01(float(pulse.get("confidence") or 0.0))
        except (TypeError, ValueError):
            return dict(s)
        min_conf = float(
            self.config.get("mood_fast_path_min_confidence", 0.55) or 0.55)
        if confidence < min_conf:
            return dict(s)

        p_mood = str(pulse.get("mood") or "neutral").strip().lower() or "neutral"
        try:
            p_int = _clamp01(float(pulse.get("intensity") or 0.0))
        except (TypeError, ValueError):
            return dict(s)
        s_mood, s_int = s["mood"], float(s["intensity"])

        # 尖峰：P 明显更高
        if p_int - s_int >= _mood.PULSE_SPIKE_DELTA:
            return {
                **s,
                "mood": p_mood,
                "intensity": max(s_int, p_int),
            }

        # 惯性：P 近中性、S 仍高
        p_near_neutral = (p_mood == "neutral" or p_int <= DECAY_FLOOR + 0.05)
        if p_near_neutral and s_int >= 0.3 and s_mood != "neutral":
            beta = _mood.PULSE_INERTIA_BETA
            return {
                **s,
                "mood": s_mood,
                "intensity": _clamp01(beta * p_int + (1.0 - beta) * s_int),
            }

        return {**s, "mood": p_mood, "intensity": p_int}

    def build_state_context(self, user_message: str | None = None,
                            pulse: dict | None = None) -> str:
        """本轮情绪状态（进 messages 尾部 context.mood，允许每轮变化）。

        - strength 调制：根据 mood_influence_strength 调整注入浓度
        - 强度用高/中/低档，避免两位小数与时间文案污染 system 前缀
        - baseline 风味：即使 neutral 也根据历史关系给出有温度的基线描述
        - attribution 提示：告知 AI 当前情绪来源（self/other/shared）
        - 寒暄/无承接的自我歉意：仅弱化本轮展示文案，不改 DB 真值
        - pulse：本轮快路径脉冲；用户侧用 fuse_display_pulse，AI 侧只用库内衰减态
        """
        if not self.config.get("mood_enabled", True):
            return ""
        strength = float(self.config.get("mood_influence_strength", 0.5))
        if strength <= 0:
            return ""
        row = self.db.query_one("SELECT * FROM mood_state WHERE id=1")
        if not row:
            return ""

        from soul.mood_turn_policy import (
            is_brief_social_turn, should_soften_self_negative,
        )
        brief = is_brief_social_turn(user_message)
        soften_self = should_soften_self_negative(
            ai_mood=row["ai_mood"] or "",
            ai_attribution=row["ai_attribution"] or "",
            user_message=user_message,
        )

        # 寒暄轮：表达档位压到「轻微」；情绪标签仍可保留（再经 soften 处理）
        if brief:
            strength_hint = "情绪表达轻微暗示即可（本轮为极短寒暄）"
        else:
            strength_hint = self._strength_hint(strength)

        display_user = self.fuse_display_pulse(pulse)
        ai_state = self.get_decayed_ai_state()

        kwargs: dict[str, object] = {"strength_hint": strength_hint}
        scope_data = {
            "user": (display_user["mood"], display_user["intensity"],
                     display_user.get("updated_at") or row["user_updated_at"]),
            "ai": (ai_state["mood"], ai_state["intensity"],
                   ai_state.get("updated_at") or row["ai_updated_at"]),
        }
        for scope in ("user", "ai"):
            mood, raw_intensity, updated_at = scope_data[scope]
            adjusted = raw_intensity * strength
            use_baseline = False
            if scope == "ai" and soften_self:
                use_baseline = True
            if mood != "neutral" and adjusted > DECAY_FLOOR and not use_baseline:
                kwargs[f"{scope}_mood"] = _mood_cn(mood)
                band = self._intensity_band(adjusted)
                if brief:
                    band = "低"
                kwargs[f"{scope}_intensity_band"] = band
                kwargs[f"{scope}_time_hint"] = (
                    "" if brief else self._time_hint(updated_at))
            else:
                kwargs[f"{scope}_mood"] = self._baseline_flavor(scope)
                kwargs[f"{scope}_intensity_band"] = "低"
                kwargs[f"{scope}_time_hint"] = ""

        if soften_self or brief:
            # 寒暄或弱化自我歉意时，不强调「来自对自己表现的评估」以免诱发道歉独白
            kwargs["ai_attribution_hint"] = ""
        else:
            kwargs["ai_attribution_hint"] = self._attribution_hint(
                row["ai_attribution"] or "")
        return PROMPTS.render("agent/prompts/mood_state", **kwargs)

    def build_hint(self) -> str:
        """兼容旧调用点：等价于本轮状态上下文（不再进入 system）。"""
        return self.build_state_context()

    @staticmethod
    def _intensity_band(intensity: float) -> str:
        if intensity >= 0.5:
            return "高"
        if intensity >= 0.2:
            return "中"
        return "低"

    @staticmethod
    def _strength_hint(strength: float) -> str:
        if strength >= 0.8:
            return "情绪表达可以充分外显"
        if strength >= 0.4:
            return "情绪表达适度流露"
        return "情绪表达轻微暗示即可"

    @staticmethod
    def _attribution_hint(attribution: str) -> str:
        return {
            "self": "本次情绪主要来自你对自己表现的评估",
            "other": "本次情绪主要来自对用户当前行为的感受",
            "shared": "本次情绪来自双方互动过程",
        }.get(attribution, "")

    def _baseline_flavor(self, scope: str) -> str:
        """根据历史情绪记录给出有温度的基线描述。"""
        warm_since = (now_cst() - timedelta(days=7)
                      ).isoformat(timespec="seconds")
        warm_count = self.db.query_one(
            "SELECT count(*) c FROM mood_history "
            "WHERE scope=? AND mood IN ('warm','affectionate','trusting','grateful') "
            "AND create_time > ?", (scope, warm_since))["c"]
        if warm_count >= _mood.BASELINE_WARM_THRESHOLD:
            return "平静但有暖意"
        curious_since = (now_cst() - timedelta(days=1)
                         ).isoformat(timespec="seconds")
        curious_count = self.db.query_one(
            "SELECT count(*) c FROM mood_history "
            "WHERE scope=? AND mood IN ('curious','eager','aspiring') "
            "AND create_time > ?", (scope, curious_since))["c"]
        if curious_count >= _mood.BASELINE_CURIOUS_THRESHOLD:
            return "平静而好奇"
        return "平静"

    # ---- 更新（融合 + 落库 + 留痕） ----------------------------------------
    def apply(self, user_res: dict, ai_res: dict) -> dict:
        """v1 兼容入口：委托 v2，归因默认 none，无平复事件。
        现有调用点无需修改，_update_mood v2 直接调用 apply_v2 获得完整能力。"""
        return self.apply_v2(
            user_res={**user_res,
                      "attribution": user_res.get("attribution", "none")},
            ai_res={
                **ai_res, "attribution": ai_res.get("attribution", "none")},
            peace_event="none",
        )

    def apply_v2(self, user_res: dict, ai_res: dict,
                 peace_event: str = "none") -> dict:
        """v2 情绪应用：融合 → 传染 → 平复事件 → 落库。
        返回 {scope}_changed/{scope}_mood/{scope}_intensity/{scope}_attribution
        + peace_event_applied。"""
        self._ensure_row()
        row = self.db.query_one("SELECT * FROM mood_state WHERE id=1")
        now = now_cst().isoformat(timespec="seconds")

        new_moods = {}
        for scope, res in (("user", user_res), ("ai", ai_res)):
            fresh_mood = str(res.get("mood") or "neutral").strip() or "neutral"
            fresh_intensity = _clamp01(float(res.get("intensity") or 0.0))
            confidence = _clamp01(float(res.get("confidence") or 0.0))
            old_mood = row[f"{scope}_mood"]
            old_intensity = self._decay(
                row[f"{scope}_intensity"], row[f"{scope}_updated_at"])
            if old_intensity < DECAY_FLOOR:
                old_mood, old_intensity = "neutral", 0.0
            new_mood, new_intensity = self._fuse(
                old_mood, old_intensity, fresh_mood, fresh_intensity,
                confidence, row[f"{scope}_updated_at"])
            new_moods[scope] = {
                "mood": new_mood, "intensity": new_intensity,
                "attribution": res.get("attribution", "none"),
                "confidence": confidence, "note": res.get("note", ""),
            }

        # 传染
        new_moods = self._apply_contagion(new_moods, _mood.CONTAGION_FACTOR)

        # 平复事件
        peace_applied = False
        if peace_event != "none":
            new_moods = self._apply_peace_event(new_moods, peace_event)
            peace_applied = True
            self.db.execute(
                "UPDATE mood_state SET last_peace_event_at=? WHERE id=1", (now,))

        # 落库
        out = {"user_changed": False, "ai_changed": False,
               "peace_event_applied": peace_applied}
        for scope in ("user", "ai"):
            nm = new_moods[scope]
            old_mood = row[f"{scope}_mood"]
            changed = (nm["mood"] != old_mood
                       or abs(nm["intensity"] - row[f"{scope}_intensity"]) > 0.01)
            self.db.execute(
                f"UPDATE mood_state SET {scope}_mood=?, {scope}_intensity=?, "
                f"{scope}_updated_at=?, {scope}_source=?, {scope}_attribution=? "
                f"WHERE id=1",
                (nm["mood"], round(nm["intensity"], 4), now,
                 "analysis" if nm.get("confidence", 0) > 0 else "decay",
                 nm["attribution"]))
            if changed:
                self.db.execute(
                    "INSERT INTO mood_history(scope,mood,intensity,source,"
                    "note,create_time) VALUES(?,?,?,?,?,?)",
                    (scope, nm["mood"], round(nm["intensity"], 4), "analysis",
                     f"attr={nm['attribution']};peace={peace_event};{nm.get('note', '')[:120]}",
                     now))
            out[f"{scope}_changed"] = changed
            out[f"{scope}_mood"] = nm["mood"]
            out[f"{scope}_intensity"] = round(nm["intensity"], 4)
            out[f"{scope}_attribution"] = nm["attribution"]
        return out

    def _apply_contagion(self, new_moods: dict, factor: float) -> dict:
        """情绪传染规则：
        - 强度 < 0.4 不传染
        - 正面/中性：双向传染
        - 负面 + 归因 self/shared：以关切方式传染
        - 负面 + 归因 other：不传染（对方觉得是你的问题不会让你也难过）
        """
        for from_scope, to_scope in (("user", "ai"), ("ai", "user")):
            fd, td = new_moods[from_scope], new_moods[to_scope]
            if fd["intensity"] < 0.4:
                continue
            if fd["mood"] in CONTAGION_BLOCKED_MOODS and fd["attribution"] == "other":
                continue
            if fd["mood"] in POSITIVE_MOODS:
                if td["intensity"] < fd["intensity"]:
                    td["intensity"] = min(1.0,
                                          td["intensity"] + (fd["intensity"] - td["intensity"]) * factor)
                    if td["mood"] == "neutral":
                        td["mood"] = fd["mood"]
            elif fd["mood"] in NEGATIVE_MOODS and fd["attribution"] in ("self", "shared"):
                if td["mood"] == "neutral" or td["intensity"] < 0.3:
                    td["mood"] = "compassionate" if to_scope == "ai" else "concerned"
                    td["intensity"] = min(1.0, fd["intensity"] * factor * 1.5)
        return new_moods

    def _apply_peace_event(self, new_moods: dict, event: str) -> dict:
        """平复事件触发快速衰减 + 情绪方向转换。"""
        decay = _mood.PEACE_EVENT_DECAY_FACTOR
        for scope in ("user", "ai"):
            data = new_moods[scope]
            if data["mood"] in NEGATIVE_MOODS:
                data["intensity"] = data["intensity"] * decay
                if event == "user_apology" and scope == "ai":
                    data["mood"] = "relieved"
                elif event == "ai_admission" and scope == "user":
                    data["mood"] = "relieved"
                elif event == "mutual_reconciliation":
                    data["mood"] = "warm"
                elif event == "task_celebration":
                    data["mood"] = "pleased" if data["intensity"] > 0.1 else "warm"
                elif event == "misunderstanding_resolved":
                    data["mood"] = "relieved"
        return new_moods

    def natural_decline(self, sid: str) -> None:
        """连续中性对话触发自然回落：强度额外 × factor。"""
        recent_triggers = self.db.query_one(
            "SELECT count(*) c FROM mood_triggers "
            "WHERE session_id=? AND message_id > "
            "(SELECT COALESCE(MAX(id)-6, 0) FROM conversations WHERE session_id=?)",
            (sid, sid))["c"]
        recent_since = (now_cst() - timedelta(minutes=30)).isoformat(
            timespec="seconds")
        recent_moods = self.db.query_all(
            "SELECT scope, mood FROM mood_history "
            "WHERE create_time > ? "
            "ORDER BY id DESC LIMIT 6", (recent_since,))
        all_neutral = all(m["mood"] == "neutral" for m in recent_moods)
        min_turns = _mood.NATURAL_DECLINE_MIN_NEUTRAL_TURNS
        factor = _mood.NATURAL_DECLINE_FACTOR
        if recent_triggers == 0 and all_neutral and len(recent_moods) >= min_turns:
            row = self.db.query_one("SELECT * FROM mood_state WHERE id=1")
            for scope in ("user", "ai"):
                intensity = self._decay(
                    row[f"{scope}_intensity"], row[f"{scope}_updated_at"])
                new_intensity = intensity * factor
                self.db.execute(
                    f"UPDATE mood_state SET {scope}_intensity=? WHERE id=1",
                    (round(new_intensity, 4),))

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
