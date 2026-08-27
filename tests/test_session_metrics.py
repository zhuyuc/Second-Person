"""Regression tests for whole-session metrics and provider cache usage."""
from pathlib import Path

from infrastructure.db import Database
from infrastructure.llm_provider import TokenRecorder, normalize_usage
from infrastructure.session_metrics import record_step, session_metrics, turn_metrics

ROOT = Path(__file__).resolve().parent.parent


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "metrics.db")
    db.run_migrations(ROOT / "migrations")
    return db


def test_deepseek_cache_usage_is_normalized():
    usage = normalize_usage({
        "prompt_tokens": 1000,
        "completion_tokens": 120,
        "prompt_tokens_details": {"cached_tokens": 930},
    })
    assert usage == {
        "input_tokens": 1000,
        "output_tokens": 120,
        "cache_read_tokens": 930,
        "cache_write_tokens": 0,
    }


def test_anthropic_cache_usage_is_added_to_billed_input():
    usage = normalize_usage({
        "input_tokens": 70,
        "output_tokens": 12,
        "cache_read_input_tokens": 30,
        "cache_creation_input_tokens": 10,
    }, input_key="input_tokens", output_key="output_tokens")
    assert usage["input_tokens"] == 110
    assert usage["cache_read_tokens"] == 30
    assert usage["cache_write_tokens"] == 10


def test_session_projection_keeps_cumulative_usage_and_current_turn_speed(tmp_path: Path):
    db = _db(tmp_path)
    try:
        db.execute("INSERT INTO sessions(session_id,title,last_active,message_count) "
                   "VALUES('sess_metrics','测试','now',0)")
        for turn_id in ("turn_a", "turn_b"):
            db.execute(
                "INSERT INTO agent_turns(id,session_id,status,reasoning_effort,max_steps,created_at,updated_at) "
                "VALUES(?,?, 'completed','off',2,'now','now')",
                (turn_id, "sess_metrics"),
            )
        record_step(db, turn_id="turn_a", step=1, llm_ms=1000, ttft_ms=200,
                    decode_ms=800, input_tokens=100, output_tokens=80,
                    cache_read_tokens=90)
        record_step(db, turn_id="turn_b", step=1, llm_ms=2000, ttft_ms=500,
                    decode_ms=1500, input_tokens=300, output_tokens=150,
                    cache_read_tokens=240, tool_ms=120)
        recorder = TokenRecorder(db)
        recorder.record("model", "agent_step", 100, 80, "sess_metrics",
                        cache_read_tokens=90)
        recorder.record("model", "agent_step", 300, 150, "sess_metrics",
                        cache_read_tokens=240)
        # execute_nowait is intentionally asynchronous; this read is a barrier.
        db.execute("UPDATE token_usage SET input_tokens=input_tokens WHERE 0")

        current = turn_metrics(db, "turn_b")
        aggregate = session_metrics(db, "sess_metrics", current_turn_id="turn_b")
        assert current["tokens_per_second"] == 100.0
        assert aggregate["turns"] == 2
        assert aggregate["steps"] == 2
        assert aggregate["ttft_average_ms"] == 350.0
        assert aggregate["input_tokens"] == 400
        assert aggregate["output_tokens"] == 230
        assert aggregate["cache_read_tokens"] == 330
        assert aggregate["cache_hit_percent"] == 82.5
        assert aggregate["current_turn"]["tokens_per_second"] == 100.0
    finally:
        db.close()
