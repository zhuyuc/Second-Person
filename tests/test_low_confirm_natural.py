"""低置信确认：场景门控、相关性、软话术、成功注入才记账。"""
from __future__ import annotations

from memory.low_confirm_policy import (
    build_constraint,
    is_identity_question,
    is_strong_task,
    pick_candidate,
    relevance_score,
    scene_blocks_low_confirm,
    short_summary,
    try_build_low_confirm_constraint,
)


def test_identity_question_blocks():
    assert is_identity_question("你是一个什么样的人")
    assert is_identity_question("你是谁")
    assert not is_identity_question("考点预测数据管线怎么设计")


def test_scene_blocks_identity_and_greeting():
    assert scene_blocks_low_confirm("你是一个什么样的人")
    assert scene_blocks_low_confirm("你好")
    assert scene_blocks_low_confirm("帮我写一份报告")
    assert not scene_blocks_low_confirm("考点预测那套数据管线怎么设计")


def test_scene_blocks_high_negative_pulse():
    assert scene_blocks_low_confirm(
        "继续刚才的事",
        pulse={"mood": "angry", "intensity": 0.8, "confidence": 0.9},
    )
    assert scene_blocks_low_confirm(
        "继续",
        active_action="comfort_first",
    )


def test_pick_skips_unrelated_on_identity():
    # 人设问在 try_build 层会被 scene 挡住；这里测相关性本身无关
    cand = [{
        "id": "mem_x",
        "title": "希望训练考试预测模型",
        "summary": "用户想用历年教材和真题训练AI预测后年考题",
    }]
    picked, tier = pick_candidate("你是一个什么样的人", cand)
    assert picked is None
    assert tier == "none"


def test_pick_high_related_exam_topic():
    cand = [{
        "id": "mem_x",
        "title": "希望训练考试预测模型",
        "summary": "用户想用历年教材和真题训练AI预测后年考题",
    }]
    picked, tier = pick_candidate("考点预测那套数据管线怎么设计", cand)
    assert picked is not None
    assert tier in ("high", "weak")
    assert relevance_score("考点预测那套数据管线怎么设计", cand[0]) > 0


def test_try_build_skips_identity_even_with_candidate():
    cand = [{
        "id": "mem_x",
        "title": "希望训练考试预测模型",
        "summary": "用户想用历年教材和真题训练AI预测后年考题",
    }]
    text, c = try_build_low_confirm_constraint(
        sid_already_asked=False,
        user_message="你是一个什么样的人",
        candidates=cand,
    )
    assert text is None and c is None


def test_try_build_injects_soft_copy_when_related():
    cand = [{
        "id": "mem_x",
        "title": "希望训练考试预测模型",
        "summary": "用户想用历年教材和真题训练AI预测后年考题",
    }]
    text, c = try_build_low_confirm_constraint(
        sid_already_asked=False,
        user_message="考点预测那套数据管线怎么设计",
        candidates=cand,
    )
    assert c is not None
    assert text is not None
    # 旧硬命令腔不得出现；反例短语允许写在「不要用…」禁令里
    assert "本轮回复末尾请自然确认" not in text
    assert "无需输出 JSON" not in text
    assert "顺带确认" in text or "轻声一问" in text
    assert "可选" in text


def test_try_build_hello_skips():
    text, c = try_build_low_confirm_constraint(
        sid_already_asked=False,
        user_message="你好",
        candidates=[{
            "id": "mem_x", "title": "t",
            "summary": "用户想用历年教材和真题训练AI预测后年考题",
        }],
    )
    assert text is None and c is None


def test_short_summary_truncates():
    s = short_summary("希望训练考试预测模型",
                      "用户想用历年教材和真题训练AI预测后年考题" * 3)
    assert len(s) <= 36


def test_build_constraint_no_boilerplate():
    text = build_constraint(
        {"title": "希望训练考试预测模型",
         "summary": "用户想用历年教材和真题训练AI预测后年考题"},
        "high",
    )
    assert "本轮回复末尾请自然确认" not in text
    assert "顺带确认" in text
    assert "可以不问" in text
    assert is_strong_task("帮我写一份方案")


def test_try_build_skips_when_session_already_asked():
    text, c = try_build_low_confirm_constraint(
        sid_already_asked=True,
        user_message="考点预测那套数据管线怎么设计",
        candidates=[{
            "id": "mem_x",
            "title": "希望训练考试预测模型",
            "summary": "用户想用历年教材和真题训练AI预测后年考题",
        }],
    )
    assert text is None and c is None


def test_try_build_skips_unrelated_topic():
    text, c = try_build_low_confirm_constraint(
        sid_already_asked=False,
        user_message="今天中午吃什么比较好",
        candidates=[{
            "id": "mem_x",
            "title": "希望训练考试预测模型",
            "summary": "用户想用历年教材和真题训练AI预测后年考题",
        }],
    )
    assert text is None and c is None


def test_core_marks_only_on_successful_inject(monkeypatch):
    """接线：仅成功写入 constraint 时 mark + 会话名额。"""
    from agent.core import AgentCore

    core = AgentCore.__new__(AgentCore)
    core.config = {}
    core.lifecycle = type("L", (), {})()
    core._low_confirm_asked_sessions = set()
    core._pending_low_confirm = None
    marked: list[str] = []

    def _list():
        return [{
            "id": "mem_x",
            "title": "希望训练考试预测模型",
            "summary": "用户想用历年教材和真题训练AI预测后年考题",
        }]

    core.lifecycle.list_low_confirm_candidates = _list  # type: ignore[attr-defined]
    core.lifecycle.mark_low_confirm_asked = (  # type: ignore[attr-defined]
        lambda mid: marked.append(mid))

    parts: list[str] = []
    core._try_append_low_confirm(
        sid="s1",
        user_message="你是一个什么样的人",
        pulse=None,
        active_action=None,
        constraints_parts=parts,
    )
    assert parts == []
    assert marked == []
    assert "s1" not in core._low_confirm_asked_sessions

    core._try_append_low_confirm(
        sid="s1",
        user_message="考点预测那套数据管线怎么设计",
        pulse=None,
        active_action=None,
        constraints_parts=parts,
    )
    assert len(parts) == 1
    assert marked == ["mem_x"]
    assert "s1" in core._low_confirm_asked_sessions
