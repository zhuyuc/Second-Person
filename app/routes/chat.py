"""
对话接口（开发文档 §一）。

POST /chat/send（SSE）/ cancel / session active-request / sessions /
messages / feedback / session rename|create|delete / session usage
生成与连接解耦：回复在后台任务中生成并写入缓冲，SSE 只是缓冲的读者。
刷新/断网/关页仅断开读者，生成继续；唯有 POST /chat/cancel（用户手动停止）可取消。
同 crid 重连从头回放缓冲并续跟实时事件；缓冲完成后保留 5 分钟。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.contracts import (
    ContractValidationError,
    ToolApprovalDecisionRequest,
    parse_chat_send,
    read_json_object,
)
from infrastructure.prompt_loader import PROMPTS
from infrastructure.session_metrics import session_metrics
from infrastructure.sse_contract import SSEContractError, validate_sse_event

router = APIRouter()


class ChatCancelRequest(BaseModel):
    client_request_id: str = ""


class ChatFeedbackRequest(BaseModel):
    message_id: int
    feedback: int = Field(ge=0, le=2)
    reason: str | None = None


class SessionRenameRequest(BaseModel):
    session_id: str
    title: str


class SessionHandoffRequest(BaseModel):
    from_session_id: str


class SessionPinRequest(BaseModel):
    session_id: str
    pinned: bool = False


def _load_images_as_data_uri(data_dir, filenames: list[str]) -> list[str]:
    """将已持久化的图片文件名加载为 dataURI，用于编辑消息时继承原图。"""
    import base64
    from pathlib import Path
    out = []
    img_dir = Path(data_dir) / "chat_images"
    ext_mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}
    for fname in filenames:
        p = img_dir / fname
        if not p.exists():
            continue
        mime = ext_mime.get(p.suffix.lower(), "image/png")
        b64 = base64.b64encode(p.read_bytes()).decode()
        out.append(f"data:{mime};base64,{b64}")
    return out or None


# client_request_id -> {events, dropped, done, started, finished, size, sid, task}
_BUFFERS: dict[str, dict] = {}
BUFFER_TTL = 300        # 生成完成后缓冲保留 5 分钟供断线重连
BUFFER_MAX = 1024 * 1024


def _c():
    from app.main import get_container
    return get_container()


_BUFFERS_MAX_COUNT = 200


def _gc_buffers():
    now = time.time()
    for k, v in list(_BUFFERS.items()):
        if v["done"]:
            if now - (v.get("finished") or v["started"]) > BUFFER_TTL:
                _BUFFERS.pop(k, None)
        # 活跃请求不按固定时长取消：深度/长文的完成时间由任务合同决定。
        # 模型、工具和用户主动停止仍各自有超时/取消语义。
    if len(_BUFFERS) > _BUFFERS_MAX_COUNT:
        completed = sorted(
            ((k, v) for k, v in _BUFFERS.items() if v["done"]),
            key=lambda kv: kv[1]["started"])
        for k, _ in completed[:max(0, len(_BUFFERS) - _BUFFERS_MAX_COUNT)]:
            _BUFFERS.pop(k, None)


async def _follow(buf: dict):
    """从头回放缓冲事件并持续跟读到 done（首连与断线重连同一条路径）。
    读者断开只终止本生成器，不影响后台生成任务。"""
    idx = 0
    while True:
        local = idx - buf.get("dropped", 0)
        if local < 0:
            # 缓冲被裁剪（超 1MB）：跳到现存最早事件
            idx = buf.get("dropped", 0)
            continue
        if local < len(buf["events"]):
            e = buf["events"][local]
            idx += 1
            yield {"event": e["event"],
                   "data": json.dumps(e["data"], ensure_ascii=False)}
            continue
        if buf["done"]:
            break
        # 事件驱动：等生产者 nudge 唤醒，最长 1s 兑底（防 nudge 竞态/丢失），
        # 替代固定 20Hz 轮询的空转；超时后重新检查缓冲区保证不丢事件
        ev = buf.get("nudge")
        if ev is not None:
            try:
                await asyncio.wait_for(ev.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            if ev.is_set():
                ev.clear()
        else:
            await asyncio.sleep(0.05)


@router.post("/chat/send")
async def chat_send(request: Request):
    try:
        payload = parse_chat_send(await read_json_object(request))
    except (ContractValidationError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    sid = payload.session_id
    message = payload.message
    crid = payload.client_request_id
    images = payload.images
    regen_id = payload.regenerate_message_id
    edit_message_id = payload.edit_message_id
    location = payload.location
    handoff_path = payload.handoff_path
    reasoning_effort = payload.reasoning_effort
    c = _c()

    _gc_buffers()
    # 断线重连/刷新重挂：已有缓冲则从头回放并续跟（生成在后台继续，不重复计费）
    if crid and crid in _BUFFERS:
        return EventSourceResponse(_follow(_BUFFERS[crid]), ping=5)

    if not sid:
        sid = c.sessions.create_session()
        c.notifications.flush_pending()

    buf = {"events": [], "dropped": 0, "done": False, "started": time.time(),
           "finished": None, "size": 0, "sid": sid, "task": None,
           "nudge": asyncio.Event(), "reasoning_effort": reasoning_effort}
    if crid:
        _BUFFERS[crid] = buf

    # 编辑消息分支化：创建新版本节点，旧版本停用但保留
    edit_parent_id = None
    edit_version_group_id = None
    if edit_message_id:
        orig = c.db.query_one(
            "SELECT id, parent_id, version_group_id, images, content FROM conversations WHERE id=?",
            (edit_message_id,))
        if orig:
            edit_parent_id = orig["parent_id"]
            edit_version_group_id = orig["version_group_id"] or orig["id"]
            # 编辑时继承附件：当前版本可能无附件（修复前创建），
            # 从同 version_group 所有兄弟版本中查找有附件的记录
            if not images:
                img_row = c.db.query_one(
                    "SELECT images FROM conversations "
                    "WHERE version_group_id=? AND images IS NOT NULL "
                    "ORDER BY id LIMIT 1",
                    (edit_version_group_id,))
                if img_row and img_row["images"]:
                    images = _load_images_as_data_uri(
                        c.sessions.data_dir, json.loads(img_row["images"]))
            # 编辑时继承文档附件上下文（【附件：...】前缀）
            att_content = orig["content"] or ""
            if "\n---\n" not in att_content or "【附件：" not in att_content:
                att_row = c.db.query_one(
                    "SELECT content FROM conversations "
                    "WHERE version_group_id=? AND content LIKE '%【附件：%' "
                    "ORDER BY id LIMIT 1",
                    (edit_version_group_id,))
                if att_row:
                    att_content = att_row["content"] or ""
            if "\n---\n" in att_content and "【附件：" in att_content:
                att_prefix = att_content.rsplit("\n---\n", 1)[0]
                message = att_prefix + "\n---\n" + message
            # 同组旧版本停用
            c.db.execute(
                "UPDATE conversations SET is_active=0 WHERE version_group_id=?",
                (edit_version_group_id,))
            # 旧版本的下游链也需停用
            siblings = c.db.query_all(
                "SELECT id FROM conversations WHERE version_group_id=?",
                (edit_version_group_id,))
            for sib in siblings:
                c.sessions._deactivate_downstream(sib["id"])

    # 重新生成分支化：旧回复保留，创建 assistant 兄弟节点
    regen_parent_id = None
    regen_version_group_id = None
    if regen_id and not edit_message_id:
        orig = c.db.query_one(
            "SELECT id, parent_id, version_group_id FROM conversations WHERE id=?",
            (regen_id,))
        if orig:
            regen_parent_id = orig["parent_id"]
            regen_version_group_id = orig["version_group_id"] or orig["id"]
            # 同组旧版本停用
            c.db.execute(
                "UPDATE conversations SET is_active=0 WHERE version_group_id=?",
                (regen_version_group_id,))
            # 旧版本的下游链也停用
            siblings = c.db.query_all(
                "SELECT id FROM conversations WHERE version_group_id=?",
                (regen_version_group_id,))
            for sib in siblings:
                c.sessions._deactivate_downstream(sib["id"])

    # 首条消息后异步生成标题
    row = c.db.query_one(
        "SELECT message_count FROM sessions WHERE session_id=?", (sid,))
    is_first = row and row["message_count"] == 0
    if is_first:
        asyncio.create_task(_gen_title(c, sid, message))

    # P1-D：用户否认信号 → 即时抑制该会话最近入池的候选，避免下轮回顾复原
    try:
        from memory.write_gate import has_denial_signal
        if message and has_denial_signal(message) and getattr(c, "memory_gate", None):
            suppressed = c.memory_gate.suppress_recent_from_denial(
                sid, minutes=int(c.config.get("memory_denial_suppress_window_minutes", 30)))
            if suppressed:
                logging.getLogger("second_person.chat").info(
                    "用户否认信号触发抑制：session=%s 影响候选=%d", sid, suppressed)
    except Exception:  # noqa: BLE001 - 抑制失败不阻塞主流程
        logging.getLogger("second_person.chat").warning(
            "denial-signal suppress failed", exc_info=True)

    async def produce():
        """后台消费 Agent 事件流写入缓冲：SSE 断开不影响生成，
        仅 /chat/cancel（用户手动停止）可取消。"""
        try:
            # 编辑模式：edit_parent_id/edit_version_group_id 传入管线，
            # 管线写入用户消息时使用这些字段建立树关系
            _ep = edit_parent_id
            _evg = edit_version_group_id
            # 重新生成模式：用户消息不重写（已存在），只重新生成 assistant 回复
            # 传入 regen_parent_id 让新 assistant 回复挂到正确的 parent
            if regen_id and not edit_message_id:
                _ep = regen_parent_id
                _evg = regen_version_group_id
            async for evt in c.core.run(sid, message, crid, images=images,
                                        regenerate=bool(regen_id),
                                        regenerate_message_id=regen_id,
                                        location=location,
                                        handoff_path=handoff_path,
                                        reasoning_effort=reasoning_effort,
                                        edit_parent_id=_ep,
                                        edit_version_group_id=_evg):
                try:
                    validate_sse_event(evt.get("event", ""), evt.get("data"))
                except SSEContractError as exc:
                    logging.getLogger("second_person.chat").error(
                        "SSE event contract violation: %s", exc)
                    buf["events"].append({"event": "error", "data": {
                        "code": 500, "message": "服务端事件协议错误"}})
                    break
                buf["events"].append(evt)
                buf["size"] += len(json.dumps(evt.get("data", {})))
                if buf["size"] > BUFFER_MAX:
                    cut = len(buf["events"]) - 50
                    if cut > 0:
                        buf["events"] = buf["events"][-50:]
                        buf["dropped"] += cut
                buf["nudge"].set()   # 唤醒跟读者
        except asyncio.CancelledError:
            buf["events"].append({"event": "error",
                                  "data": {"code": 499, "message": "已停止生成"}})
            raise
        finally:
            buf["done"] = True
            buf["finished"] = time.time()
            buf["nudge"].set()   # 终态唤醒，让跟读者及时退出

    buf["task"] = asyncio.create_task(produce())
    return EventSourceResponse(_follow(buf), ping=5)


@router.get("/chat/reasoning-efforts")
async def reasoning_efforts():
    """Return the selected model's supported levels, with legacy fallback."""
    capabilities = _c().providers.capability_snapshot("chat")
    supported = capabilities.get("reasoning_efforts") or ["off", "low", "high", "max"]
    labels = {"off": "关闭推理", "low": "低", "high": "高", "max": "最大"}
    return {"code": 200, "data": [
        {"value": value, "label": labels.get(value, value)}
        for value in supported
    ], "capabilities": capabilities}


