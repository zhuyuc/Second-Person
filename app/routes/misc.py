"""引导 / 文档导入 / 健康检查 / 一次性任务状态接口（开发文档 §三点五/§四/§五/§2.14）。"""
from __future__ import annotations

from fastapi import APIRouter, Request, UploadFile, File
from infrastructure.timeutil import now_cst

router = APIRouter()


def _c():
    from app.main import get_container
    return get_container()


# ---- 三点五 首次引导 ------------------------------------------------------
@router.get("/onboarding/status")
async def onboarding_status():
    c = _c()
    completed = c.config.get_raw("onboarding_completed", False)
    return {"code": 200, "data": {"completed": completed}}


@router.post("/onboarding/test-connection")
async def test_connection(request: Request):
    body = await request.json()
    c = _c()
    cfg = body.get("provider_config", {})
    from infrastructure.llm_provider import ProviderSnapshot
    snap = ProviderSnapshot("onboard", cfg.get("provider_type", "openai_compatible"),
                            cfg.get("base_url", ""), cfg.get("api_key", ""),
                            cfg.get("model_id", ""))
    try:
        # 同 settings 探测：连通性验证限 max_tokens，避免推理模型思考拖慢引导流程
        await c.llm.chat(snap, [{"role": "user", "content": "ping"}],
                         source="main_chat", max_tokens=10)
        return {"code": 200, "data": {"ok": True}}
    except Exception as e:  # noqa: BLE001
        return {"code": 200, "data": {"ok": False, "error": str(e)}}


@router.post("/onboarding/test-embedding")
async def test_embedding(request: Request):
    body = await request.json()
    c = _c()
    cfg = body.get("provider_config", {})
    from infrastructure.llm_provider import ProviderSnapshot
    snap = ProviderSnapshot("emb", "openai_compatible", cfg.get("base_url", ""),
                            cfg.get("api_key", ""), cfg.get("model_id", ""))
    try:
        await c.llm.embed(snap, ["ping"])
        return {"code": 200, "data": {"ok": True}}
    except Exception as e:  # noqa: BLE001
        return {"code": 200, "data": {"ok": False, "error": str(e)}}


@router.post("/onboarding/welcome-chat/start")
async def welcome_start():
    """开始欢迎对话：创建一个会话供引导期对话使用。"""
    c = _c()
    sid = c.sessions.create_session()
    return {"code": 200, "data": {"session_id": sid}}


@router.post("/onboarding/welcome-chat/finish")
async def welcome_finish(request: Request):
    """结束欢迎对话，画像 Agent 引导模式生成 SOUL 草稿。"""
    c = _c()
    # 限定欢迎会话（最新活跃）：避免把其他会话内容喂给画像 Agent 污染 SOUL 初稿
    sid = c.sessions.latest_active_session()
    convs = c.db.query_all(
        "SELECT role,content FROM conversations WHERE session_id=? "
        "ORDER BY id DESC LIMIT 10", (sid,)) if sid else []
    text = "\n".join(f"{r['role']}: {r['content']}" for r in reversed(convs))
    draft = await c.profile_builder.build_initial_soul(text)
    return {"code": 200, "data": draft or {"soul_core": "", "soul_style_dialog": ""}}


@router.post("/onboarding/soul/confirm")
async def soul_confirm(request: Request):
    body = await request.json()
    c = _c()
    if body.get("soul_core"):
        c.soul.write_core(body["soul_core"])
    if body.get("soul_style"):
        await c.fw.submit("soul_style", {"section": "dialog",
                                         "content": body["soul_style"],
                                         "create_version": True,
                                         "diff_summary": "引导初始化"})
    c.config.set_raw("onboarding_completed", True)
    from datetime import datetime
    if not c.config.get_raw("first_installed", None):
        c.config.set_raw("first_installed",
                         now_cst().strftime("%Y-%m-%d"))
    return {"code": 200, "data": {}}


