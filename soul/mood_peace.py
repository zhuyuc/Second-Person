"""平复事件启发式检测（after-turn 规则通道）。

judge 未给出 peace_event 时，用关键词从用户+助手文本推断。
"""
from __future__ import annotations

_PEACE_EVENTS = (
    "user_apology",
    "ai_admission",
    "mutual_reconciliation",
    "task_celebration",
    "misunderstanding_resolved",
    "none",
)

_USER_APOLOGY = ("对不起", "抱歉", "我错了", "是我不好", "我道歉", "不好意思")
_AI_ADMISSION = ("是我搞错", "是我的错", "我弄错了", "我理解错了", "承认错误",
                 "我失误了", "是我搞砸")
_RECONCILE = ("和解", "没关系了", "不计较了", "过去了", "别介意", "和好")
_CELEBRATE = ("太棒了", "搞定了", "终于成了", "成功了", "庆祝一下", "我们做到了")
_MISUNDERSTAND = ("原来如此", "误会了", "我懂了", "澄清了", "理解错了你的意思")


def normalize_peace_event(raw: str | None) -> str:
    value = str(raw or "none").strip() or "none"
    return value if value in _PEACE_EVENTS else "none"


def detect_peace_event(user_message: str, assistant_content: str = "") -> str:
    """从本轮文本启发式推断平复事件；无命中返回 none。"""
    user = user_message or ""
    ai = assistant_content or ""
    both = f"{user}\n{ai}"

    if any(k in user for k in _USER_APOLOGY):
        return "user_apology"
    if any(k in ai for k in _AI_ADMISSION):
        return "ai_admission"
    if any(k in both for k in _RECONCILE):
        return "mutual_reconciliation"
    if any(k in both for k in _CELEBRATE):
        return "task_celebration"
    if any(k in both for k in _MISUNDERSTAND):
        return "misunderstanding_resolved"
    return "none"