@router.get("/chat/model-capabilities")
async def model_capabilities():
    """Provider-neutral capability catalog for the model selector."""
    return {"code": 200, "data": _c().providers.capability_snapshot("chat")}


@router.get("/chat/turns/{turn_id}")
async def get_turn(turn_id: str):
    turn = _c().core.get_turn(turn_id)
    if not turn:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"code": 200, "data": turn}


@router.get("/chat/turns/{turn_id}/events")
async def get_turn_events(turn_id: str, after_seq: int = 0):
    if not _c().core.get_turn(turn_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"code": 200, "data": _c().core.get_turn_events(turn_id, after_seq)}


@router.post("/chat/turns/{turn_id}/approvals/{approval_id}")
async def decide_tool_approval(turn_id: str, approval_id: str,
                               body: ToolApprovalDecisionRequest):
    row = _c().core.decide_tool_approval(approval_id, body.approved)
    if not row or row["turn_id"] != turn_id:
        raise HTTPException(status_code=404, detail="待确认工具不存在或已处理")
    return {"code": 200, "data": {
        "approval_id": approval_id,
        "turn_id": turn_id,
        "status": row["status"],
    }}


class SwitchVersionRequest(BaseModel):
    session_id: str
    version_group_id: int
    target_message_id: int


@router.post("/chat/switch-version")
async def switch_version(body: SwitchVersionRequest):
    """切换消息版本：同组全部停用 → 目标激活 → 递归激活下游链。
    返回更新后的活跃分支消息列表。"""
    c = _c()
    c.sessions.switch_version(body.session_id, body.version_group_id,
                              body.target_message_id)
    msgs = c.sessions.get_messages(body.session_id)
    return {"code": 200, "data": {"messages": msgs}}


