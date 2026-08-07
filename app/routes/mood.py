"""
Mood API —— 情绪状态查询与重置接口。
GET  /mood/current  → 当前双方情绪
GET  /mood/history  → 历史记录
POST /mood/reset    → 重置
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from soul.mood_manager import _mood_cn

router = APIRouter()


def _c():
    """延迟取 container，避免循环导入。"""
    from app.container import _container
    return _container


@router.get("/mood/current")
async def get_current_mood():
    c = _c()
    row = c.db.query_one("SELECT * FROM mood_state WHERE id=1")
    if not row:
        return {"code": 200, "data": {"enabled": False}}
    decay_user = c.mood._decay(row["user_intensity"], row["user_updated_at"])
    decay_ai = c.mood._decay(row["ai_intensity"], row["ai_updated_at"])
    return {"code": 200, "data": {
        "enabled": c.config.get("mood_enabled", True),
        "user": {
            "mood": row["user_mood"],
            "mood_cn": _mood_cn(row["user_mood"]),
            "intensity": round(decay_user, 2),
            "attribution": row.get("user_attribution", ""),
            "updated_at": row["user_updated_at"],
        },
        "ai": {
            "mood": row["ai_mood"],
            "mood_cn": _mood_cn(row["ai_mood"]),
            "intensity": round(decay_ai, 2),
            "attribution": row.get("ai_attribution", ""),
            "active_action": row.get("active_action", ""),
            "updated_at": row["ai_updated_at"],
        },
    }}


@router.get("/mood/history")
async def get_mood_history(scope: str = Query("ai"), limit: int = Query(20)):
    c = _c()
    rows = c.db.query_all(
        "SELECT * FROM mood_history WHERE scope=? "
        "ORDER BY id DESC LIMIT ?", (scope, limit))
    return {"code": 200, "data": rows}


@router.post("/mood/reset")
async def reset_mood(scope: str | None = Query(None)):
    c = _c()
    c.mood.reset(scope=scope)
    return {"code": 200, "data": {"reset": True}}
