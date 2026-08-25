"""工程规则与公开协议的一致性门禁。"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.contracts import ContractValidationError, parse_chat_send
from infrastructure.sse_contract import (
    SSE_EVENT_SPECS,
    SSE_TERMINAL_EVENTS,
    SSEContractError,
    validate_sse_event,
)

ROOT = Path(__file__).resolve().parent.parent


def test_engineering_rule_documents_are_the_declared_entry_points():
    required = {
        "docs/ARCHITECTURE_RULES.md",
        "docs/API_CONTRACT.md",
        "docs/PROMPT_REGISTRY.md",
        "docs/UI_UX_SPEC.md",
    }
    assert all((ROOT / path).is_file() for path in required)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "user_profile/" in readme
    assert "profile/        用户画像" not in readme
    assert "docs/ARCHITECTURE_RULES.md" in readme
    assert "docs/API_CONTRACT.md" in readme


def test_chat_request_contract_uses_only_reasoning_effort():
    request = parse_chat_send({
        "message": "测试", "client_request_id": "  request-1  ",
        "edit_message_id": "42",
    })
    assert request.client_request_id == "request-1"
    assert request.reasoning_effort == "high"
    assert request.edit_message_id == 42

    assert parse_chat_send({"message": "测试", "reasoning_effort": "max"}).reasoning_effort == "max"
    with pytest.raises(ContractValidationError, match="reasoning_effort"):
        parse_chat_send({"message": "测试", "reasoning_effort": "automatic"})

    with pytest.raises(ContractValidationError, match="images"):
        parse_chat_send({"message": "测试", "images": {"bad": True}})
    with pytest.raises(ContractValidationError, match="message"):
        parse_chat_send({"message": None})
    with pytest.raises(ContractValidationError, match="positive"):
        parse_chat_send({"message": "测试", "regenerate_message_id": 0})


def test_chat_sse_events_are_registered_and_have_terminal_semantics():
    expected = {
        "queued", "error", "reasoning_delta", "decision_notice", "tool_executing",
        "tool_visual", "content_delta", "citations",
        "handoff_ready", "mood_updated", "turn_completed",
        "turn_started", "step_started", "tool_pending_approval", "tool_blocked",
        "tool_result",
    }
    assert expected == set(SSE_EVENT_SPECS)
    assert SSE_TERMINAL_EVENTS == {"turn_completed", "error"}
    assert "tool_confirm" not in SSE_EVENT_SPECS

    validate_sse_event("tool_pending_approval", {
        "turn_id": "turn_1", "approval_id": "apr_1", "tool_name": "file_write",
        "risk_level": "destructive",
    })
    with pytest.raises(SSEContractError, match="missing fields"):
        validate_sse_event("content_delta", {})
    with pytest.raises(SSEContractError, match="unknown"):
        validate_sse_event("unregistered_event", {})


def test_agent_event_literals_do_not_bypass_sse_registry():
    patterns = [
        ROOT / "agent/core.py",
        ROOT / "agent/tool_executor.py",
    ]
    literals: set[str] = set()
    for path in patterns:
        source = path.read_text(encoding="utf-8")
        literals.update(re.findall(r'(?:emit|yield)\(\s*["\']([a-z_]+)["\']', source))
    assert literals <= set(SSE_EVENT_SPECS), sorted(literals - set(SSE_EVENT_SPECS))


def test_vue_pages_do_not_bypass_first_party_api_layer():
    for path in (ROOT / "frontend/src/views").rglob("*.vue"):
        assert "fetch(" not in path.read_text(encoding="utf-8"), path
    memory_view = (ROOT / "frontend/src/views/MemoryView.vue").read_text(encoding="utf-8")
    assert "@/api/imports" in memory_view
    assert "@/composables/useSSE'" not in memory_view


def test_routes_use_the_shared_json_object_reader():
    for filename in ("chat.py", "memory.py", "misc.py", "settings.py"):
        source = (ROOT / "app/routes" / filename).read_text(encoding="utf-8")
        assert "await request.json()" not in source, filename
        assert "read_json_object" in source, filename


def test_product_document_declares_host_owned_tool_approval():
    product_doc = (ROOT / "docs/SecondPerson-全系统产品方案.md").read_text(encoding="utf-8")
    assert "POST /chat/tool-confirm" not in product_doc
    assert "tool_confirm" not in product_doc
    assert "获得用户确认后执行" in product_doc