@router.get("/chat/version-siblings")
async def version_siblings(version_group_id: int):
    """返回同一版本组的所有兄弟消息 ID 列表（按创建顺序）。"""
    c = _c()
    rows = c.db.query_all(
        "SELECT id, content, create_time FROM conversations "
        "WHERE version_group_id=? ORDER BY id",
        (version_group_id,))
    return {"code": 200, "data": [
        {"id": r["id"], "content": (r["content"] or "")[:80],
         "create_time": r["create_time"]} for r in rows]}


@router.post("/chat/cancel")
async def chat_cancel(body: ChatCancelRequest):
    """用户手动停止生成：取消后台生成任务（已产出部分由中断补救落库）。
    除此接口外，任何连接层动作（刷新/断网/关页）都不中断生成。"""
    buf = _BUFFERS.get(body.client_request_id or "")
    task = buf.get("task") if buf else None
    if task and not task.done():
        task.cancel()
        return {"code": 200, "data": {"cancelled": True}}
    return {"code": 200, "data": {"cancelled": False}}


async def _generate_handoff(c, from_sid: str, to_sid: str) -> None:
    """后台异步生成 handoff 摘要（会话上下文管理方案 v2）。

    不阻塞路由返回，失败静默降级为 status=failed 的占位文件。
    """
    from memory.handoff_summary import HandoffSummaryGenerator
    from langfuse.integration import get_tracer
    tracer = get_tracer()
    trace = tracer.trace_start(
        "handoff.summary", session_id=to_sid,
        input={"from_session_id": from_sid, "to_session_id": to_sid})
    try:
        gen = HandoffSummaryGenerator(
            llm=c.llm, db=c.db, data_dir=c.data_dir,
            config=c.config, bus=c.bus, tracer=tracer)
        await gen.generate(from_sid, to_sid)
        trace.end(output={"status": "completed"})
    except Exception as e:  # noqa: BLE001
        trace.end(level="ERROR", status_message=str(e))
        logging.getLogger("second_person.chat").warning(
            "handoff 摘要生成失败 from=%s to=%s: %s", from_sid, to_sid, e)
        c.db.execute(
            "UPDATE sessions SET handoff_summary_path='__failed__' "
            "WHERE session_id=?", (to_sid,))