# ---- 四 文档导入 ----------------------------------------------------------
@router.post("/import/document")
async def import_document(file: UploadFile = File(...)):
    c = _c()
    content = await file.read()
    try:
        result = await c.ingest.ingest_file(file.filename, content, source="web_ui")
    except ValueError as e:
        return {"code": 400, "message": str(e), "trace_id": None, "details": None}
    except Exception as e:  # noqa: BLE001
        # 单个文档解析/提炼失败（已回滚落盘）：返回带文件名的友好错误，
        # 不抛 500，以免前端批量导入时难以定位是哪个文件出错。
        return {"code": 400,
                "message": f"「{file.filename}」导入失败：{e}",
                "trace_id": None, "details": None}
    return {"code": 200, "data": result}


@router.post("/import/document/stream")
async def import_document_stream(file: UploadFile = File(...)):
    """流式导入：边解析/提炼边通过 SSE 推送实时进度，避免前端长时间无反馈。
    事件：progress（stage/current/total）、done（结果）、error（错误信息）。"""
    import asyncio
    import json

    from sse_starlette.sse import EventSourceResponse

    c = _c()
    content = await file.read()
    filename = file.filename
    queue: asyncio.Queue = asyncio.Queue()

    async def progress_cb(stage: str, data: dict) -> None:
        await queue.put(("progress", {"stage": stage, **data}))

    async def run() -> None:
        try:
            result = await c.ingest.ingest_file(
                filename, content, source="web_ui", progress_cb=progress_cb)
            await queue.put(("done", result))
        except ValueError as e:
            await queue.put(("error", {"message": str(e)}))
        except Exception as e:  # noqa: BLE001
            await queue.put(("error", {"message": f"「{filename}」导入失败：{e}"}))
        finally:
            await queue.put(None)

    async def gen():
        task = asyncio.create_task(run())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                event, data = item
                yield {"event": event,
                       "data": json.dumps(data, ensure_ascii=False)}
        finally:
            # 读者提前断开：取消后台导入任务，不残留悬空协程
            if not task.done():
                task.cancel()

    return EventSourceResponse(gen(), ping=5)


@router.post("/import/url")
async def import_url(request: Request):
    body = await request.json()
    c = _c()
    from tools.web_fetch import web_fetch
    result = await c.ingest.ingest_url(
        body["url"],
        lambda u: web_fetch(u, c.config.get("web_fetch_timeout_seconds", 15)))
    return {"code": 200, "data": result}


@router.get("/import/documents")
async def list_documents():
    return {"code": 200, "data": _c().ingest.list_documents()}


@router.post("/import/documents/{doc_id}/confirm")
async def confirm_import(doc_id: str, request: Request):
    """预览导入确认：silent_doc_import=false 时用户勾选后写入。"""
    body = await request.json()
    try:
        result = await _c().ingest.confirm_import(doc_id, body.get("selected", []))
    except KeyError:
        return {"code": 404, "message": "无待确认的导入记录", "trace_id": None,
                "details": None}
    return {"code": 200, "data": result}


@router.get("/import/documents/{doc_id}")
async def document_detail(doc_id: str):
    detail = _c().ingest.get_document_detail(doc_id)
    if detail is None:
        return {"code": 404, "message": "文档不存在", "trace_id": None, "details": None}
    return {"code": 200, "data": detail}


@router.delete("/import/documents/{doc_id}")
async def delete_document(doc_id: str, cascade: bool = False):
    """删除知识库文档。cascade=true 时连带删除该文档提炼的记忆（重要记忆保留），
    逐条复用单条记忆物理删除链（图谱边/md/向量/矛盾自愈同事务清理）。"""
    import json

    c = _c()
    deleted = kept = failed = 0
    if cascade:
        # 必须在删 raw_docs 行之前读取归属映射，否则溯源永久丢失
        row = c.db.query_one(
            "SELECT extracted_memory_ids FROM raw_docs WHERE id=?", (doc_id,))
        mem_ids = json.loads(row["extracted_memory_ids"]
                             or "[]") if row else []
        for mid in mem_ids:
            m = c.palace.get(mid)
            if m is None:
                continue  # 已手动删除/被合并演化，幂等跳过
            if m["is_important"]:
                kept += 1  # 用户显式标记的重要记忆不随文档级联删除
                continue
            # 逐条独立删除：单条失败不中断其余，汇总结果交前端提示
            try:
                await c.fw.submit(
                    "memory", {"op": "delete", "memory_id": mid}, wait=True)
                c.oplog.log("memory_delete", mid)
                deleted += 1
            except Exception:  # noqa: BLE001
                failed += 1
    c.ingest.delete_document(doc_id)
    return {"code": 200, "data": {
        "deleted_memories": deleted, "kept_important": kept, "failed": failed,
        "warning": "删除会影响 --recompile 重建完整性"}}


