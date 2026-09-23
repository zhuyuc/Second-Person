"""低置信记忆确认：场景门控、相关性评分、软化约束话术（零 LLM）。

产品方案：docs/低置信记忆确认-自然表达优化方案.md
- 相关才问、无关则让；成功注入才记账
- 不改 system 前缀；只产出 constraints 文本
"""
from __future__ import annotations

import re
from typing import Any

from memory import _constants as _mem
from soul.mood_manager import NEGATIVE_MOODS
from soul.mood_turn_policy import is_brief_social_turn

# 身份 / 人设提问：本轮不宜夹带无关确认
_IDENTITY_PATTERNS = [
    r"你是谁",
    r"你是什么",
    r"你是一个什么样",
    r"你什么样的人",
    r"介绍一下你自己",
    r"介绍下你自己",
    r"你的身份",
    r"你的能力",
    r"你会什么",
    r"你能做什么",
    r"what are you",
    r"who are you",
]

# 强任务：要结果/交付，不宜夹带闲聊确认
_STRONG_TASK_PATTERNS = [
    r"写(一份|个|篇)?(文档|报告|方案|简历|代码|脚本)",
    r"改(一下|一改|代码|文件)",
    r"帮我(写|改|生成|画|导出|下载|实现)",
    r"生成(图片|图像|视频|文档)",
    r"出[一张张]图",
    r"导出",
    r"实现一下",
    r"修复",
    r"debug",
    r"refactor",
    r"提交\s*pr",
    r"写单测",
]

# 正式交付 / 结构化输出
_STRUCTURED_PATTERNS = [
    r"按markdown",
    r"输出json",
    r"只要(代码|表格|json)",
    r"不要解释",
    r"正式文档",
    r"导出为",
]

# 闲聊 / 开放收尾（弱相关时才允许轻声一问）
_CASUAL_PATTERNS = [
    r"聊聊",
    r"随便",
    r"最近怎么样",
    r"在忙什么",
    r"有空吗",
    r"想你了",
]

_TOKEN_RE = re.compile(
    r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9_\-]{1,}|\d{2,}"
)


def _tokens(text: str) -> set[str]:
    return {m.group(0).lower() for m in _TOKEN_RE.finditer(text or "")}


def short_summary(title: str | None, summary: str | None,
                  max_chars: int = 36) -> str:
    """注入前短句化，避免模型照抄长摘要。"""
    body = (summary or "").strip() or (title or "").strip()
    body = re.sub(r"\s+", "", body)
    if len(body) <= max_chars:
        return body
    return body[: max_chars - 1] + "…"


def is_identity_question(message: str | None) -> bool:
    q = (message or "").strip()
    if not q:
        return False
    return any(re.search(p, q, re.IGNORECASE) for p in _IDENTITY_PATTERNS)


def is_strong_task(message: str | None) -> bool:
    q = (message or "").strip()
    if not q:
        return False
    return any(re.search(p, q, re.IGNORECASE) for p in _STRONG_TASK_PATTERNS)


def is_structured_output_turn(message: str | None) -> bool:
    q = (message or "").strip()
    if not q:
        return False
    return any(re.search(p, q, re.IGNORECASE) for p in _STRUCTURED_PATTERNS)


def is_casual_open_turn(message: str | None) -> bool:
    """弱相关时：仅闲聊/开放收尾允许轻声一问。"""
    q = (message or "").strip()
    if not q:
        return False
    if is_brief_social_turn(q) or is_identity_question(q) or is_strong_task(q):
        return False
    if any(re.search(p, q, re.IGNORECASE) for p in _CASUAL_PATTERNS):
        return True
    # 短开放句且非强任务：偏闲聊
    if len(q) <= 24 and "?" not in q and "？" not in q:
        return True
    return False


def pulse_high_negative(pulse: dict | None) -> bool:
    """快路径脉冲：高强度负面 → 先承接，不问。"""
    if not isinstance(pulse, dict):
        return False
    mood = str(pulse.get("mood") or "").strip().lower()
    try:
        intensity = float(pulse.get("intensity") or 0.0)
    except (TypeError, ValueError):
        intensity = 0.0
    return mood in NEGATIVE_MOODS and intensity >= 0.55


def scene_blocks_low_confirm(
    user_message: str | None, *,
    pulse: dict | None = None,
    active_action: str | None = None,
) -> bool:
    """本轮不宜追问（硬跳过，不记账）。"""
    if is_brief_social_turn(user_message):
        return True
    if is_identity_question(user_message):
        return True
    if is_strong_task(user_message):
        return True
    if is_structured_output_turn(user_message):
        return True
    if pulse_high_negative(pulse):
        return True
    if (active_action or "") == "comfort_first":
        return True
    return False