async def _gen_title(c, sid: str, message: str):
    """首条消息异步生成标题。

    会话创建时已持久化为“新对话”。只有模型返回有效标题时才回填，
    因此用户的原始输入不会短暂地成为侧边栏标题。标题生成不纳入
    Langfuse 可观测性上报。
    """
    try:
        snap = c.providers.snapshot_for("agent")
        q = message.split(
            "\n---\n")[-1].strip() if "\n---\n" in message else message
        q = q[:500]
        if snap:
            async def _call_llm():
                try:
                    from infrastructure.json_repair import repair_json
                    resp = await c.llm.chat(snap, [
                        {"role": "system", "content": PROMPTS.load_raw(
                            "app/prompts/title_gen")},
                        {"role": "user", "content": q}], source="title_gen",
                        session_id=sid, json_mode=True)
                    raw = resp["content"].strip()
                    try:
                        obj = repair_json(raw)
                        return (obj.get("title") or "")[:15]
                    except (ValueError, AttributeError):
                        return raw[:15]
                except Exception:  # noqa: BLE001
                    return None

            result = await _call_llm()
            if result:
                c.sessions.set_auto_title(sid, result)
    except Exception:  # noqa: BLE001
        logger = logging.getLogger("second_person.chat")
        logger.warning("会话标题生成失败 session=%s", sid, exc_info=True)


@router.get("/chat/session/{session_id}/active-request")
async def active_request(session_id: str):
    # 仅返回本会话的进行中请求，避免多会话并发时跨会话误命中
    for crid, buf in _BUFFERS.items():
        if not buf["done"] and buf.get("sid") == session_id:
            return {"code": 200, "data": {"client_request_id": crid,
                                          "status": "generating"}}
    return {"code": 200, "data": None}


