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
import time

from fastapi import APIRouter, Request, UploadFile, File
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from infrastructure.prompt_loader import PROMPTS

router = APIRouter()

# client_request_id -> {events, dropped, done, started, finished, size, sid, task}
_BUFFERS: dict[str, dict] = {}
BUFFER_TTL = 300        # 生成完成后缓冲保留 5 分钟供断线重连
BUFFER_HARD_TTL = 900   # 硬上限：超时仍未完成（流水线自身 600s 超时）取消并回收
BUFFER_MAX = 1024 * 1024


def _c():
    from app.main import get_container
    return get_container()


def _gc_buffers():
    now = time.time()
    for k, v in list(_BUFFERS.items()):
        if v["done"]:
            if now - (v.get("finished") or v["started"]) > BUFFER_TTL:
                _BUFFERS.pop(k, None)
        elif now - v["started"] > BUFFER_HARD_TTL:
            t = v.get("task")
            if t and not t.done():
                t.cancel()
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
    body = await request.json()
    sid = body.get("session_id")
    message = body.get("message", "")
    crid = body.get("client_request_id")
    images = body.get("images") or None
    regen_id = body.get("regenerate_message_id")
    # 浏览器定位（可选）：前端 Geolocation + 逆地理编码后随消息携带
    location = (body.get("location") or "").strip()[:60] or None
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
           "nudge": asyncio.Event()}
    if crid:
        _BUFFERS[crid] = buf

    # 重新生成语义：先删除被重生成的 assistant 回复及其对应用户消息，
    # 再按正常流程生成（新的一轮重新落库），避免会话里留下重复轮次
    if regen_id:
        c.sessions.delete_turn(sid, int(regen_id))

    # 首条消息后异步生成标题
    row = c.db.query_one(
        "SELECT message_count FROM sessions WHERE session_id=?", (sid,))
    is_first = row and row["message_count"] == 0
    if is_first:
        asyncio.create_task(_gen_title(c, sid, message))

    async def produce():
        """后台消费 Agent 事件流写入缓冲：SSE 断开不影响生成，
        仅 /chat/cancel（用户手动停止）可取消。"""
        try:
            async for evt in c.core.run(sid, message, crid, images=images,
                                        regenerate=bool(regen_id),
                                        regenerate_message_id=regen_id,
                                        location=location):
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


@router.post("/chat/cancel")
async def chat_cancel(request: Request):
    """用户手动停止生成：取消后台生成任务（已产出部分由中断补救落库）。
    除此接口外，任何连接层动作（刷新/断网/关页）都不中断生成。"""
    body = await request.json()
    buf = _BUFFERS.get(body.get("client_request_id") or "")
    task = buf.get("task") if buf else None
    if task and not task.done():
        task.cancel()
        return {"code": 200, "data": {"cancelled": True}}
    return {"code": 200, "data": {"cancelled": False}}


async def _gen_title(c, sid: str, message: str):
    """首条消息异步生成 10 字标题；3 秒超时退化为取前 15 字符。
    超时后 LLM 调用仍继续完成（shielding），晚到时覆盖已退化标题。
    在独立 Langfuse span 内执行，确保 generation 挂在 trace 下。"""
    from observability_langfuse import get_tracer
    tracer = get_tracer()
    span = tracer.span_start("title_generation", input={
                             "session_id": sid, "message": message})
    try:
        snap = c.providers.snapshot_for("agent")
        q = message.split(
            "\n---\n")[-1].strip() if "\n---\n" in message else message
        q = q[:500]
        title = q[:15]  # 立即设兜底（满足 3 秒退化为取前 15 字符）
        c.sessions.set_auto_title(sid, title)
        if snap:
            async def _call_llm():
                try:
                    resp = await c.llm.chat(snap, [
                        {"role": "system", "content": PROMPTS.load_raw(
                            "app/prompts/title_gen")},
                        {"role": "user", "content": q}], source="title_gen",
                        session_id=sid)
                    return resp["content"].strip()[:15]
                except Exception:  # noqa: BLE001
                    return None
            try:
                result = await asyncio.wait_for(asyncio.shield(
                    asyncio.create_task(_call_llm())), timeout=3)
            except asyncio.TimeoutError:
                span.end(output={"title": title,
                         "fallback": True, "timeout": True})
                return  # 已设兜底 title，LLM 任务继续后台完成
            if result:
                c.sessions.set_auto_title(sid, result)
                span.end(output={"title": result})
                return
        span.end(output={"title": title, "fallback": True})
    except Exception:  # noqa: BLE001
        span.end()


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


@router.post("/chat/feedback")
async def feedback(request: Request):
    body = await request.json()
    c = _c()
    mid = body["message_id"]
    fb = body["feedback"]
    reason = body.get("reason")
    from observability_langfuse import get_tracer
    tr = get_tracer().trace_start("user_feedback", input={
        "message_id": mid, "feedback": fb, "reason": reason,
        "session_id": c.db.query_one(
            "SELECT session_id FROM conversations WHERE id=?", (mid,))["session_id"]
        if c.db.query_one("SELECT 1 FROM conversations WHERE id=?", (mid,))
        else None})
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
            context_snippet = (row.get("content") or "")[:300] if row else ""
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
async def rename(request: Request):
    body = await request.json()
    _c().sessions.rename(body["session_id"], body["title"])
    return {"code": 200, "data": {}}


@router.post("/chat/session/create")
async def create():
    return {"code": 200, "data": {"session_id": _c().sessions.create_session()}}


@router.post("/chat/session/pin")
async def pin(request: Request):
    body = await request.json()
    _c().sessions.set_pinned(body["session_id"], bool(body.get("pinned")))
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
    from observability_langfuse import get_tracer
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


@router.get("/chat/session/{session_id}/usage")
async def session_usage(session_id: str):
    row = _c().db.query_one(
        "SELECT COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o "
        "FROM token_usage WHERE session_id=?", (session_id,))
    return {"code": 200, "data": {"input_tokens": row["i"], "output_tokens": row["o"]}}
