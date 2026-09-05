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
    parse_chat_send,
    read_json_object,
)
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
    """将已持久化的图片文件名加载为 dataURI，用于编辑消息时继承原图。

    安全：filenames 来自 DB JSON，可能被污染。这里校验每个 fname 必须是
    纯文件名（无路径分隔、无 ..），且 resolve 后仍在 img_dir 内，避免越界读。
    本函数同步读盘 + base64，调用方应放 asyncio.to_thread 避免阻塞事件循环。
    """
    import base64
    from pathlib import Path
    out = []
    img_dir = (Path(data_dir) / "chat_images").resolve()
    ext_mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}
    for fname in filenames:
        if not fname or "/" in fname or "\\" in fname or ".." in fname:
            continue
        p = (img_dir / fname).resolve()
        try:
            p.relative_to(img_dir)
        except ValueError:
            continue
        if not p.exists():
            continue
        mime = ext_mime.get(p.suffix.lower(), "image/png")
        b64 = base64.b64encode(p.read_bytes()).decode()
        out.append(f"data:{mime};base64,{b64}")
    return out or None


# client_request_id -> {events, dropped, done, started, finished, size, sid, task}
_BUFFERS: dict[str, dict] = {}
# 保护 _BUFFERS dict 本身的结构变更（增删/遍历/命中判断），不覆盖 buf 内部字段。
# 只包裹**纯 dict 操作**的短临界区，绝不在持锁时 await 长耗时协程（如 _follow 的 nudge
# 等待），因此不会造成生产者/读者互相阻塞或死锁。
# 单进程 asyncio.Lock 已足够：全部访问点都在同一事件循环内。
_BUFFERS_LOCK: asyncio.Lock | None = None
_BUFFERS_LOOP: asyncio.AbstractEventLoop | None = None
BUFFER_TTL = 300        # 生成完成后缓冲保留 5 分钟供断线重连
BUFFER_MAX = 1024 * 1024


def _c():
    from app.main import get_container
    return get_container()


_BUFFERS_MAX_COUNT = 200


def _buffers_lock() -> asyncio.Lock:
    """惰性创建锁；若当前 event loop 与上次不同（测试常见：多 asyncio.run），
    重建以避免 asyncio.Lock 绑定失效 loop 时的运行时错误。"""
    global _BUFFERS_LOCK, _BUFFERS_LOOP
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _BUFFERS_LOCK is None or _BUFFERS_LOOP is not loop:
        _BUFFERS_LOCK = asyncio.Lock()
        _BUFFERS_LOOP = loop
    return _BUFFERS_LOCK