@router.get("/chat/sessions")
async def sessions(keyword: str = None, page: int = 1, page_size: int = 20):
    return {"code": 200, "data": _c().sessions.list_sessions(keyword, page, page_size)}


@router.get("/chat/messages")
async def messages(session_id: str, before_id: int = None, limit: int = 50):
    return {"code": 200, "data": _c().sessions.get_messages(session_id, before_id, limit)}


@router.get("/chat/session/{session_id}/metrics")
async def session_metrics_route(session_id: str):
    """Return whole-session counters and the latest completed-turn speed."""
    c = _c()
    if not c.db.query_one("SELECT 1 FROM sessions WHERE session_id=?", (session_id,)):
        raise HTTPException(status_code=404, detail="会话不存在")
    latest = c.db.query_one(
        "SELECT id FROM agent_turns WHERE session_id=? AND status='completed' "
        "ORDER BY created_at DESC LIMIT 1",
        (session_id,))
    return {"code": 200, "data": session_metrics(
        c.db, session_id, current_turn_id=latest["id"] if latest else None)}


@router.post("/chat/feedback")
async def feedback(body: ChatFeedbackRequest):
    c = _c()
    mid = body.message_id
    fb = body.feedback
    reason = body.reason
    from langfuse.integration import get_tracer
    msg_row = c.db.query_one(
        "SELECT session_id FROM conversations WHERE id=?", (mid,))
    tr = get_tracer().trace_start("user_feedback", input={
        "message_id": mid, "feedback": fb, "reason": reason,
        "session_id": msg_row["session_id"] if msg_row else None})
    try:
        c.sessions.set_feedback(mid, fb)
        if fb == 2:
            # 无 reason 的点踩同样记录显式负反馈信号（画像/风格学习依赖完整采集）
            c.signals.set_explicit_reaction(mid, 2)
            if reason:
                await _handle_downvote(c, mid, reason)
        elif fb == 1:
            c.signals.set_explicit_reaction(mid, 1)
            await _handle_upvote(c, mid)
        tr.end()
    except Exception:
        tr.end(level="ERROR")
        raise
    return {"code": 200, "data": {}}


async def _handle_upvote(c, message_id: int):
    """点赞：对该回复引用的每条记忆执行 confidence 升级（medium→strong）。"""
    row = c.db.query_one(
        "SELECT session_id, citations FROM conversations WHERE id=?", (message_id,))
    cites = json.loads(row["citations"]) if row and row["citations"] else []
    for cit in cites:
        if cit.get("id"):
            await c.lifecycle.upvote_upgrade(cit["id"])

    # v2 情绪触发：点赞 → AI pleased
    if hasattr(c, "mood_trigger") and c.mood_trigger and row:
        c.mood_trigger.record(
            session_id=row["session_id"], message_id=message_id,
            scope="ai", source_type="evaluation", event_key="user_thumbs_up",
            attribution="other", mood_hint="pleased", intensity_hint=0.4,
            note="用户点赞")


async def _handle_downvote(c, message_id: int, reason: str):
    row = c.db.query_one(
        "SELECT session_id, content, citations FROM conversations WHERE id=?", (message_id,))
    cites = json.loads(row["citations"]) if row and row["citations"] else []
    if reason == "memory_stale":
        for cit in cites:
            await c.lifecycle.downvote_stale(cit.get("id"))
    elif reason == "tone_wrong":
        # 改走画像审核队列，不再通过 ctx_entry.add_pending 直接注入对话
        if hasattr(c, "conflict_scanner") and c.conflict_scanner:
            context_snippet = (row["content"] or "")[:300] if row else ""
            c.conflict_scanner.enqueue_tone_review(
                message_id,
                row["session_id"] if row else "",
                context_snippet,
            )
    # output_format_wrong 已在 signal 负反馈记录

    # v2 情绪触发：点踩 → AI concerned
    if hasattr(c, "mood_trigger") and c.mood_trigger and row:
        c.mood_trigger.record(
            session_id=row["session_id"], message_id=message_id,
            scope="ai", source_type="evaluation", event_key="user_thumbs_down",
            mood_hint="concerned", intensity_hint=0.4,
            note=f"用户点踩：{reason or '无原因'}")


@router.post("/chat/session/rename")
async def rename(body: SessionRenameRequest):
    _c().sessions.rename(body.session_id, body.title)
    return {"code": 200, "data": {}}


@router.post("/chat/session/create")
async def create():
    return {"code": 200, "data": {"session_id": _c().sessions.create_session()}}


