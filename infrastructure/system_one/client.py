"""System One HTTP 客户端：POST /v1/systemone（Jev / jev-style 兼容）。"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urljoin

import httpx

from soul.mood_taxonomy import (
    INTENSITY_SCORE_LEVELS,
    MOOD_CHOICE_CRITERIA,
    clamp01,
    intensity_from_score,
    normalize_mood,
)

logger = logging.getLogger("second_person.system_one")

_DEFAULT_TIMEOUT = 8.0


def _root(base_url: str) -> str:
    return (base_url or "").rstrip("/") + "/"


def build_mood_questions() -> dict[str, Any]:
    """七情 choice + 三档 intensity score。"""
    return {
        "mood": {
            "type": "choice",
            "instructions": "判断这段话表达的主要情绪（七情之一，或平静）。",
            "criteria": dict(MOOD_CHOICE_CRITERIA),
        },
        "intensity": {
            "type": "score",
            "instructions": "该情绪的强烈程度。",
            "criteria": list(INTENSITY_SCORE_LEVELS),
        },
    }


def parse_mood_answers(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """从 systemone 响应解析 {mood, intensity, confidence}。"""
    if not isinstance(payload, dict):
        return None
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        return None
    mood_ans = answers.get("mood") or {}
    int_ans = answers.get("intensity") or {}
    if not isinstance(mood_ans, dict) or not isinstance(int_ans, dict):
        return None

    mood_raw = mood_ans.get("choice") or mood_ans.get("mood")
    if mood_raw is None or str(mood_raw).strip() == "":
        return None
    mood = normalize_mood(mood_raw, default="")
    if mood not in MOOD_CHOICE_CRITERIA:
        return None

    score = int_ans.get("score")
    if score is None and isinstance(int_ans.get("probabilities"), dict):
        # 无期望分时用众数档
        probs = int_ans["probabilities"]
        levels = list(INTENSITY_SCORE_LEVELS)
        best_i, best_p = 0, -1.0
        for i, name in enumerate(levels):
            p = float(probs.get(name) or probs.get(str(i)) or 0.0)
            if p > best_p:
                best_i, best_p = i, p
        score = best_i
    intensity = intensity_from_score(score, len(INTENSITY_SCORE_LEVELS))

    confs: list[float] = []
    for ans in (mood_ans, int_ans):
        if "confidence" in ans:
            try:
                confs.append(clamp01(float(ans["confidence"])))
            except (TypeError, ValueError):
                pass
    if not confs and isinstance(mood_ans.get("probabilities"), dict):
        try:
            confs.append(clamp01(max(float(v) for v in mood_ans["probabilities"].values())))
        except (TypeError, ValueError):
            pass
    confidence = min(confs) if confs else 0.5

    return {
        "mood": mood,
        "intensity": round(clamp01(intensity), 4),
        "confidence": round(clamp01(confidence), 4),
    }


async def system_one_decide(
    base_url: str, *,
    state: str,
    model: str,
    questions: dict[str, Any] | None = None,
    api_key: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """调用 POST {base}/v1/systemone，返回原始 JSON。"""
    url = urljoin(_root(base_url), "v1/systemone")
    headers = {"Content-Type": "application/json"}
    key = (api_key or "").strip()
    if key and key != "local":
        headers["Authorization"] = f"Bearer {key}"
    body = {
        "model": model or "jev-style",
        "state": state or "",
        "questions": questions or build_mood_questions(),
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    if not isinstance(data, dict):
        raise ValueError("systemone 响应非对象")
    return data


async def probe_system_one(base_url: str, timeout: float = 5.0) -> dict:
    """连通性：优先 /health，其次 /v1/models。"""
    root = _root(base_url)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for path in ("health", "v1/models"):
            try:
                resp = await client.get(urljoin(root, path))
                if 200 <= resp.status_code < 500:
                    return {"ok": True, "status": resp.status_code, "path": path}
            except Exception as exc:  # noqa: BLE001
                last = str(exc)
                logger.debug("system_one probe %s 失败: %s", path, last)
                continue
    return {"ok": False, "error": "System One 服务不可达"}
