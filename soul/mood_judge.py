"""Turn-end mood analysis via LLM (v2 pipeline input for MoodManager.apply_v2)."""
from __future__ import annotations

import logging

from infrastructure.json_repair import repair_json
from infrastructure.prompt_loader import PROMPTS

logger = logging.getLogger("second_person.mood_judge")

_DEFAULT_RES = {
    "mood": "neutral", "intensity": 0.0, "confidence": 0.0,
    "attribution": "none", "note": "",
}


def _normalize_res(raw: dict | None) -> dict:
    if not isinstance(raw, dict):
        return dict(_DEFAULT_RES)
    mood = str(raw.get("mood") or "neutral").strip() or "neutral"
    try:
        intensity = max(0.0, min(1.0, float(raw.get("intensity") or 0.0)))
    except (TypeError, ValueError):
        intensity = 0.0
    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0
    attribution = str(raw.get("attribution") or "none").strip() or "none"
    if attribution not in ("none", "self", "other", "shared"):
        attribution = "none"
    return {
        "mood": mood,
        "intensity": intensity,
        "confidence": confidence,
        "attribution": attribution,
        "note": str(raw.get("note") or "")[:200],
    }


async def judge_turn_moods(llm, providers, *, user_message: str,
                           assistant_content: str,
                           trigger_summary: str = "",
                           session_id: str | None = None) -> tuple[dict, dict]:
    """Return (user_res, ai_res) for MoodManager.apply_v2."""
    snap = providers.snapshot_for("agent") or providers.snapshot_for("chat")
    if snap is None:
        return _normalize_res(None), _normalize_res(None)
    parts = [
        f"【用户消息】\n{user_message or ''}",
        f"【助手回复】\n{assistant_content or ''}",
    ]
    if trigger_summary:
        parts.append(f"【规则触发摘要】\n{trigger_summary}")
    prompt = [
        {"role": "system", "content": PROMPTS.load_raw("agent/prompts/mood_judge")},
        {"role": "user", "content": "\n\n".join(parts)},
    ]
    try:
        resp = await llm.chat(snap, prompt, source="system_agent",
                              session_id=session_id, json_mode=True)
        data = repair_json(resp.get("content") or "")
        user = _normalize_res(data.get("user") if isinstance(data, dict) else None)
        ai = _normalize_res(data.get("ai") if isinstance(data, dict) else None)
        return user, ai
    except Exception:  # noqa: BLE001
        logger.warning("情绪判定失败，降级 neutral", exc_info=True)
        return _normalize_res(None), _normalize_res(None)
