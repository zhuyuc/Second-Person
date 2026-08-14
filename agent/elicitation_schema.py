"""
ask_user 伪工具 Schema 与校验（对应产品方案 §04 Tool 契约）。

ask_user 不注册到 ToolRegistry，而是由 clarification_router 产出 seed，
经 Schema 校验后直接 emit elicitation SSE 事件。

校验拒绝时：丢弃 tool_use，向 LLM 追加 tool_result error → 触发降级为文字追问。
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger("second_person.elicitation_schema")

# ---- 问答题 Schema ---------------------------------------------------------

# 内部判定字段（双自检中间产物）不向前端透出：
# counterfactual = 反事实分叉结论；answer_branches = 逐选项答案分支预演。
# 二者为闭环闸门依据，由 schema 校验强制执行（写不出分支 → 拒绝 → 降级文字澄清）。
QUESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "id":           {"type": "string"},
        "question":     {"type": "string", "maxLength": 80},
        "description":  {"type": "string", "maxLength": 120},
        "options":      {"type": "array", "minItems": 2, "maxItems": 4,
                         "items": {"type": "string", "maxLength": 20}},
        "counterfactual": {"type": "string", "maxLength": 60},
        "answer_branches": {"type": "array", "minItems": 2, "maxItems": 4,
                            "items": {"type": "string", "maxLength": 30}},
        "allow_custom": {"type": "boolean", "default": True},
        "required":     {"type": "boolean", "default": True},
    },
    "required": ["id", "question", "options", "counterfactual", "answer_branches"],
}

# ---- ask_user 完整入参 Schema ----------------------------------------------

ASK_USER_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array", "minItems": 1, "maxItems": 3,
            "items": QUESTION_SCHEMA,
        },
        "reason": {"type": "string", "maxLength": 120},
    },
    "required": ["questions", "reason"],
}


# ---- 校验函数 ---------------------------------------------------------------

def _check_schema(instance: dict, schema: dict, path: str = "") -> list[str]:
    """递归 Schema 校验，返回错误列表。仅覆盖本模块所需的 type/required/minItems/maxItems/maxLength。"""
    errors: list[str] = []
    stype = schema.get("type")
    if stype == "object":
        if not isinstance(instance, dict):
            errors.append(
                f"{path}: expected object, got {type(instance).__name__}")
            return errors
        required = schema.get("required", [])
        props = schema.get("properties", {})
        for key in required:
            if key not in instance:
                errors.append(f"{path}.{key}: required field missing")
        for key, val in instance.items():
            if key in props:
                errors.extend(_check_schema(val, props[key], f"{path}.{key}"))
    elif stype == "array":
        if not isinstance(instance, list):
            errors.append(
                f"{path}: expected array, got {type(instance).__name__}")
            return errors
        min_items = schema.get("minItems")
        max_items = schema.get("maxItems")
        if min_items is not None and len(instance) < min_items:
            errors.append(
                f"{path}: expected at least {min_items} items, got {len(instance)}")
        if max_items is not None and len(instance) > max_items:
            errors.append(
                f"{path}: expected at most {max_items} items, got {len(instance)}")
        items_schema = schema.get("items")
        if items_schema:
            for i, item in enumerate(instance):
                errors.extend(_check_schema(
                    item, items_schema, f"{path}[{i}]"))
    elif stype == "string":
        if not isinstance(instance, str):
            errors.append(
                f"{path}: expected string, got {type(instance).__name__}")
            return errors
        max_len = schema.get("maxLength")
        if max_len is not None and len(instance) > max_len:
            errors.append(f"{path}: max length {max_len}, got {len(instance)}")
    elif stype == "boolean":
        if not isinstance(instance, bool):
            errors.append(
                f"{path}: expected boolean, got {type(instance).__name__}")
    return errors


def validate_ask_user(payload: dict | str, config: dict | None = None) -> tuple[bool, list[str], dict | None]:
    """校验 ask_user 入参。

    Returns: (valid, errors, normalized_payload)
    - valid=True: errors 为空，normalized_payload 为校验后的 dict
    - valid=False: errors 非空，normalized_payload 为 None
    """
    cfg = config or {}
    max_questions = cfg.get("elicitation_max_questions", 3)
    min_options = cfg.get("elicitation_min_options", 2)
    max_options = cfg.get("elicitation_max_options", 4)
    option_max = cfg.get("elicitation_option_max_chars", 20)
    reason_max = cfg.get("elicitation_reason_max_chars", 60)
    question_max = cfg.get("elicitation_question_max_chars", 40)

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as e:
            return False, [f"JSON parse error: {e}"], None

    if not isinstance(payload, dict):
        return False, ["payload must be a JSON object"], None

    errors: list[str] = []

    # --- questions 数组校验 ---
    questions = payload.get("questions")
    if not isinstance(questions, list):
        errors.append("questions: must be an array")
        return False, errors, None
    if len(questions) < 1:
        errors.append("questions: at least 1 question required")
    if len(questions) > max_questions:
        errors.append(
            f"questions: at most {max_questions} questions, got {len(questions)}")

    for i, q in enumerate(questions):
        if not isinstance(q, dict):
            errors.append(f"questions[{i}]: must be an object")
            continue
        # id
        if not q.get("id"):
            errors.append(f"questions[{i}].id: required")
        # question
        if not q.get("question"):
            errors.append(f"questions[{i}].question: required")
        elif isinstance(q["question"], str) and len(q["question"]) > question_max:
            errors.append(f"questions[{i}].question: max {question_max} chars")
        # options
        opts = q.get("options")
        if not isinstance(opts, list):
            errors.append(f"questions[{i}].options: must be an array")
        else:
            if len(opts) < min_options:
                errors.append(
                    f"questions[{i}].options: at least {min_options} items, got {len(opts)}")
            if len(opts) > max_options:
                errors.append(
                    f"questions[{i}].options: at most {max_options} items, got {len(opts)}")
            for j, opt in enumerate(opts):
                if isinstance(opt, str) and len(opt) > option_max:
                    errors.append(
                        f"questions[{i}].options[{j}]: max {option_max} chars")
        # counterfactual（反事实分叉结论）
        if not q.get("counterfactual"):
            errors.append(f"questions[{i}].counterfactual: required")
        # answer_branches（逐选项答案分支预演）：与 options 一一对应、逐项非空
        branches = q.get("answer_branches")
        if not isinstance(branches, list) or not branches:
            errors.append(
                f"questions[{i}].answer_branches: must be a non-empty array")
        elif isinstance(opts, list):
            if len(branches) != len(opts):
                errors.append(
                    f"questions[{i}].answer_branches: must match options count "
                    f"({len(opts)}), got {len(branches)}")
            for j, b in enumerate(branches):
                if not isinstance(b, str) or not b.strip():
                    errors.append(
                        f"questions[{i}].answer_branches[{j}]: must be non-empty string")

    # --- reason 校验 ---
    reason = payload.get("reason")
    if not reason:
        errors.append("reason: required")
    elif isinstance(reason, str) and len(reason) > reason_max:
        errors.append(f"reason: max {reason_max} chars")

    if errors:
        return False, errors, None
    return True, [], payload


def strip_internal_fields(payload: dict) -> dict:
    """剥离内部判定字段（counterfactual/answer_branches），仅向前端透出展示字段。

    三自检中间产物仅供闭环闸门校验使用，不应出现在 elicit 卡片数据中。
    """
    questions = []
    for q in (payload.get("questions") or []):
        if not isinstance(q, dict):
            continue
        cleaned = {k: v for k, v in q.items()
                   if k not in ("counterfactual", "answer_branches")}
        questions.append(cleaned)
    out = dict(payload)
    out["questions"] = questions
    return out


def build_tool_result_error(error_type: str = "schema_invalid") -> dict:
    """构造 tool_result error 供追加到 LLM messages。"""
    return {
        "tool": "ask_user",
        "ok": False,
        "error": error_type,
        "result": json.dumps({"error": "schema_invalid"}, ensure_ascii=False),
    }
