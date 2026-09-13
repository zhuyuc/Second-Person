"""本轮情绪展示策略：寒暄降档、自我歉意无承接时弱化（不改 DB 真值）。"""
from __future__ import annotations

import re

from memory.retriever_gates import is_ack_only

# 极短寒暄 / 招呼（ACK 之外）
_BRIEF_GREETING = [
    r"^(你好|您好|嗨|哈喽|hello|hi|hey|在吗|在不在|早|晚安|早安)[。!！?？…~啊呀哦嗯]*$",
]

# 用户在承接上轮失误/任务（此时允许自省/歉意全量显现）
_THREAD_CONTINUATION = [
    r"上次", r"刚才", r"之前", r"继续", r"接着", r"再改", r"改一下",
    r"那个错", r"你说的", r"你弄的", r"没做好", r"搞砸", r"失败",
    r"道歉", r"抱歉", r"为什么错", r"怎么又", r"还是不行",
    r"\.(?:html?|py|md|js|ts|tsx|vue|css|json)\b",
]

SELF_NEGATIVE_MOODS = frozenset({
    "apologetic", "ashamed", "self_critical", "guilty", "remorseful",
})

GREETING_EXPRESSION_CONSTRAINT = (
    "【本轮表达约束：极短寒暄】\n"
    "情绪可以保留在语气里，但回复控制在一两句短招呼内；"
    "禁止主动提起「上次的事」「是我没做好」「过意不去」等道歉/复盘独白，"
    "除非用户明确提到。不要拉长开场或追问「今天想聊什么」式客套段。"
)


def is_brief_social_turn(message: str | None) -> bool:
    """极短寒暄/确认类：你好、谢谢、好的、嗯…"""
    q = (message or "").strip()
    if not q:
        return False
    if is_ack_only(q):
        return True
    if len(q) <= 3 and "?" not in q and "？" not in q:
        # 「你好」「嗨」等；带问号的短句不当寒暄降档
        return True
    return any(re.match(p, q, re.IGNORECASE) for p in _BRIEF_GREETING)


def user_continues_prior_thread(message: str | None) -> bool:
    """用户是否在承接上轮任务/失误话题。"""
    q = (message or "").strip()
    if not q:
        return False
    return any(re.search(p, q, re.IGNORECASE) for p in _THREAD_CONTINUATION)


def should_soften_self_negative(*, ai_mood: str, ai_attribution: str,
                                user_message: str | None) -> bool:
    """自我/未标明归因的歉意自责：用户未承接时，本轮注入侧弱化。"""
    mood = (ai_mood or "").strip().lower()
    if mood not in SELF_NEGATIVE_MOODS:
        return False
    attr = (ai_attribution or "").strip().lower()
    if attr == "other":
        return False
    if user_continues_prior_thread(user_message):
        return False
    return True
