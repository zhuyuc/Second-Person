"""
IM 端追问答案解析器（对应产品方案 §03 IM 端降级方案）。

解析规则：
  数字回答：如"1 2 3"或"1，3，1" → 按空格/逗号/换行拆分，映射到对应 Q 的 options index
  文字回答：如"我准备投 20 万，持有两年，看好美团" → 全文作为 custom_answer
  混合回答：如"1，持有三年，暂无" → 按顺序尝试匹配数字，剩余文本作为 custom_answer
  关键词"跳过"：任意位置识别 → 触发 close
  无法解析：全文作为 custom_answer
"""
from __future__ import annotations

import re
from typing import Any

# 匹配独立数字（1-4）
_DIGIT_RE = re.compile(r'\b([1-4])\b')
# 跳过关键词
_SKIP_RE = re.compile(r'跳过|略过|不用了|算了|取消')


def parse_im_elicitation(text: str, questions: list[dict]) -> dict[str, Any]:
    """解析 IM 端用户回答。

    Args:
        text: 用户输入消息文本
        questions: 原始 ask_user 的 questions 数组 [{id, options, ...}]

    Returns:
        {"action": "answer"|"close", "answers": [...], "custom_text": str|None}
        answers: [{question_id, type: "option"|"custom", value}]
    """
    text = text.strip()

    # 检查跳过关键词
    if _SKIP_RE.search(text):
        return {"action": "close", "answers": [], "custom_text": None}

    # 提取所有独立数字
    digits = [int(m.group(1)) for m in _DIGIT_RE.finditer(text)]
    # 移除数字后的剩余文本
    remaining = _DIGIT_RE.sub('', text).strip().strip('，,。.').strip()

    answers = []

    if digits and not remaining:
        # 纯数字回答
        for i, d in enumerate(digits):
            if i < len(questions) and 1 <= d <= len(questions[i].get("options", [])):
                answers.append({
                    "question_id": questions[i]["id"],
                    "type": "option",
                    "value": questions[i]["options"][d - 1],
                })
    elif digits and remaining:
        # 混合回答：数字匹配对应 Q，剩余文本作为最后一个 Q 的 custom_answer
        for i, d in enumerate(digits):
            if i < len(questions) and 1 <= d <= len(questions[i].get("options", [])):
                answers.append({
                    "question_id": questions[i]["id"],
                    "type": "option",
                    "value": questions[i]["options"][d - 1],
                })
        # 剩余文本
        idx = len(digits)
        if idx < len(questions) and remaining:
            answers.append({
                "question_id": questions[idx]["id"],
                "type": "custom",
                "value": remaining,
            })
    elif remaining and not digits:
        # 纯文字：整体作为 custom_answer
        if questions:
            answers.append({
                "question_id": questions[0]["id"],
                "type": "custom",
                "value": remaining,
            })
    else:
        # 无法解析：全文 custom
        if questions:
            answers.append({
                "question_id": questions[0]["id"],
                "type": "custom",
                "value": text,
            })

    return {"action": "answer", "answers": answers, "custom_text": remaining or text}
