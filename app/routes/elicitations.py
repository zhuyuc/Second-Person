"""
追问式补充信息模块 · API 端点（对应产品方案 §07 后端接口与流转）。

端点清单：
  POST /chat/elicitations/{tool_use_id}/answer    — 提交追问答案（一次性全部）
  POST /chat/elicitations/{tool_use_id}/close     — 关闭追问
  GET  /chat/elicitations/pending?session_id=xxx  — 查询会话是否有 pending 追问
  POST /chat/elicitations/{tool_use_id}/rendered  — 前端回传渲染耗时（Langfuse 埋点用）
"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

router = APIRouter()


# ---- Pydantic 请求/响应模型 ------------------------------------------------

class ElicitationAnswerRequest(BaseModel):
    answers: list[dict] = Field(..., min_length=1, max_length=3,
                                description="完整答案数组 [{question_id, type, value}]")
    client_request_id: str = Field(default="",
                                   description="前端生成的请求 ID，供多 tab 接管")


class ElicitationCloseRequest(BaseModel):
    answers: list[dict] = Field(default_factory=list, max_length=3,
                                description="已答部分的 partial answers")
    client_request_id: str = Field(default="")


class ElicitationRenderedRequest(BaseModel):
    render_ms: int = Field(..., ge=0, description="从 SSE 事件接收到 UI 渲染完成的毫秒数")


# ---- 辅助函数 ----------------------------------------------------------------

def _c():
    from app.main import get_container
    return get_container()


def _time_now_sec() -> int:
    return int(time.time())


# ---- 端点 -------------------------------------------------------------------

@router.post("/chat/elicitations/{tool_use_id}/answer")
async def elicitation_answer(tool_use_id: str, body: ElicitationAnswerRequest):
    """提交追问答案（一次性全部）。后端写入答案并唤醒挂起的 pipeline。

    当前 SSE 连接不中断：pipeline 被唤醒后继续产出 content_delta。
    """
    from agent.elicitation_state import get as get_state

    state = get_state(tool_use_id)
    if not state:
        # 内存中没有 → 尝试从 DB 恢复（可能服务重启丢失了内存状态）
        # 但如果没有活跃的 pipeline 在等待，答案无法被消费
        raise HTTPException(
            404, f"elicitation {tool_use_id} not found or already resolved")

    if state.is_resolved:
        raise HTTPException(
            409, f"elicitation {tool_use_id} already resolved ({state.status.value})")

    # 写入答案并唤醒
    answers_json = json.dumps(body.answers, ensure_ascii=False)
    state.answer(answers_json)

    # 持久化
    c = _c()
    c.db.execute(
        "UPDATE elicitations SET status='answered_all', answers_json=?, resolved_at=? WHERE id=?",
        (answers_json, _time_now_sec(), tool_use_id))

    return {"code": 200, "data": {"status": "answered_all"}}


@router.post("/chat/elicitations/{tool_use_id}/close")
async def elicitation_close(tool_use_id: str, body: ElicitationCloseRequest):
    """用户关闭追问。写入 partial 答案并唤醒 pipeline。"""
    from agent.elicitation_state import get as get_state

    state = get_state(tool_use_id)
    if not state:
        raise HTTPException(404, f"elicitation {tool_use_id} not found")
    if state.is_resolved:
        raise HTTPException(409, f"elicitation {tool_use_id} already resolved")

    answers_json = json.dumps(
        body.answers, ensure_ascii=False) if body.answers else None
    state.close("user_x", answers_json)

    c = _c()
    c.db.execute(
        "UPDATE elicitations SET status='closed', close_reason='user_x', "
        "answers_json=?, resolved_at=? WHERE id=?",
        (answers_json, _time_now_sec(), tool_use_id))

    # 标记 session 不再触发追问
    c.db.execute(
        "UPDATE sessions SET elicitation_blocked=1 WHERE session_id=?",
        (state.session_id,))

    return {"code": 200, "data": {"status": "closed"}}


@router.get("/chat/elicitations/pending")
async def elicitation_pending(session_id: str = Query(...)):
    """查询会话是否有 pending 追问（断线重连恢复用）。"""
    from agent.elicitation_state import get_pending_for_session

    c = _c()
    state = get_pending_for_session(c.db, session_id)
    if not state:
        return {"code": 200, "data": {"pending": False}}

    questions = json.loads(state.questions_json)
    return {
        "code": 200,
        "data": {
            "pending": True,
            "tool_use_id": state.id,
            "questions": questions,
            "status": state.status.value,
            "answers_json": state.answers_json,
        },
    }


@router.post("/chat/elicitations/{tool_use_id}/rendered")
async def elicitation_rendered(tool_use_id: str, body: ElicitationRenderedRequest):
    """前端回传渲染耗时，记录到 Langfuse span。"""
    from observability_langfuse import get_tracer
    tracer = get_tracer()
    sp = tracer.span_start("elicitation_rendered", input={
        "tool_use_id": tool_use_id,
        "render_ms": body.render_ms,
    })
    sp.end(output={"render_ms": body.render_ms})
    return {"code": 200, "data": {"recorded": True}}
