"""
下一步建议模块（Next Step Suggestion）。

职责：
- 从元认知骨架（CognitiveSkeleton）中提取种子候选池（seeds）
- 四道门槛过滤（seeds 空 / 情绪 / 意图收敛 / brief 寒暄 / doc_only）
- 从 LLM 流式输出中解析建议句分隔符

降级（对话零阻塞铁律）：种子提取或门槛过滤任何异常 → 返回空列表，
调用方跳过建议句，不阻塞主链路。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("second_person.next_step")

# ---- 常量 ------------------------------------------------------------------

# 负向情绪集合（对齐 mood_judge_v2 情绪分类体系）
NEGATIVE_MOODS = {"sad", "anxious", "angry", "tired"}

# 评分门槛：三维度各 1-5，总分 15，≥12 入选
SCORE_THRESHOLD = 12

# 建议句长度约束（字符）
MIN_SUGGESTION_CHARS = 10
MAX_SUGGESTION_CHARS = 80

# 分隔符正则：匹配 \n 后跟 ↳ 或 —
_SEPARATOR_RE = re.compile(r'\n(?:↳|—)\s*')

# 四类锚点枚举
ANCHOR_KINDS = ("deepen", "verify", "extend", "contrast")


# ---- 数据结构 --------------------------------------------------------------

@dataclass
class Seed:
    """单条候选种子。"""
    kind: str           # deepen / verify / extend / contrast
    text: str           # 候选内容描述
    anchor_ref: str     # 溯源锚点（如 "strategy.decompose.parts[0]"）


# ---- 流水线 ----------------------------------------------------------------

class NextStepPipeline:
    """下一步建议流水线：种子提取 → 门槛过滤。"""

    def __init__(self, config: dict | None = None):
        cfg = config or {}
        self.enabled: bool = cfg.get("next_step_suggest_enabled", True)
        self.emotion_threshold: float = cfg.get(
            "next_step_emotion_threshold", 0.5)

    # ---- 种子提取（从元认知骨架） ------------------------------------------

    def extract_seeds(self, skeleton) -> list[Seed]:
        """从 CognitiveSkeleton 提取四类锚点种子。

        每类最多 2 条，总数上限 5。anchor 缺失一律丢弃。
        任何异常返回空列表（零阻塞）。
        """
        try:
            return self._extract(skeleton)
        except Exception:
            logger.warning("种子提取失败", exc_info=True)
            return []

    def _extract(self, skeleton) -> list[Seed]:
        if skeleton is None:
            return []
        data = skeleton.to_dict() if hasattr(skeleton, "to_dict") else {}
        seeds: list[Seed] = []

        # deepen ← decompose 中未展开的子问题
        decompose = data.get("decompose") or {}
        if decompose.get("needed") and decompose.get("parts"):
            for i, part in enumerate(decompose["parts"][:2]):
                text = str(part).strip()
                if text:
                    seeds.append(Seed(
                        kind="deepen", text=text,
                        anchor_ref=f"skeleton.decompose.parts[{i}]"))

        # verify ← hidden_assumptions 中未验证的关键假设
        assumptions = data.get("hidden_assumptions") or []
        for i, a in enumerate(assumptions[:2]):
            if isinstance(a, dict) and not a.get("holds", True):
                text = str(a.get("assumption", "")).strip()
                if text:
                    seeds.append(Seed(
                        kind="verify", text=text,
                        anchor_ref=f"skeleton.hidden_assumptions[{i}]"))

        # extend ← expert_lens.non_obvious_insight 的延伸方向
        el = data.get("expert_lens") or {}
        insight = str(el.get("non_obvious_insight", "")).strip()
        if insight:
            seeds.append(Seed(
                kind="extend", text=insight,
                anchor_ref="skeleton.expert_lens.non_obvious_insight"))

        # contrast ← answer_shape 明确排除的角度
        ash = data.get("answer_shape") or {}
        excluded = str(ash.get("closing_move", "")).strip()
        if excluded and excluded != insight:
            seeds.append(Seed(
                kind="contrast", text=excluded,
                anchor_ref="skeleton.answer_shape.closing_move"))

        # 总数上限 5
        return seeds[:5]

    # ---- 门槛过滤 ----------------------------------------------------------

    def filter_gates(self, seeds: list[Seed], *,
                     emotion=None,
                     db=None,
                     session_id: str | None = None,
                     depth_level: str = "normal",
                     doc_only: bool = False,
                     elicitation_active: bool = False) -> list[Seed]:
        """四道门槛过滤，任一命中返回空列表。

        1. seeds 空门槛
        2. 情绪门槛（负向 + 强度 ≥ 阈值 → 不出）
        3. 意图收敛门槛（最近 3 轮同 kind → 不出）
        4. brief 寒暄 / doc_only 门槛
        5. elicitation 活跃门槛（追问中不出建议句，§01 互斥规则）
        """
        if not seeds:
            return []
        if doc_only:
            return []
        if depth_level == "brief":
            return []
        # 追问门槛：追问轮次不出建议句
        if elicitation_active:
            return []
        # 情绪门槛
        if emotion is not None:
            valence = getattr(emotion, "valence", "neutral") or "neutral"
            intensity = getattr(emotion, "intensity", 0) or 0
            if valence in NEGATIVE_MOODS and intensity >= self.emotion_threshold:
                return []
        # 意图收敛门槛
        if db is not None and session_id:
            try:
                if self._is_intent_converging(db, session_id):
                    return []
            except Exception:
                logger.warning("意图收敛检测失败", exc_info=True)
        return seeds

    def _is_intent_converging(self, db, session_id: str) -> bool:
        """最近 3 轮意图同 kind → 收敛期，不出建议。"""
        rows = db.query_all(
            "SELECT response_strategy_json FROM conversations "
            "WHERE session_id=? AND role='assistant' "
            "ORDER BY id DESC LIMIT 3",
            (session_id,))
        if len(rows) < 3:
            return False
        types = []
        for row in rows:
            raw = row["response_strategy_json"] if hasattr(
                row, "__getitem__") else row[0]
            if raw:
                try:
                    d = json.loads(raw)
                    t = d.get("intent_type")
                    if t:
                        types.append(t)
                except (json.JSONDecodeError, TypeError):
                    pass
        return len(types) == 3 and len(set(types)) == 1


# ---- 分隔符解析（后处理） --------------------------------------------------

def parse_suggestion(text: str) -> tuple[str, str | None]:
    """从 LLM 输出中解析建议句。

    检测正文末尾的分隔符（\\n↳ 或 \\n—），提取后续文本为建议句。
    返回 (去除建议句后的正文, 建议句文本或 None)。
    """
    m = _SEPARATOR_RE.search(text)
    if not m:
        return text, None
    suggestion = text[m.end():].strip()
    body = text[:m.start()].rstrip()
    if not suggestion or len(suggestion) < MIN_SUGGESTION_CHARS:
        return text, None
    return body, suggestion[:MAX_SUGGESTION_CHARS]


def strip_suggestion_from_partial(text: str) -> str:
    """中断补救：移除部分回复中可能残留的分隔符和建议句。"""
    m = _SEPARATOR_RE.search(text)
    if not m:
        return text
    return text[:m.start()].rstrip()