def relevance_score(user_message: str | None, candidate: dict) -> float:
    """零 LLM 相关分：词元 Jaccard + 中文 bigram 重叠，落在 [0,1]。"""
    msg = (user_message or "").strip()
    cand = f"{candidate.get('title') or ''} {candidate.get('summary') or ''}"
    msg_tok = _tokens(msg)
    cand_tok = _tokens(cand)
    score = 0.0
    if msg_tok and cand_tok:
        inter = len(msg_tok & cand_tok)
        union = len(msg_tok | cand_tok)
        if union:
            score = inter / union
    # 中文 bigram：共享越多分越高（短句也能挂上「考点/预测」类主题）
    msg_bi = {msg[i:i + 2] for i in range(max(0, len(msg) - 1))
              if "\u4e00" <= msg[i] <= "\u9fff" and "\u4e00" <= msg[i + 1] <= "\u9fff"}
    cand_bi = {cand[i:i + 2] for i in range(max(0, len(cand) - 1))
               if "\u4e00" <= cand[i] <= "\u9fff" and "\u4e00" <= cand[i + 1] <= "\u9fff"}
    if msg_bi and cand_bi:
        shared = msg_bi & cand_bi
        if shared:
            score = max(score, min(0.55, 0.12 * len(shared)))
    # 同 domain：用户消息点到领域名时抬一档相关
    domain = str(candidate.get("domain") or "").strip().lower()
    if domain and len(domain) >= 2 and domain in msg.lower():
        score = max(score, _mem.LOW_CONFIRM_RELEVANCE_HIGH)
    return min(1.0, score)


def relevance_tier(score: float) -> str:
    """high | weak | none"""
    if score >= _mem.LOW_CONFIRM_RELEVANCE_HIGH:
        return "high"
    if score >= _mem.LOW_CONFIRM_RELEVANCE_WEAK:
        return "weak"
    return "none"


def pick_candidate(
    user_message: str | None,
    candidates: list[dict],
) -> tuple[dict | None, str]:
    """从候选池按相关分排序；返回 (candidate, tier) 或 (None, none)。

    弱相关仅在闲聊/开放收尾轮放行。
    """
    if not candidates:
        return None, "none"
    ranked: list[tuple[float, dict]] = []
    for c in candidates:
        ranked.append((relevance_score(user_message, c), c))
    ranked.sort(key=lambda x: x[0], reverse=True)
    best_score, best = ranked[0]
    tier = relevance_tier(best_score)
    if tier == "high":
        return best, "high"
    if tier == "weak" and is_casual_open_turn(user_message):
        return best, "weak"
    return None, "none"


def build_constraint(candidate: dict, tier: str) -> str:
    """软化约束话术；tier=high|weak。"""
    brief = short_summary(candidate.get("title"), candidate.get("summary"))
    if tier == "high":
        return (
            "【顺带确认（可选、要自然）】\n"
            f"你对用户有一条尚未确认的印象：{brief}。\n"
            "若与本轮话题顺得上，用一两句口语轻轻问是不是这样；\n"
            "问完即止，不要开新话题段，不要用「早前我推断」「这事属实吗」等套话。\n"
            "若接不上，本轮可以不问。"
        )
    return (
        "【轻声一问（可选）】\n"
        "本轮若已回答完且语气轻松，可在最后用半句带过：\n"
        f"「对了，以前好像听你提过 {brief}——还在做吗？」\n"
        "不要单独开一节，不要复述推断过程。接不上就省略。"
    )


def try_build_low_confirm_constraint(
    *,
    sid_already_asked: bool,
    user_message: str | None,
    candidates: list[dict],
    pulse: dict | None = None,
    active_action: str | None = None,
    min_msg_chars: int | None = None,
) -> tuple[str | None, dict | None]:
    """门控 + 选题 + 话术。返回 (constraint_text, candidate)；跳过则为 (None, None)。

    调用方仅在返回非空 constraint 时 mark_low_confirm_asked / 记会话名额。
    """
    if sid_already_asked:
        return None, None
    msg = (user_message or "").strip()
    if not msg:
        return None, None
    min_chars = int(
        min_msg_chars if min_msg_chars is not None
        else _mem.LOW_CONFIRM_MIN_MSG_CHARS
    )
    if len(msg) < min_chars and "?" not in msg and "？" not in msg:
        return None, None
    if scene_blocks_low_confirm(
            user_message, pulse=pulse, active_action=active_action):
        return None, None
    picked, tier = pick_candidate(user_message, candidates)
    if not picked or tier == "none":
        return None, None
    return build_constraint(picked, tier), picked
