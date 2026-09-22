"""Pre-turn 用户情绪脉冲（DeepSeek Flash / 词典降级）。

硬约束：
- 仅 source="mood_fast" 这一次传 extra_body thinking_enabled=False
- 绝不写入 Provider / 槽位默认
- 硬超时后降级词典 → 再无信号则 source=state
- Langfuse 挂同一 agent.turn（mood.fast），不新开 trace
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from infrastructure.json_repair import repair_json
from infrastructure.prompt_loader import PROMPTS
from soul.mood_fast_signal import detect_lexicon
from soul.mood_manager import MOOD_CN

logger = logging.getLogger("second_person.mood_fast")

_STATE_EMPTY = {
    "mood": "neutral",
    "intensity": 0.0,
    "confidence": 0.0,
    "source": "state",
}


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def _parse_pulse(raw: Any, *, source: str) -> dict | None:
    """校验 mood 白名单并 clamp；非法返回 None。"""
    if not isinstance(raw, dict):
        return None
    mood = str(raw.get("mood") or "").strip().lower()
    if mood not in MOOD_CN:
        return None
    try:
        intensity = _clamp01(float(raw.get("intensity")))
    except (TypeError, ValueError):
        return None
    try:
        confidence = _clamp01(float(raw.get("confidence")))
    except (TypeError, ValueError):
        return None
    return {
        "mood": mood,
        "intensity": round(intensity, 4),
        "confidence": round(confidence, 4),
        "source": source,
    }


async def detect_user_pulse(
    llm, providers, config, *,
    user_message: str,
    session_id: str | None = None,
    parent_trace_id: str | None = None,
) -> dict:
    """本轮用户脉冲。返回 {mood, intensity, confidence, source}。"""
    if not config.get("mood_enabled", True):
        return dict(_STATE_EMPTY)
    if float(config.get("mood_influence_strength", 0.5) or 0) <= 0:
        return dict(_STATE_EMPTY)
    if not config.get("mood_fast_path_enabled", True):
        return dict(_STATE_EMPTY)

    provider = str(config.get("mood_fast_path_provider", "flash") or "flash")
    if provider == "lexicon":
        hit = detect_lexicon(user_message)
        return hit if hit else dict(_STATE_EMPTY)

    # flash 通道
    result = await _detect_flash(
        llm, providers, config,
        user_message=user_message,
        session_id=session_id,
        parent_trace_id=parent_trace_id,
    )
    if result is not None:
        return result
    hit = detect_lexicon(user_message)
    return hit if hit else dict(_STATE_EMPTY)


async def _detect_flash(
    llm, providers, config, *,
    user_message: str,
    session_id: str | None,
    parent_trace_id: str | None,
) -> dict | None:
    # 独立 mood_fast 槽（设置页可换模型）；未配置时回退 agent → chat
    snap = (providers.snapshot_for("mood_fast")
            or providers.snapshot_for("agent")
            or providers.snapshot_for("chat"))
    if snap is None:
        return None

    timeout_ms = int(config.get("mood_fast_path_timeout_ms", 800) or 800)
    timeout_ms = max(100, min(5000, timeout_ms))
    # 配置项仅控制本调用；默认 False。禁止写回槽位。
    thinking_on = bool(config.get("mood_fast_path_thinking", False))

    from langfuse.integration import get_tracer
    tracer = get_tracer()
    t0 = time.perf_counter()
    with tracer.attach_trace(parent_trace_id):
        span = tracer.span_start(
            "mood.fast",
            input={"chars": len(user_message or "")},
            metadata={
                "phase": "pre_turn",
                "timeout_ms": timeout_ms,
                "thinking": thinking_on,
            },
        )
        try:
            system = PROMPTS.load_raw("agent/prompts/mood_fast")
            prompt = [
                {"role": "system", "content": system},
                {"role": "user", "content": (user_message or "")[:2000]},
            ]
            # 仅本调用关思考；不得写回槽位默认
            extra_body = {"thinking_enabled": thinking_on}
            resp = await asyncio.wait_for(
                llm.chat(
                    snap, prompt,
                    source="mood_fast",
                    session_id=session_id,
                    json_mode=True,
                    temperature=0.1,
                    max_tokens=64,
                    extra_body=extra_body,
                ),
                timeout=timeout_ms / 1000.0,
            )
            data = repair_json(resp.get("content") or "")
            parsed = _parse_pulse(data, source="flash")
            latency_ms = round((time.perf_counter() - t0) * 1000)
            if parsed is None:
                span.end(output={
                    "ok": False, "reason": "invalid",
                    "latency_ms": latency_ms, "thinking": thinking_on,
                })
                return None
            span.end(output={
                "mood": parsed["mood"],
                "intensity": parsed["intensity"],
                "confidence": parsed["confidence"],
                "source": "flash",
                "latency_ms": latency_ms,
                "thinking": thinking_on,
            })
            return parsed
        except asyncio.TimeoutError:
            latency_ms = round((time.perf_counter() - t0) * 1000)
            span.end(output={
                "ok": False, "reason": "timeout",
                "latency_ms": latency_ms, "thinking": thinking_on,
            })
            logger.info("mood_fast 超时 (%sms)，降级词典", timeout_ms)
            return None
        except Exception as e:  # noqa: BLE001
            latency_ms = round((time.perf_counter() - t0) * 1000)
            span.end(
                level="ERROR",
                status_message=str(e)[:240],
                output={"ok": False, "reason": "error", "latency_ms": latency_ms},
            )
            logger.warning("mood_fast 失败，降级词典", exc_info=True)
            return None
