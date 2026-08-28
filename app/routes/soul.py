"""SOUL / 用户画像 / 输出样式接口（开发文档 §2.7-2.9）。"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class SoulCoreRequest(BaseModel):
    content: str


class SoulStyleRollbackRequest(BaseModel):
    source: str
    version: int


class PendingConfirmRequest(BaseModel):
    pending_id: str
    approved: bool = True


class OutputStyleToggleRequest(BaseModel):
    enabled: bool


class OutputStyleRequest(BaseModel):
    content: str = ""


class ProfileReviewActionRequest(BaseModel):
    id: int


def _c():
    from app.main import get_container
    return get_container()


# ---- 2.7 用户画像 ---------------------------------------------------------
@router.get("/profile")
async def profile():
    return {"code": 200, "data": _c().profile.parse()}


@router.post("/profile/build-now")
async def profile_build_now():
    """手动触发用户画像重建（绕过夜间维护链，需已有 active/stable 记忆）。"""
    ok = await _c().profile_builder.rebuild()
    return {"code": 200, "data": {"ok": ok}}


# ---- 2.8 SOUL 管理 --------------------------------------------------------
@router.get("/soul")
async def get_soul():
    c = _c()
    return {"code": 200, "data": {"soul_core": c.soul.read_core(),
                                  "soul_style": c.soul.read_style()}}


@router.put("/soul/core")
async def put_core(body: SoulCoreRequest):
    _c().soul.write_core(body.content)
    return {"code": 200, "data": {}}


@router.post("/soul/core/reset")
async def reset_core():
    """恢复默认人格（SOUL_CORE 基线），会覆盖用户自定义内容。"""
    from soul.constants import DEFAULT_SOUL_CORE
    c = _c()
    c.soul.write_core(DEFAULT_SOUL_CORE)
    c.oplog.log("soul_core_reset", "恢复默认人格")
    return {"code": 200, "data": {"content": DEFAULT_SOUL_CORE}}


@router.get("/soul/style/history")
async def style_history(source: str = "dialog"):
    return {"code": 200, "data": _c().soul.history(source)}


@router.get("/soul/style/diff")
async def style_diff(source: str, from_: int = 0, to: int = 0):
    return {"code": 200, "data": _c().soul.diff(source, from_, to)}


@router.post("/soul/style/rollback")
async def style_rollback(body: SoulStyleRollbackRequest):
    await _c().soul.rollback(body.source, body.version)
    return {"code": 200, "data": {}}


@router.get("/soul/pending")
async def get_pending():
    return {"code": 200, "data": _c().ctx_entry.list_pending()}


@router.post("/soul/pending/confirm")
async def confirm_pending(body: PendingConfirmRequest):
    c = _c()
    pid = body.pending_id
    approved = body.approved
    # 先读取待确认项（不立即移除）
    item = next((p for p in c.ctx_entry.list_pending()
                 if p.get("id") == pid), None)
    if not item:
        return {"code": 200, "data": {}}
    if not approved:
        # 忽略：直接移除
        c.ctx_entry.remove_pending(pid)
        return {"code": 200, "data": {}}
    # 确认三步（严格按序）：先落盘 dialog 序列，落盘成功后才移除 pending
    sections = c.soul.read_style()
    section = "行为原则" if item.get("type") == "behavior" else "对话风格"
    proposed = (item.get("proposed_change") or "").strip()
    # —— 去重：检查目标段落是否已有语义相同的规则（余弦≥θ）——
    existing_lines = [ln.strip().lstrip("- ") for ln in
                      sections.get(section, "").splitlines() if ln.strip().startswith("-")]
    if proposed and existing_lines:
        try:
            import numpy as np
            all_texts = existing_lines + [proposed]
            vecs = await c.embed_fn(all_texts)
            pvec = np.asarray(vecs[-1], dtype=float)
            threshold = c.config.get("soul_style_dedup_threshold", 0.85)
            for ev in vecs[:-1]:
                ev = np.asarray(ev, dtype=float)
                cos = float(pvec @ ev / (np.linalg.norm(pvec)
                            * np.linalg.norm(ev) + 1e-9))
                if cos >= threshold:
                    # 语义重复，跳过追加，移除 pending 并告知前端
                    c.ctx_entry.remove_pending(pid)
                    return {"code": 200, "data": {"deduplicated": True}}
        except Exception:  # noqa: BLE001 - embed 失败降级为不去重
            pass
    # —— 正常追加 ——
    sections[section] = (sections.get(section, "") + "\n- " + proposed).strip()
    content = f"## 对话风格\n{sections.get('对话风格', '')}\n## 行为原则\n{sections.get('行为原则', '')}"
    # 确认三步第二/三步（严格按序）：wait=True 等待真正落盘，
    # 落盘失败时 pending 保留不移除（下次会话再问一次）并告知前端
    try:
        await c.fw.submit("soul_style", {"section": "dialog", "content": content,
                                         "create_version": True,
                                         "diff_summary": proposed}, wait=True)
    except Exception as e:  # noqa: BLE001 - 含 WriteFailedError/QueueFullError
        return {"code": 500,
                "message": f"风格写入失败，本次确认未生效，下次会话将再次询问：{e}",
                "trace_id": None, "details": None}
    # 第三步：仅在落盘成功后才从 CONTEXT_ENTRY 移除 pending
    c.ctx_entry.remove_pending(pid)
    return {"code": 200, "data": {}}


# ---- 2.9 输出样式画像 -----------------------------------------------------
@router.get("/output-style")
async def output_style():
    c = _c()
    sections = c.soul.read_style()
    count = c.output_style_builder.signal_count()
    # 上次提炼时间：auto 序列最新版本文件的修改时间
    last_built = None
    try:
        from soul.soul_manager import _history_dir
        files = sorted((_history_dir(c.data_dir)).glob("auto_v*.md"),
                       key=lambda f: f.stat().st_mtime, reverse=True)
        if files:
            from infrastructure.timeutil import from_ts
            last_built = from_ts(
                files[0].stat().st_mtime).isoformat(timespec="seconds")
    except Exception:  # noqa: BLE001
        pass
    from memory import _constants as _mem_const
    batch = _mem_const.OUTPUT_STYLE_SIGNAL_BATCH_THRESHOLD
    return {"code": 200, "data": {
        "profile_text": sections.get("输出样式", ""),
        "auto_evolve_enabled": c.config.get("output_style_auto_evolve_enabled", True),
        "signal_count": count, "is_cold_start": count < 50,
        "last_built": last_built, "batch_threshold": batch}}


@router.post("/output-style/toggle-auto")
async def toggle_auto(body: OutputStyleToggleRequest):
    _c().config.update_params(
        {"output_style_auto_evolve_enabled": body.enabled})
    return {"code": 200, "data": {}}


@router.put("/output-style")
async def put_output_style(body: OutputStyleRequest):
    """用户手动编辑输出样式画像。走 FileWriter soul_style(auto) 落盘，
    带版本历史（可回滚）；wait=True 等待真正生效。"""
    content = body.content.strip()
    c = _c()
    try:
        await c.fw.submit("soul_style", {
            "section": "auto", "content": content,
            "create_version": True, "diff_summary": "用户手动编辑"}, wait=True)
    except Exception as e:  # noqa: BLE001 - 含 WriteFailedError/QueueFullError
        from infrastructure.observability import get_trace_id
        return {"code": 500, "message": f"画像写入失败：{e}",
                "trace_id": get_trace_id(), "details": None}
    if c.oplog:
        c.oplog.log("output_style_edit", "用户手动编辑输出样式画像")
    return {"code": 200, "data": {}}


@router.post("/output-style/build-now")
async def build_now():
    await _c().output_style_builder.build(force=True)
    return {"code": 200, "data": {}}


# ---- 画像审核队列（v3 §反馈闭环：策略偏好候选的消费端） ------------------

@router.get("/profile-review/pending")
async def profile_review_pending(review_type: str = ""):
    """待确认候选列表（可按轨道过滤）+ 各轨道计数。"""
    c = _c()
    sql = ("SELECT id,review_type,change_key,title,proposed_content,evidence,"
           "priority,created_at FROM profile_review_queue WHERE status='pending'")
    params: tuple = ()
    if review_type:
        sql += " AND review_type=?"
        params = (review_type,)
    sql += " ORDER BY priority, created_at"
    rows = c.db.query_all(sql, params)
    return {"code": 200, "data": {
        "list": [dict(r) for r in rows],
        "counts": c.conflict_scanner.pending_count()}}


@router.post("/profile-review/confirm")
async def profile_review_confirm(body: ProfileReviewActionRequest):
    """确认候选：strategy_preference 轨道写入 RESPONSE_STRATEGY.md 对应场景段。"""
    import json as _json
    c = _c()
    row = c.db.query_one(
        "SELECT * FROM profile_review_queue WHERE id=? AND status='pending'",
        (body.id,))
    if not row:
        from infrastructure.observability import get_trace_id
        return {"code": 404, "message": "候选不存在或已处理",
                "trace_id": get_trace_id(), "details": None}
    if row["review_type"] != "strategy_preference":
        from infrastructure.observability import get_trace_id
        return {"code": 400, "message": "该轨道暂不支持在线确认",
                "trace_id": get_trace_id(), "details": None}
    scene = "other"
    try:
        ev = _json.loads(row["evidence"] or "{}")
        scene = ev.get("scene") or "other"
    except ValueError:
        pass
    try:
        await c.fw.submit("response_strategy", {
            "scene": scene, "entry": row["proposed_content"]}, wait=True)
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger("second_person.soul").warning("策略偏好写入失败", exc_info=True)
        from infrastructure.observability import get_trace_id
        return {"code": 500, "message": "策略偏好写入失败",
                "trace_id": get_trace_id(), "details": None}
    from infrastructure.timeutil import now_cst
    c.db.execute(
        "UPDATE profile_review_queue SET status='confirmed', reviewed_at=?, "
        "reviewed_by='user' WHERE id=?",
        (now_cst().isoformat(timespec="seconds"), row["id"]))
    if c.oplog:
        c.oplog.log("strategy_preference_confirm", row["title"])
    return {"code": 200, "data": {}}


@router.post("/profile-review/reject")
async def profile_review_reject(body: ProfileReviewActionRequest):
    """拒绝候选：进入 60 天拒绝保护期，同方向不再重提。"""
    c = _c()
    row = c.db.query_one(
        "SELECT * FROM profile_review_queue WHERE id=? AND status='pending'",
        (body.id,))
    if not row:
        from infrastructure.observability import get_trace_id
        return {"code": 404, "message": "候选不存在或已处理",
                "trace_id": get_trace_id(), "details": None}
    c.conflict_scanner.reject_and_protect(
        row["review_type"], row["change_key"], row["proposed_content"][:200])
    from infrastructure.timeutil import now_cst
    c.db.execute(
        "UPDATE profile_review_queue SET status='rejected', reviewed_at=?, "
        "reviewed_by='user' WHERE id=?",
        (now_cst().isoformat(timespec="seconds"), row["id"]))
    return {"code": 200, "data": {}}


@router.get("/response-strategy")
async def get_response_strategy():
    """读 RESPONSE_STRATEGY.md 全文（不存在时为空，策略引擎用默认模板兜底）。"""
    from pathlib import Path
    c = _c()
    p = Path(c.sessions.data_dir) / "profile" / "RESPONSE_STRATEGY.md"
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    return {"code": 200, "data": {"content": text}}