# ---- 本地目录全域接入（个人知识接入） -------------------------------------
@router.get("/import/local-dirs")
async def list_local_dirs():
    return {"code": 200, "data": _c().folder_scanner.list_dirs()}


@router.post("/import/local-dirs")
async def add_local_dir(request: Request):
    body = await request.json()
    c = _c()
    try:
        item = c.folder_scanner.add_dir(
            body.get("path", ""), bool(body.get("recursive", True)))
    except ValueError as e:
        return {"code": 400, "message": str(e), "trace_id": None,
                "details": None}
    c.oplog.log("local_dir_add", item["path"])
    return {"code": 200, "data": item}


@router.put("/import/local-dirs/{dir_id}")
async def update_local_dir(dir_id: int, request: Request):
    body = await request.json()
    c = _c()
    if "enabled" in body:
        c.folder_scanner.set_enabled(dir_id, bool(body["enabled"]))
    return {"code": 200, "data": {}}


@router.delete("/import/local-dirs/{dir_id}")
async def remove_local_dir(dir_id: int):
    """解除跟踪：仅停止后续扫描，已提炼记忆与 raw_docs 副本保留。"""
    c = _c()
    c.folder_scanner.remove_dir(dir_id)
    return {"code": 200, "data": {}}


@router.post("/import/local-dirs/scan")
async def scan_local_dirs():
    """手动触发立即扫描全部已启用目录（与调度扫描共用并发锁）。"""
    c = _c()
    result = await c.folder_scanner.scan_all(trigger="manual")
    return {"code": 200, "data": result}


@router.get("/import/local-dirs/{dir_id}/files")
async def list_local_dir_files(dir_id: int, status: str = ""):
    c = _c()
    sql = ("SELECT path,status,fail_reason,imported_at,doc_id "
           "FROM local_dir_files WHERE dir_id=? "
           + ("AND status=? " if status else "")
           + "ORDER BY last_seen_at DESC LIMIT 500")
    rows = c.db.query_all(sql, (dir_id, status) if status else (dir_id,))
    return {"code": 200, "data": [dict(r) for r in rows]}


# ---- 文档导出（Markdown / Word 下载） --------------------------------------
@router.get("/files/{stored_name}")
async def download_generated_file(stored_name: str):
    """generate_document 工具产物下载（temp/exports，夜间链 7 天清理）。"""
    from pathlib import Path
    from fastapi.responses import FileResponse
    # 防路径穿越：只接受纯文件名
    if Path(stored_name).name != stored_name or stored_name.startswith("."):
        return {"code": 404, "message": "文件不存在", "trace_id": None,
                "details": None}
    path = Path(_c().data_dir) / "temp" / "exports" / stored_name
    if not path.is_file():
        return {"code": 404, "message": "文件不存在或已过期清理，请重新生成",
                "trace_id": None, "details": None}
    # 对外文件名去掉 uuid 前缀，还原标题原名
    display = stored_name.split(
        "_", 1)[-1] if "_" in stored_name else stored_name
    media = ("application/vnd.openxmlformats-officedocument"
             ".wordprocessingml.document" if stored_name.endswith(".docx")
             else "text/markdown; charset=utf-8")
    return FileResponse(path, media_type=media, filename=display)


# ---- 2.14 一次性任务状态 --------------------------------------------------
@router.get("/tasks/{task_id}/status")
async def task_status(task_id: str):
    c = _c()
    row = c.db.query_one(
        "SELECT result,fail_reason FROM task_logs WHERE task_id LIKE ? "
        "ORDER BY run_time DESC LIMIT 1", (f"{task_id}%",))
    status = "completed" if row and row["result"] == "success" else \
             ("failed" if row and row["result"] == "failed" else "running")
    # 真实进度：Embedding 迁移读取 done_count/total_count
    progress = 50
    if task_id.startswith("embedding_migration"):
        mrow = c.db.query_one(
            "SELECT done_count, total_count FROM embedding_migration "
            "WHERE id=?", (task_id.split("_")[-1],))
        if mrow and mrow["total_count"]:
            progress = int(mrow["done_count"] / mrow["total_count"] * 100)
    return {"code": 200, "data": {"task_id": task_id, "status": status,
                                  "progress": progress}}


