"""Hybrid TokenMeter — anchor + delta pricing under different session shapes."""
from __future__ import annotations

from agent.token_meter import TokenMeter, Anchor


def _msg(role: str, content: str) -> dict:
    return {"role": role, "content": content}


# ---------------------------------------------------------------- cold start

def test_cold_start_uses_estimate_only():
    meter = TokenMeter()
    m = meter.measure("sid1",
                      [_msg("user", "hello"), _msg("assistant", "hi")],
                      system="you are helpful")
    assert m.source == "estimate-only"
    assert m.total_tokens > 0
    # 无 anchor 时 uncached == total（不知道 cache 情况）
    assert m.uncached_tokens == m.total_tokens
    assert m.anchor is None


def test_empty_messages_still_produces_measurement():
    meter = TokenMeter()
    m = meter.measure("sid1", [], system="")
    assert m.total_tokens == 0
    assert m.source == "estimate-only"


def test_estimate_scales_with_content_length():
    meter = TokenMeter()
    short = meter.measure("s", [_msg("user", "hi")])
    long = meter.measure("s", [_msg("user", "hi " * 500)])
    assert long.total_tokens > short.total_tokens


# ---------------------------------------------------------------- anchor path

def test_anchor_replaces_estimate_after_commit():
    meter = TokenMeter()
    msgs = [_msg("user", "explain agent loops"),
            _msg("assistant", "sure, here's a summary")]
    # cold: estimate
    cold = meter.measure("s", msgs, system="sys")
    # commit provider usage from that call
    meter.commit_anchor("s", msgs, {
        "input_tokens": 1200, "output_tokens": 200,
        "cache_read_tokens": 800, "cache_write_tokens": 0,
    }, system="sys")
    warm = meter.measure("s", msgs, system="sys")
    assert warm.source == "anchor+delta"
    # anchor 复用：等长同尾 → total == exact_prompt_tokens
    assert warm.total_tokens == 1200
    # uncached = exact - cache_read
    assert warm.uncached_tokens == 400
    assert cold.source == "estimate-only"


def test_anchor_prices_delta_for_new_messages():
    meter = TokenMeter()
    msgs = [_msg("user", "q1"), _msg("assistant", "a1")]
    meter.commit_anchor("s", msgs, {
        "input_tokens": 1000, "output_tokens": 100,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
    })
    # 追加一条 user，用 anchor 定价
    extended = msgs + [_msg("user", "follow-up longer question " * 20)]
    m = meter.measure("s", extended)
    assert m.source == "anchor+delta"
    # 1000（锚点）+ delta > 1000
    assert m.total_tokens > 1000
    delta = m.total_tokens - 1000
    assert delta > 40   # 大概 20 * 4 词 + 开销


def test_anchor_invalidates_when_system_changes():
    meter = TokenMeter()
    msgs = [_msg("user", "q"), _msg("assistant", "a")]
    meter.commit_anchor("s", msgs, {
        "input_tokens": 500, "output_tokens": 50,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
    }, system="original system")
    # system 变了 → 锚点失效，退回估算
    m = meter.measure("s", msgs, system="different system prompt")
    assert m.source == "estimate-only"


def test_anchor_invalidates_when_tail_edited():
    meter = TokenMeter()
    msgs = [_msg("user", "q1"), _msg("assistant", "a1"),
            _msg("user", "q2")]
    meter.commit_anchor("s", msgs, {
        "input_tokens": 700, "output_tokens": 80,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
    })
    # 编辑最后一条 → tail_hash 变 → 锚点失效
    edited = [_msg("user", "q1"), _msg("assistant", "a1"),
              _msg("user", "q2 EDITED")]
    m = meter.measure("s", edited)
    assert m.source == "estimate-only"


def test_anchor_invalidates_when_history_shrinks():
    meter = TokenMeter()
    msgs = [_msg("user", "q1"), _msg("assistant", "a1"), _msg("user", "q2")]
    meter.commit_anchor("s", msgs, {
        "input_tokens": 700, "output_tokens": 80,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
    })
    # 消息被压缩/删除 → 短于锚点 → 失效
    shrunk = msgs[:1]
    m = meter.measure("s", shrunk)
    assert m.source == "estimate-only"


def test_drop_anchor_forces_reestimate():
    meter = TokenMeter()
    msgs = [_msg("user", "q")]
    meter.commit_anchor("s", msgs, {
        "input_tokens": 500, "output_tokens": 50,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
    })
    assert meter.has_anchor("s")
    meter.drop_anchor("s")
    assert not meter.has_anchor("s")
    assert meter.measure("s", msgs).source == "estimate-only"


# ---------------------------------------------------------------- edge cases

def test_missing_usage_keeps_prior_anchor():
    meter = TokenMeter()
    msgs = [_msg("user", "q")]
    meter.commit_anchor("s", msgs, {
        "input_tokens": 500, "output_tokens": 50,
        "cache_read_tokens": 100, "cache_write_tokens": 0,
    })
    # 后续 step usage 缺失 → 不应清除旧 anchor
    meter.commit_anchor("s", msgs + [_msg("assistant", "a")], None)
    assert meter.has_anchor("s")
    m = meter.measure("s", msgs)   # 用原有 anchor 仍能命中
    assert m.source == "anchor+delta"


def test_zero_input_usage_ignored():
    meter = TokenMeter()
    msgs = [_msg("user", "q")]
    meter.commit_anchor("s", msgs, {"input_tokens": 0, "output_tokens": 0,
                                     "cache_read_tokens": 0, "cache_write_tokens": 0})
    # 全 0 usage 视同缺失，不写锚
    assert not meter.has_anchor("s")


def test_tool_calls_priced():
    """带 tool_calls 的 assistant message 也参与估算。"""
    meter = TokenMeter()
    plain = _msg("assistant", "sure")
    with_calls = {
        "role": "assistant", "content": "",
        "tool_calls": [{"function": {"name": "fs_read",
                                       "arguments": '{"path": "/README.md"}'}}]
    }
    m1 = meter.measure("s", [plain])
    m2 = meter.measure("s", [with_calls])
    assert m2.total_tokens > m1.total_tokens


def test_uncached_tokens_reflect_cache_hit():
    """cache_read 大部分命中时 uncached 应远小于 total（真实压力低）。"""
    meter = TokenMeter()
    msgs = [_msg("user", "q")]
    meter.commit_anchor("s", msgs, {
        "input_tokens": 50000, "output_tokens": 500,
        "cache_read_tokens": 45000, "cache_write_tokens": 0,
    })
    m = meter.measure("s", msgs)
    assert m.total_tokens == 50000
    # 只有 5000 是真实计费部分
    assert m.uncached_tokens == 5000