async def _gc_buffers():
    """按 TTL 与总量上限回收已完成的缓冲项。整段持锁 —— 内部只做纯 dict 操作。"""
    now = time.time()
    async with _buffers_lock():
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
    attachments_overridden = payload.attachments_overridden
    keep_image_names = payload.keep_image_names
    location = payload.location
    handoff_path = payload.handoff_path
    reasoning_effort = payload.reasoning_effort
    c = _c()

    await _gc_buffers()
    # 断线重连/刷新重挂：已有缓冲则从头回放并续跟（生成在后台继续，不重复计费）。
    # 只在临界区做 dict 查询，拿到 buf 引用后立即释放锁再返回 SSE 流 —— 不能持锁
    # 进入 _follow，否则读者阻塞在 nudge 等待会锁死所有其他 buffer 操作。
    if crid:
        async with _buffers_lock():
            existing = _BUFFERS.get(crid)
        if existing is not None:
            return EventSourceResponse(_follow(existing), ping=5)

    if not sid:
        # M5.1：project_id 从请求带入，实现「新建项目会话」延迟创建
        # （避免用户点了「+ 新建会话」但没输入内容就切走，留一堆空会话）
        pid = payload.project_id
        if pid:
            proj = c.projects.get(pid)
            if not proj or proj.status != "active":
                pid = None      # 静默降级，不阻塞对话
        sid = c.sessions.create_session(project_id=pid)
        c.notifications.flush_pending()

    buf = {"events": [], "dropped": 0, "done": False, "started": time.time(),
           "finished": None, "size": 0, "sid": sid, "task": None,
           "nudge": asyncio.Event(), "reasoning_effort": reasoning_effort}
    if crid:
        async with _buffers_lock():
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
            if attachments_overridden:
                # 前端已接管附件：message 已包含用户确认的【附件：】前缀，
                # 后端不再重建文档前缀；图片仅继承 keep_image_names 命中的旧图，
                # 再拼接前端本次新上传的 images（data URI）。
                kept: list[str] = []
                if keep_image_names:
                    keep_set = set(keep_image_names)
                    rows = c.db.query_all(
                        "SELECT images FROM conversations "
                        "WHERE version_group_id=? AND images IS NOT NULL",
                        (edit_version_group_id,))
                    ordered: list[str] = []
                    seen: set[str] = set()
                    for r in rows:
                        try:
                            for fname in json.loads(r["images"]) or []:
                                if fname in keep_set and fname not in seen:
                                    seen.add(fname)
                                    ordered.append(fname)
                        except (TypeError, ValueError):
                            continue
                    if ordered:
                        loaded = await asyncio.to_thread(
                            _load_images_as_data_uri, c.sessions.data_dir, ordered)
                        kept = loaded or []
                images = kept + (images or [])
                images = images or None
            else:
                # 旧行为（兼容未升级前端）：自动继承附件。
                # 编辑时继承附件：当前版本可能无附件（修复前创建），
                # 从同 version_group 所有兄弟版本中查找有附件的记录
                if not images:
                    img_row = c.db.query_one(
                        "SELECT images FROM conversations "
                        "WHERE version_group_id=? AND images IS NOT NULL "
                        "ORDER BY id LIMIT 1",
                        (edit_version_group_id,))
                    if img_row and img_row["images"]:
                        images = await asyncio.to_thread(
                            _load_images_as_data_uri,
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
            # 停用被编辑消息之后的旧链（旧 assistant 等），兼容 parent_id 缺失的存量数据
            c.db.execute(
                "UPDATE conversations SET is_active=0 "
                "WHERE session_id=? AND id>?",
                (sid, edit_message_id))

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

    # 首条消息后异步生成标题（侧边会话不进列表，标题无消费方，跳过省成本）
    row = c.db.query_one(
        "SELECT message_count, channel FROM sessions WHERE session_id=?", (sid,))
    is_first = row and row["message_count"] == 0
    if is_first and row["channel"] != "aside":
        from infrastructure.background_tasks import track_task
        track_task(c.chat_svc.generate_title(sid, message),
                   name=f"generate_title:{sid}")

    # P1-D：用户否认信号 → 即时抑制该会话最近入池的候选，避免下轮回顾复原
    try:
        from memory.write_gate import has_denial_signal
        if message and has_denial_signal(message) and getattr(c, "memory_gate", None):
            from memory import _constants as _mem_const
            suppressed = c.memory_gate.suppress_recent_from_denial(
                sid, minutes=_mem_const.DENIAL_SUPPRESS_WINDOW_MINUTES)
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
    async with _buffers_lock():
        buf = _BUFFERS.get(body.client_request_id or "")
        task = buf.get("task") if buf else None
    if task and not task.done():
        task.cancel()
        return {"code": 200, "data": {"cancelled": True}}
    return {"code": 200, "data": {"cancelled": False}}


@router.get("/chat/session/{session_id}/active-request")
async def active_request(session_id: str):
    # 仅返回本会话的进行中请求，避免多会话并发时跨会话误命中。
    # 持锁快照后再遍历，避免 chat_send 并发写入 _BUFFERS 触发
    # "dictionary changed size during iteration"。
    async with _buffers_lock():
        snapshot = list(_BUFFERS.items())
    for crid, buf in snapshot:
        if not buf["done"] and buf.get("sid") == session_id:
            return {"code": 200, "data": {"client_request_id": crid,
                                          "status": "generating"}}
    return {"code": 200, "data": None}


@router.get("/chat/sessions")
async def sessions(keyword: str = None, page: int = 1, page_size: int = 20):
    return {"code": 200, "data": _c().sessions.list_sessions(keyword, page, page_size)}


@router.get("/chat/search")
async def chat_search(q: str = "", scope: str = "all", limit: int = 30):
    """跨会话搜索：命中标题 / 用户消息 / AI 回复，按会话聚合并高亮。

    scope: all | title | user | assistant （非法值降级为 all）
    """
    return {"code": 200,
            "data": _c().sessions.search_conversations(q, scope, limit)}


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
        await c.chat_svc.handle_feedback(mid, fb, reason)
        tr.end()
    except Exception:
        tr.end(level="ERROR")
        raise
    return {"code": 200, "data": {}}


@router.post("/chat/session/rename")
async def rename(body: SessionRenameRequest):
    _c().sessions.rename(body.session_id, body.title)
    return {"code": 200, "data": {}}


class SessionCreateRequest(BaseModel):
    project_id: str | None = None
    # 侧边会话：channel 固定为 'aside'（Web 端不得伪造 IM 渠道），
    # from_session 记录发起它的主会话（供级联删除与 Langfuse 关联）。
    aside: bool = False
    from_session: str | None = None


@router.post("/chat/session/create")
async def create(body: SessionCreateRequest | None = None):
    c = _c()
    project_id = body.project_id if body else None
    if project_id:
        proj = c.projects.get(project_id)
        if not proj:
            raise HTTPException(404, f"项目不存在：{project_id}")
        if proj.status != "active":
            raise HTTPException(409, f"项目已归档：{project_id}")
    channel = "aside" if (body and body.aside) else None
    from_session = (body.from_session if (body and body.aside) else None)
    return {"code": 200, "data": {
        "session_id": c.sessions.create_session(
            project_id=project_id, channel=channel, from_session=from_session)}}


class SessionArchiveRequest(BaseModel):
    session_id: str


@router.post("/chat/session/archive")
async def archive_session(body: SessionArchiveRequest):
    _c().sessions.archive_session(body.session_id, source="manual")
    return {"code": 200, "data": {}}


@router.post("/chat/session/unarchive")
async def unarchive_session(body: SessionArchiveRequest):
    _c().sessions.unarchive_session(body.session_id)
    return {"code": 200, "data": {}}


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
        from infrastructure.background_tasks import track_task
        track_task(c.chat_svc.generate_handoff(from_sid, new_sid),
                   name=f"generate_handoff:{from_sid}")
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
        raise HTTPException(status_code=400, detail=f"文件超过 {ATTACH_MAX_MB} MB 上限")
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
    c = _c()
    c.sessions.delete_session(session_id)
    # 清理 AgentCore 的会话级 lock/queue 计数，避免长跑进程内存泄漏
    c.core.cleanup_session(session_id)
    return {"code": 200, "data": {}}
