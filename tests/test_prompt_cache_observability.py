"""Prompt cache fingerprints stay deterministic and observable."""
from pathlib import Path

from infrastructure.db import Database
from infrastructure.prompt_cache import (
    build_prompt_fingerprint,
    classify_prompt_change,
    session_context_payload,
)
from infrastructure.session_metrics import record_step, turn_metrics


ROOT = Path(__file__).resolve().parent.parent


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "prompt-cache.db")
    db.run_migrations(ROOT / "migrations")
    return db


def test_fingerprint_ignores_dict_key_order_but_keeps_tool_order():
    system = "stable rules"
    context_a = {"history": [{"role": "user", "content": "hi"}], "x": 1}
    context_b = {"x": 1, "history": [{"content": "hi", "role": "user"}]}
    tools = [{"type": "function", "function": {"name": "read"}}]

    assert build_prompt_fingerprint(system, tools, context_a) == \
        build_prompt_fingerprint(system, tools, context_b)
    reversed_tools = list(reversed(tools)) + [
        {"type": "function", "function": {"name": "write"}}]
    assert build_prompt_fingerprint(system, tools, context_a) != \
        build_prompt_fingerprint(system, reversed_tools, context_a)


def test_session_context_payload_excludes_request_specific_tail():
    payload = session_context_payload({
        "history": [],
        "project_context": "project",
        "memory_context": "retrieval result",
        "dynamic_blocks": [("mood", "today")],
    })
    assert payload == {"history": [], "project_context": "project"}


def test_change_reason_separates_prefix_reuse_from_context_change():
    tools = [{"type": "function", "function": {"name": "read"}}]
    first = build_prompt_fingerprint("rules", tools, {"history": []})
    same = build_prompt_fingerprint("rules", tools, {"history": ["new turn"]})
    changed_tools = build_prompt_fingerprint(
        "rules", tools + [{"type": "function", "function": {"name": "write"}}],
        {"history": ["new turn"]})

    assert classify_prompt_change(None, first) == ("initial", False)
    assert classify_prompt_change(first, same) == ("session_context_changed", True)
    assert classify_prompt_change(same, changed_tools) == ("tool_schema_changed", False)


def test_turn_metrics_exposes_hashes_reasons_and_reuse(tmp_path: Path):
    db = _db(tmp_path)
    try:
        db.execute("INSERT INTO sessions(session_id,title,last_active,message_count) "
                   "VALUES('cache_session','test','now',0)")
        db.execute(
            "INSERT INTO agent_turns(id,session_id,status,reasoning_effort,max_steps,created_at,updated_at) "
            "VALUES('cache_turn','cache_session','completed','off',2,'now','now')")
        record_step(
            db, turn_id="cache_turn", step=1, llm_ms=100, ttft_ms=20,
            decode_ms=80, input_tokens=100, cache_read_tokens=96,
            system_prompt_hash="system", tool_schema_hash="tools",
            session_context_hash="context", prefix_hash="prefix",
            cache_change_reason="initial", prefix_reused=False)
        record_step(
            db, turn_id="cache_turn", step=2, llm_ms=100, ttft_ms=20,
            decode_ms=80, input_tokens=100, cache_read_tokens=95,
            system_prompt_hash="system", tool_schema_hash="tools",
            session_context_hash="context-2", prefix_hash="prefix",
            cache_change_reason="session_context_changed", prefix_reused=True)

        metrics = turn_metrics(db, "cache_turn")
        assert metrics["prompt_cache"]["observations"] == 2
        assert metrics["prompt_cache"]["prefix_reused_steps"] == 1
        assert metrics["prompt_cache"]["change_reasons"] == {
            "initial": 1, "session_context_changed": 1}
        assert metrics["prompt_cache"]["latest"]["prefix_hash"] == "prefix"
    finally:
        db.close()