@router.post("/chat/session/handoff")
async def session_handoff(body: SessionHandoffRequest):
    """开启新会话并触发 handoff 摘要生成（会话上下文管理方案 v2）。

    body: { from_session_id: str }
    返回: { new_session_id, from_session_id }
    """
    from fastapi import HTTPException
    from infrastructure.timeutil import now_cst
    from_sid = body.from_session_id.strip()
    if not from_sid:
        raise HTTPException(400, "缺少 from_session_id")
    c = _c()
    # 校验旧会话存在且未 readonly
    row = c.db.query_one(
        "SELECT readonly FROM sessions WHERE session_id=?", (from_sid,))
    if not row:
        raise HTTPException(404, "会话不存在")
    if row["readonly"]:
        raise HTTPException(409, "会话已结束，不可重复切换")
    # 创建新会话
    new_sid = c.sessions.create_session(from_session=from_sid)
    # 标记旧会话
    now = now_cst().isoformat(timespec="seconds")
    c.db.execute(
        "UPDATE sessions SET readonly=1, ended_at=?, succeeded_by=?"
        " WHERE session_id=?", (now, new_sid, from_sid))
    # 异步生成摘要
    if c.config.get("handoff_summary_enabled", True):
        asyncio.create_task(_generate_handoff(c, from_sid, new_sid))
    return {"code": 200, "data": {
        "new_session_id": new_sid,
        "from_session_id": from_sid,
    }}


@router.get("/chat/session/{session_id}/handoff-status")
async def handoff_status(session_id: str):
    """查询 handoff 摘要状态（会话上下文管理方案 v2）。

    返回: { status: "generating" | "ready" | "failed" | null }
    """
    row = _c().db.query_one(
        "SELECT handoff_summary_path FROM sessions WHERE session_id=?",
        (session_id,))
    if not row:
        return {"code": 200, "data": {"status": None}}
    from agent.session_context import _handoff_status_from_row
    return {"code": 200, "data": {"status": _handoff_status_from_row(row)}}


@router.post("/chat/session/pin")
async def pin(body: SessionPinRequest):
    _c().sessions.set_pinned(body.session_id, body.pinned)
    return {"code": 200, "data": {}}


# 对话附件：上传并解析为文本（供当前提问作为上下文，不入记忆库）
# 不对解析文本做截断：现代模型上下文窗口足够（百万级），完整交给模型。
ATTACH_MAX_MB = 20


@router.post("/chat/attachment")
async def upload_attachment(file: UploadFile = File(...)):
    import tempfile
    import os
    from pathlib import Path
    from scheduler.ingest import extract_text
    from langfuse.integration import get_tracer
    tr = get_tracer().trace_start("attachment_upload", input={
        "filename": file.filename,
        "size_bytes": file.size if file.size is not None else 0})
    content = await file.read()
    if len(content) > ATTACH_MAX_MB * 1024 * 1024:
        tr.end(level="ERROR", output={"ok": False, "error": "超过 20MB 上限"})
        return {"code": 400, "message": f"文件超过 {ATTACH_MAX_MB} MB 上限",
                "trace_id": None, "details": None}
    suffix = Path(file.filename or "file").suffix
    tmp = Path(tempfile.gettempdir()) / \
        f"sp_attach_{os.getpid()}_{int(time.time()*1000)}{suffix}"

    def _parse() -> str:
        # 临时文件写盘（最大 20MB）与 docx/pdf 解析均为同步重操作，
        # 整体丢工作线程，避免上传附件冻结自己与其他会话的 SSE 流
        try:
            tmp.write_bytes(content)
            # 仅针对文档抽文本；图片不走此路（前端直接作多模态图发送）
            return extract_text(tmp) or ""
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    try:
        text = await asyncio.to_thread(_parse)
    except Exception as e:  # noqa: BLE001
        tr.end(level="ERROR", output={"ok": False, "error": str(e)[:500]})
        raise
    tr.end(output={"ok": bool(text.strip()), "chars": len(text),
                   "parsed": bool(text.strip()), "truncated": False})
    return {"code": 200, "data": {
        "filename": file.filename, "chars": len(text),
        "text": text, "truncated": False,
        "parsed": bool(text.strip()),
    }}


@router.delete("/chat/session/{session_id}")
async def delete(session_id: str):
    _c().sessions.delete_session(session_id)
    return {"code": 200, "data": {}}
