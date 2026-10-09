"""Pre-turn 用户情绪脉冲。

硬约束：
- 槽位绑什么走什么：system_one → /v1/systemone；其它文本协议 → chat JSON
- chat 路径仅 source="mood_fast" 这一次传 thinking_enabled（默认关），不写槽位默认
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
from soul.mood_taxonomy import CANONICAL_MOODS, clamp01, normalize_mood

logger = logging.getLogger("second_person.mood_fast")

_STATE_EMPTY = {
    "mood": "neutral",
    "intensity": 0.0,
    "confidence": 0.0,
    "source": "state",
}


def _parse_pulse(raw: Any, *, source: str) -> dict | None:
    """校验七情白名单并 clamp；非法返回 None。"""
    if not isinstance(raw, dict):
        return None
    mood = normalize_mood(raw.get("mood"), default="")
    if mood not in CANONICAL_MOODS:
        return None
    try:
        intensity = clamp01(float(raw.get("intensity")))
    except (TypeError, ValueError):
        return None
    try:
        confidence = clamp01(float(raw.get("confidence")))
    except (TypeError, ValueError):
        return None
    return {
        "mood": mood,
        "intensity": round(intensity, 4),
        "confidence": round(confidence, 4),
        "source": source,
    }


def _provider_type(snap) -> str:
    return (getattr(snap, "provider_type", "") or "").strip().lower()


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

    channel = str(config.get("mood_fast_path_provider", "flash") or "flash")
    if channel == "lexicon":
        hit = detect_lexicon(user_message)
        return hit if hit else dict(_STATE_EMPTY)

    # 独立 mood_fast 槽；未配置时回退 agent → chat
    snap = (providers.snapshot_for("mood_fast")
            or providers.snapshot_for("agent")
            or providers.snapshot_for("chat"))
    if snap is None:
        hit = detect_lexicon(user_message)
        return hit if hit else dict(_STATE_EMPTY)

    if _provider_type(snap) == "system_one":
        result = await _detect_system_one(
            snap, config,
            user_message=user_message,
            session_id=session_id,
            parent_trace_id=parent_trace_id,
        )
    else:
        result = await _detect_flash(
            llm, snap, config,
            user_message=user_message,
            session_id=session_id,
            parent_trace_id=parent_trace_id,
        )
    if result is not None:
        return result
    hit = detect_lexicon(user_message)
    return hit if hit else dict(_STATE_EMPTY)


async def _detect_system_one(
    snap, config, *,
    user_message: str,
    session_id: str | None,
    parent_trace_id: str | None,
) -> dict | None:
    timeout_ms = int(config.get("mood_fast_path_timeout_ms", 1500) or 1500)
    timeout_ms = max(100, min(5000, timeout_ms))

    from langfuse.integration import get_tracer
    from infrastructure.system_one import (
        build_mood_questions, parse_mood_answers, system_one_decide,
    )

    tracer = get_tracer()
    t0 = time.perf_counter()
    with tracer.attach_trace(parent_trace_id):
        span = tracer.span_start(
            "mood.fast",
            input={"chars": len(user_message or ""), "backend": "system_one"},
            metadata={
                "phase": "pre_turn",
                "timeout_ms": timeout_ms,
                "provider_type": "system_one",
                "session_id": session_id,
            },
        )
        try:
            # 本地服务未就绪时按需拉起（与 ComfyUI lazy 同约定）
            try:
                from infrastructure.lazy_services import ensure_service
                ensure_service("system_one", timeout=min(60.0, timeout_ms / 1000.0 + 30))
            except Exception:  # noqa: BLE001
                logger.debug("ensure system_one 跳过", exc_info=True)

            raw = await asyncio.wait_for(
                system_one_decide(
                    snap.base_url,
                    state=(user_message or "")[:2000],
                    model=getattr(snap, "model_id", None) or "jev-style",
                    questions=build_mood_questions(),
                    api_key=getattr(snap, "api_key", None),
                    timeout=max(0.5, timeout_ms / 1000.0),
                ),
                timeout=timeout_ms / 1000.0,
            )
            parsed = _parse_pulse(parse_mood_answers(raw), source="system_one")
            latency_ms = round((time.perf_counter() - t0) * 1000)
            if parsed is None:
                span.end(output={
                    "ok": False, "reason": "invalid",
                    "latency_ms": latency_ms, "backend": "system_one",
                })
                return None
            span.end(output={
                "mood": parsed["mood"],
                "intensity": parsed["intensity"],
                "confidence": parsed["confidence"],
                "source": "system_one",
                "latency_ms": latency_ms,
            })
            return parsed
        except asyncio.TimeoutError:
            latency_ms = round((time.perf_counter() - t0) * 1000)
            span.end(output={
                "ok": False, "reason": "timeout",
                "latency_ms": latency_ms, "backend": "system_one",
            })
            logger.info("mood_fast system_one 超时 (%sms)，降级词典", timeout_ms)
            return None
        except Exception as e:  # noqa: BLE001
            latency_ms = round((time.perf_counter() - t0) * 1000)
            span.end(
                level="ERROR",
                status_message=str(e)[:240],
                output={"ok": False, "reason": "error",
                        "latency_ms": latency_ms, "backend": "system_one"},
            )
            logger.warning("mood_fast system_one 失败，降级词典", exc_info=True)
            return None


async def _detect_flash(
    llm, snap, config, *,
    user_message: str,
    session_id: str | None,
    parent_trace_id: str | None,
) -> dict | None:
    timeout_ms = int(config.get("mood_fast_path_timeout_ms", 1500) or 1500)
    timeout_ms = max(100, min(5000, timeout_ms))
    thinking_on = bool(config.get("mood_fast_path_thinking", False))

    from langfuse.integration import get_tracer
    tracer = get_tracer()
    t0 = time.perf_counter()
    with tracer.attach_trace(parent_trace_id):
        span = tracer.span_start(
            "mood.fast",
            input={"chars": len(user_message or ""), "backend": "flash"},
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
