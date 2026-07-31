"""SOUL / 用户画像 / 输出样式接口（开发文档 §2.7-2.9）。"""
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


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
async def put_core(request: Request):
    body = await request.json()
    _c().soul.write_core(body["content"])
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
async def style_rollback(request: Request):
    body = await request.json()
    await _c().soul.rollback(body["source"], body["version"])
    return {"code": 200, "data": {}}


@router.get("/soul/pending")
async def get_pending():
    return {"code": 200, "data": _c().ctx_entry.list_pending()}


@router.post("/soul/pending/confirm")
async def confirm_pending(request: Request):
    body = await request.json()
    c = _c()
    pid = body["pending_id"]
    approved = body.get("approved", True)
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
            from datetime import datetime as _dt
            last_built = _dt.fromtimestamp(
                files[0].stat().st_mtime).isoformat(timespec="seconds")
    except Exception:  # noqa: BLE001
        pass
    batch = c.config.get("output_style_signal_batch_threshold", 100)
    return {"code": 200, "data": {
        "profile_text": sections.get("输出样式", ""),
        "auto_evolve_enabled": c.config.get("output_style_auto_evolve_enabled", True),
        "signal_count": count, "is_cold_start": count < 50,
        "last_built": last_built, "batch_threshold": batch}}


@router.post("/output-style/toggle-auto")
async def toggle_auto(request: Request):
    body = await request.json()
    _c().config.update_params(
        {"output_style_auto_evolve_enabled": bool(body.get("enabled"))})
    return {"code": 200, "data": {}}


@router.post("/output-style/build-now")
async def build_now():
    await _c().output_style_builder.build(force=True)
    return {"code": 200, "data": {}}