# ---- IM 平台入站 webhook（飞书/钉钉） ------------------------------------
@router.post("/im/webhook/{platform}")
async def im_webhook(platform: str, request: Request):
    c = _c()
    payload = await request.json()
    # 飞书 URL 验证挑战
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge")}
    adapter = getattr(c, "adapters", None)
    active = adapter.active if adapter else None
    if active and active.platform_type == platform and hasattr(active, "handle_webhook"):
        await active.handle_webhook(payload)
    return {"code": 200, "data": {}}


# ---- MCP 连接器 OAuth 2.1 回调 -------------------------------------------
@router.get("/connectors/oauth/callback")
async def oauth_callback(state: str = "", code: str = ""):
    c = _c()
    row = c.db.query_one("SELECT connector_id,created_at FROM oauth_states WHERE state=?",
                         (state,))
    if not row:
        return {"code": 400, "message": "state 无效或已过期", "trace_id": None, "details": None}
    # 校验 5 分钟有效期
    from datetime import datetime as _dt
    try:
        issued = _dt.fromisoformat(row["created_at"])
        if (_dt.now() - issued).total_seconds() > 300:
            c.db.execute("DELETE FROM oauth_states WHERE state=?", (state,))
            return {"code": 400, "message": "授权已过期，请重试", "trace_id": None, "details": None}
    except (TypeError, ValueError):
        pass
    # code 换 token 由具体连接器完成；此处标记已回调，加密存 credentials
    c.creds.store(f"oauth:{row['connector_id']}", "connector", code)
    c.db.execute("DELETE FROM oauth_states WHERE state=?", (state,))
    return {"code": 200, "data": {"connector_id": row["connector_id"], "authorized": True}}


# ---- 五 健康检查 ----------------------------------------------------------
def _fw_ok(c) -> bool:
    """FileWriter 队列健康：无 failed 写入积压。"""
    try:
        return c.db.query_one(
            "SELECT count(*) c FROM pending_writes WHERE status='failed'")["c"] == 0
    except Exception:  # noqa: BLE001
        return True


def _consistency_ok(c) -> bool:
    """md-SQLite 一致性：count 比对（active+stable+stale 口径）。"""
    try:
        from memory.recovery import consistency_check
        return consistency_check(c.db, c.data_dir)["consistent"]
    except Exception:  # noqa: BLE001
        return True


@router.get("/health")
async def health():
    c = _c()
    db_ok = c.db.integrity_check()
    # Provider / Embedding 可用性
    chat_snap = c.providers.snapshot_for("chat")
    if chat_snap is None:
        provider_state = "unconfigured"
    else:
        provider_state = "ok" if c.llm.status(
            chat_snap.model_id) != "unavailable" else "unavailable"
    emb_snap = c.providers.snapshot_for("embedding")
    embedding_state = "ok" if emb_snap is not None else "unconfigured"
    # FTS5 真实可用性：查询抛异常（索引损坏/表缺失）才判 error，空表属正常
    try:
        c.db.query_one("SELECT count(*) c FROM memories_fts")
        fts_state = "ok"
    except Exception:  # noqa: BLE001
        fts_state = "error"
    checks = {
        "database": "ok" if db_ok else "error",
        "vector_cache": "ok" if c.vs.loaded else "loading",
        "fts5": fts_state,
        "event_bus": "ok",  # 总线常驻内存组件，订阅者数为扩展信息非健康指标
        "scheduler": "ok" if getattr(getattr(c, "scheduler", None),
                                     "_running", True) else "degraded",
        "file_writer": "ok" if _fw_ok(c) else "degraded",
        "md_sqlite_consistency": "ok" if _consistency_ok(c) else "warning",
        "llm_provider": provider_state, "embedding": embedding_state,
    }
    # 三级判定：数据库坏/对话模型均不可用 → unhealthy；有降级项 → degraded
    if not db_ok or provider_state == "unavailable":
        status = "unhealthy"
    elif all(v in ("ok",) for v in checks.values()):
        status = "healthy"
    else:
        status = "degraded"
    return {"code": 200, "data": {"status": status, "checks": checks}}
