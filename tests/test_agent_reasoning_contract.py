from __future__ import annotations

from agent.decision_summary import build_tool_decision_notice
from agent.repeat_tool_guard import RepeatToolGuard
from infrastructure.provider_registry import infer_capabilities


def test_repeat_guard_uses_canonical_arguments_and_thresholds():
    guard = RepeatToolGuard((3, 5))
    assert guard.observe("web_search", {"q": "same", "page": 1}) is None
    assert guard.observe("web_search", {"page": 1, "q": "same"}) is None
    reminder = guard.observe("web_search", {"q": "same", "page": 1})
    assert reminder and reminder.count == 3 and reminder.threshold == 3
    assert guard.observe("web_search", {"q": "same", "page": 1}) is None
    assert guard.observe("web_search", {"q": "same", "page": 1})


def test_decision_summary_identifies_host_ownership_without_claiming_cot():
    notice = build_tool_decision_notice(
        tool_name="web_search", description="search current web information",
        arguments={"query": "DeepSeek"}, step=1, call_id="call_1")
    assert notice["actor"] == "host"
    assert notice["source"] == "host_inferred"
    assert notice["reason_code"] == "external_information"
    assert "模型认为" not in notice["summary"]


def test_provider_capability_inference_distinguishes_native_reasoning():
    deepseek = infer_capabilities("openai_compatible", "https://api.deepseek.com", "deepseek-reasoner")
    mimo = infer_capabilities("openai_compatible", "https://api.mimo.ai", "mimo-v2.5")
    assert deepseek["native_reasoning"] is True
    assert deepseek["reasoning_efforts"] == ("off", "low", "high", "max")
    assert mimo["native_reasoning"] is False
    assert "tool_call" in mimo["capabilities"]
