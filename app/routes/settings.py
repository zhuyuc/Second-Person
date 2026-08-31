"""系统设置接口（开发文档 §三）。"""
from __future__ import annotations
from infrastructure.timeutil import now_cst
from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from app.contracts import read_json_object

import asyncio
import logging
from datetime import timedelta

logger = logging.getLogger("second_person.settings")


router = APIRouter()


def _c():
    from app.main import get_container
    return get_container()


# ---- 3.1 Provider 管理 ----------------------------------------------------
@router.get("/settings/providers")
async def list_providers():
    c = _c()
    rows = c.providers.list_providers()
    for r in rows:
        r.pop("credential_id", None)
    return {"code": 200, "data": rows}


@router.post("/settings/providers")
async def add_provider(request: Request):
    c = _c()
    body = c.settings_svc.clean_provider_fields(await read_json_object(request))
    try:
        c.settings_svc.validate_provider_required(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"code": 200, "data": c.settings_svc.add_or_update_provider(body)}


@router.put("/settings/providers/{pid}")
async def edit_provider(pid: str, request: Request):
    body = _c().settings_svc.clean_provider_fields(await read_json_object(request))
    _c().providers.update_provider(pid, body, body.get("api_key"))
    return {"code": 200, "data": {}}


@router.delete("/settings/providers/{pid}")
async def delete_provider(pid: str):
    _c().providers.delete_provider(pid)
    return {"code": 200, "data": {}}


@router.post("/settings/providers/{pid}/test")
async def test_provider(pid: str):
    c = _c()
    snap = c.providers.snapshot(pid)
    if not snap:
        raise HTTPException(status_code=404, detail="Provider 不存在")
    return {"code": 200, "data": await c.settings_svc.probe_snapshot(snap)}


@router.get("/settings/providers/{pid}/key")
async def get_provider_key(pid: str):
    """本地单用户：返回解密后的 API Key 供编辑回显（前端默认脱敏显示，可点击显示）。"""
    c = _c()
    snap = c.providers.snapshot(pid)
    if not snap:
        raise HTTPException(status_code=404, detail="Provider 不存在")
    key = snap.api_key or ""
    return {"code": 200, "data": {
        "api_key": key, "api_key_masked": c.settings_svc.mask_credential(key)}}


@router.post("/settings/providers/test-connection")
async def test_connection_config(request: Request):
    """仅测试连接性（根据表单配置，不入库）。"""
    body = await read_json_object(request)
    # 空参数前置拦截，提示友好错误而非底层 httpx 异常
    if not (body.get("base_url") or "").strip():
        return {"code": 200, "data": {"ok": False, "error": "请先填写 Base URL"}}
    if not (body.get("model_id") or "").strip():
        return {"code": 200, "data": {"ok": False, "error": "请先填写模型 ID"}}
    r = await _c().settings_svc.test_provider(body)
    return {"code": 200, "data": r}


# ---- 3.2 任务-模型分配 ----------------------------------------------------
@router.get("/settings/task-slots")
async def get_task_slots():
    """槽位元数据清单（id/中文名/职责描述/回退链/当前配置），供设置页动态渲染。"""
    from infrastructure.provider_registry import TASK_SLOTS
    c = _c()
    # 先批量拉齐 slot → pid → provider 元信息，避免每 slot 一次 SELECT
    slot_pids: dict[str, str] = {}
    for slot in TASK_SLOTS.values():
        pid = c.providers.assignment(slot.key)
        if pid:
            slot_pids[slot.key] = pid
    provider_map: dict = {}
    pids = list(set(slot_pids.values()))
    if pids:
        placeholders = ",".join(["?"] * len(pids))
        rows = c.db.query_all(
            f"SELECT id, display_name, model_id, status FROM providers "
            f"WHERE id IN ({placeholders})", tuple(pids))
        provider_map = {r["id"]: r for r in rows}
    data = []
    for slot in TASK_SLOTS.values():
        pid = slot_pids.get(slot.key)
        model = None
        if pid:
            row = provider_map.get(pid)
            model = ({"provider_id": pid, "display_name": row["display_name"],
                      "model_id": row["model_id"], "status": row["status"]}
                     if row else {"provider_id": pid, "display_name": "",
                                  "model_id": "", "status": "unknown"})
        data.append({"key": slot.key, "label": slot.label, "desc": slot.desc,
                     "fallback": list(slot.fallback),
                     "lightweight": slot.lightweight, "model": model})
    return {"code": 200, "data": data}


@router.get("/settings/model-assignment")
async def get_assignment():
    from infrastructure.provider_registry import TASK_SLOTS
    c = _c()
    # 同 task-slots：批量拉 provider 元数据，避免逐 slot 查询
    slot_pids: dict[str, str] = {}
    for slot in TASK_SLOTS.values():
        pid = c.providers.assignment(slot.key)
        if pid:
            slot_pids[slot.key] = pid
    provider_map: dict = {}
    pids = list(set(slot_pids.values()))
    if pids:
        placeholders = ",".join(["?"] * len(pids))
        rows = c.db.query_all(
            f"SELECT id,display_name,status FROM providers "
            f"WHERE id IN ({placeholders})", tuple(pids))
        provider_map = {r["id"]: r for r in rows}
    out = {}
    for slot in TASK_SLOTS.values():
        pid = slot_pids.get(slot.key)
        if pid:
            row = provider_map.get(pid)
            out[f"{slot.key}_model"] = {"provider_id": pid,
                                        "display_name": row["display_name"] if row else "",
                                        "status": row["status"] if row else "unknown"}
        else:
            out[f"{slot.key}_model"] = None
    return {"code": 200, "data": out}


@router.put("/settings/model-assignment")
async def set_assignment(request: Request):
    from infrastructure.provider_registry import TASK_SLOTS
    body = await read_json_object(request)
    c = _c()
    for slot in TASK_SLOTS.values():
        key = f"{slot.key}_model"
        if body.get(key):
            c.providers.set_assignment(slot.key, body[key])
    return {"code": 200, "data": {}}


# ---- 3.3 Embedding 迁移 ---------------------------------------------------
@router.post("/settings/embedding/estimate")
async def embedding_estimate(request: Request):
    c = _c()
    count = c.db.query_one("SELECT count(*) c FROM vectors")["c"]
    return {"code": 200, "data": {"vector_count": count,
                                  "estimated_cost": round(count * 0.0002, 2),
                                  "estimated_minutes": max(1, count // 300),
                                  "old_vectors_retention_days": 30}}


@router.post("/settings/embedding/migrate")
async def embedding_migrate(request: Request):
    body = await read_json_object(request)
    if not body.get("confirm"):
        raise HTTPException(status_code=400, detail="需二次确认")
    c = _c()
    running = c.db.query_one(
        "SELECT id FROM embedding_migration WHERE status='running'")
    if running:
        raise HTTPException(status_code=409, detail="迁移已在进行")
    cur = c.db.execute(
        "INSERT INTO embedding_migration(from_model,to_model,total_count,done_count,"
        "status,started_at) VALUES('old',?,0,0,'running',?)",
        (body["target_provider_id"], now_cst().isoformat(timespec="seconds")))
    mid = cur.lastrowid
    total = c.db.query_one("SELECT count(*) c FROM vectors")["c"]
    c.db.execute(
        "UPDATE embedding_migration SET total_count=? WHERE id=?", (total, mid))
    # 启动双缓冲后台迁移：旧向量供检索，新向量写 staging，完成后原子切换
    c.migration_runner.start(mid, body["target_provider_id"])
    c.oplog.log("embedding_migrate",
                f"target={body['target_provider_id']} total={total}")
    return {"code": 200, "data": {"migration_id": mid}}


# ---- 3.4 参数 -------------------------------------------------------------
@router.get("/settings/params")
async def get_params():
    c = _c()
    return {"code": 200, "data": {"params": c.config.all_params(), "schema": c.config.schema()}}


@router.put("/settings/params")
async def put_params(request: Request):
    body = await read_json_object(request)
    c = _c()
    c.config.update_params(body)
    c.oplog.log("param_update", ",".join(body.keys()))
    return {"code": 200, "data": {}}


@router.post("/settings/params/reset")
async def reset_params():
    return {"code": 200, "data": _c().config.reset_defaults()}


# ---- 3.5 连接器 -----------------------------------------------------------
@router.get("/settings/connectors")
async def list_connectors():
    return {"code": 200, "data": _c().connectors.list_connectors()}


@router.post("/settings/connectors")
async def add_connector(request: Request):
    body = await read_json_object(request)
    c = _c()
    cid = await c.connectors.add(body["name"], body["transport"], body["config"],
                                 body.get("timeout", 120), body.get("tools_filter"))
    c.oplog.log("connector_add", cid)
    return {"code": 200, "data": {"id": cid}}


@router.put("/settings/connectors/{cid}")
async def edit_connector(cid: str, request: Request):
    body = await read_json_object(request)
    c = _c()
    await c.connectors.update(cid, body["name"], body["transport"], body["config"],
                              body.get("timeout", 120), body.get("tools_filter"))
    c.oplog.log("connector_update", cid)
    return {"code": 200, "data": {}}


@router.delete("/settings/connectors/{cid}")
async def delete_connector(cid: str):
    c = _c()
    await c.connectors.delete(cid)
    c.oplog.log("connector_delete", cid)
    return {"code": 200, "data": {}}


@router.post("/settings/connectors/{cid}/toggle")
async def toggle_connector(cid: str, request: Request):
    body = await read_json_object(request)
    await _c().connectors.toggle(cid, bool(body.get("enabled")))
    return {"code": 200, "data": {}}


@router.post("/settings/connectors/{cid}/refresh-tools")
async def refresh_tools(cid: str):
    tools = await _c().connectors.refresh_tools(cid)
    return {"code": 200, "data": {"tools": tools}}


@router.post("/settings/connectors/test")
async def test_connector(request: Request):
    """仅测试连接器连通性，不入库。"""
    body = await read_json_object(request)
    cfg = {
        "name": body.get("name", "test"),
        "transport": body["transport"],
        "config": body["config"],
        "timeout": body.get("timeout", 15),
    }
    # 空参数前置拦截，避免把配置错误报成晦涩的底层异常
    if cfg["transport"] == "stdio":
        if not (cfg["config"] or {}).get("command"):
            return {"code": 200, "data": {"ok": False, "error": "请先填写启动命令"}}
    elif not (cfg["config"] or {}).get("url"):
        return {"code": 200, "data": {"ok": False, "error": "请先填写端点地址"}}
    try:
        # 临时 MCPClient 测试连接
        from connectors.mcp_client import MCPClient
        client = MCPClient(cfg["transport"], cfg["config"], cfg["timeout"])
        await client.connect()
        tools = await client.list_tools()
        await client.disconnect()
        return {"code": 200, "data": {"ok": True, "tool_count": len(tools)}}
    except Exception as e:  # noqa: BLE001
        return {"code": 200, "data": {"ok": False, "error": str(e)}}


# ---- 3.6 用量 -------------------------------------------------------------
def _usage_where(source: str, model: str) -> tuple[str, list]:
    """用量筛选追加条件（source/model 可选，空串表示不过滤）。"""
    sql, args = "", []
    if source:
        sql += " AND source=?"
        args.append(source)
    if model:
        sql += " AND model_name=?"
        args.append(model)
    return sql, args


@router.get("/settings/usage/summary")
async def usage_summary(source: str = "", model: str = ""):
    c = _c()
    now = now_cst()
    today = now.strftime("%Y-%m-%d")
    month = now.strftime("%Y-%m")
    extra, eargs = _usage_where(source, model)
    today_used = c.db.query_one(
        "SELECT COALESCE(SUM(input_tokens+output_tokens),0) t FROM token_usage "
        "WHERE create_time LIKE ?" + extra, (f"{today}%", *eargs))["t"]
    month_used = c.db.query_one(
        "SELECT COALESCE(SUM(input_tokens+output_tokens),0) t FROM token_usage "
        "WHERE create_time LIKE ?" + extra, (f"{month}%", *eargs))["t"]
    daily_budget = c.config.get("daily_token_budget", 500000)
    monthly_budget = c.config.get("monthly_token_budget", 10000000)
    alert_ratio = c.config.get("budget_alert_ratio", 80)
    today_ratio = round(today_used / daily_budget *
                        100, 1) if daily_budget else 0
    month_ratio = round(month_used / monthly_budget *
                        100, 1) if monthly_budget else 0
    return {"code": 200, "data": {
        "today_used": today_used, "today_budget": daily_budget, "today_ratio": today_ratio,
        "month_used": month_used, "month_budget": monthly_budget, "month_ratio": month_ratio,
        "alert_ratio": alert_ratio,
        "over_budget_strategy": __import__("memory._constants", fromlist=["OVER_BUDGET_STRATEGY"]).OVER_BUDGET_STRATEGY,
        "is_alert": today_ratio >= alert_ratio or month_ratio >= alert_ratio}}


@router.get("/settings/usage/distribution")
async def usage_distribution(source: str = "", model: str = ""):
    c = _c()
    extra, eargs = _usage_where(source, model)
    src = c.db.query_all(
        "SELECT source, SUM(input_tokens+output_tokens) t FROM token_usage "
        "WHERE 1=1" + extra + " GROUP BY source", eargs)
    mdl = c.db.query_all(
        "SELECT model_name, SUM(input_tokens+output_tokens) t FROM token_usage "
        "WHERE 1=1" + extra + " GROUP BY model_name", eargs)
    by_model = [{"name": r["model_name"], "tokens": r["t"]} for r in mdl]
    # 无筛选态下，补齐"已配置但暂无消耗"的模型（tokens=0），
    # 让筛选下拉和列表能提前看到新加入的 provider，无需等第一次调用；
    # embedding 槽位的 provider 不产生 token_usage 记录，排除掉避免混淆
    if not source and not model:
        seen = {m["name"] for m in by_model}
        for r in c.db.query_all(
                "SELECT DISTINCT model_id FROM providers "
                "WHERE id NOT IN (SELECT provider_id FROM model_assignment "
                "WHERE task_type='embedding')"):
            mid = r["model_id"]
            if mid and mid not in seen:
                by_model.append({"name": mid, "tokens": 0})
    return {"code": 200, "data": {
        "by_source": [{"name": r["source"], "tokens": r["t"]} for r in src],
        "by_model": by_model}}


@router.get("/settings/usage/trend")
async def usage_trend(period: str = "30d", source: str = "", model: str = ""):
    """用量趋势：30d=近30天按天 / month=本月按天 / year=当年按月。
    每个时间桶按模型分组返回 models 明细（供前端堆叠柱状 + 多模型 tooltip），
    同时保留 tokens 总量字段兼容旧调用方。"""
    c = _c()
    now = now_cst()
    extra, eargs = _usage_where(source, model)

    def bucket(prefix: str, label: str) -> dict:
        rows = c.db.query_all(
            "SELECT model_name, SUM(input_tokens+output_tokens) t FROM token_usage "
            "WHERE create_time LIKE ?" + extra + " GROUP BY model_name",
            (f"{prefix}%", *eargs))
        models = [{"name": r["model_name"] or "未知", "tokens": r["t"] or 0}
                  for r in rows if (r["t"] or 0) > 0]
        models.sort(key=lambda m: m["tokens"], reverse=True)
        return {"label": label, "tokens": sum(m["tokens"] for m in models),
                "models": models}

    out = []
    if period == "year":
        for m in range(1, 13):
            out.append(bucket(f"{now.year}-{m:02d}", f"{m}月"))
    elif period == "month":
        import calendar
        days_in_month = calendar.monthrange(now.year, now.month)[1]
        for d in range(1, days_in_month + 1):
            out.append(bucket(f"{now.year}-{now.month:02d}-{d:02d}", f"{d}日"))
    else:  # 30d：当前往前 30 天
        for i in range(30):
            day = (now - timedelta(days=29 - i)).strftime("%Y-%m-%d")
            out.append(bucket(day, day[5:]))
    return {"code": 200, "data": out}


@router.get("/settings/usage/month-cost")
async def usage_month_cost():
    """本月费用：优先累加用量落库时冻结的金额（调价不追溯）；
    无快照的历史行按当前单价兜底折算，未配单价不计入，不做外推。"""
    c = _c()
    since = now_cst().strftime("%Y-%m-01")
    rows = c.db.query_all(
        "SELECT model_name, "
        "SUM(CASE WHEN cost IS NOT NULL THEN cost ELSE 0 END) frozen, "
        "SUM(CASE WHEN cost IS NULL THEN input_tokens ELSE 0 END) i, "
        "SUM(CASE WHEN cost IS NULL THEN output_tokens ELSE 0 END) o "
        "FROM token_usage WHERE create_time >= ? GROUP BY model_name",
        (f"{since}",))
    prices = {p["model_id"]: p for p in c.providers.list_providers()}
    total = 0.0
    detail = []
    for r in rows:
        cost = r["frozen"] or 0.0
        # 无金额快照的历史行（迁移前数据）：按当前单价兜底折算
        if (r["i"] or 0) or (r["o"] or 0):
            p = prices.get(r["model_name"])
            if not p or (not p.get("input_price") and not p.get("output_price")):
                if not cost:
                    continue   # 无快照且未配单价，不计入
            else:
                cost += (r["i"] or 0) / 1_000_000 * (p.get("input_price") or 0) + \
                    (r["o"] or 0) / 1_000_000 * (p.get("output_price") or 0)
        total += cost
        detail.append({"model": r["model_name"],
                      "month_cost": round(cost, 2)})
    return {"code": 200, "data": {"currency": "CNY",
                                  "month_cost": round(total, 2),
                                  "by_model": detail}}


# ---- 3.7 备份 -------------------------------------------------------------
@router.get("/settings/backups")
async def list_backups():
    # 逐个打开历史 zip 读 manifest，属同步磁盘 IO，丢工作线程
    data = await asyncio.to_thread(_c().backup.list_backups)
    return {"code": 200, "data": data}


@router.post("/settings/backups/create")
async def create_backup(request: Request):
    body = await read_json_object(request)
    c = _c()
    data = await c.backup.create(body.get("label"))
    c.oplog.log("backup_create", data.get("filename", ""))
    return {"code": 200, "data": data}


@router.post("/settings/backups/restore")
async def restore_backup(request: Request):
    body = await read_json_object(request)
    c = _c()
    await c.settings_svc.restore_backup(body["backup_id"])
    await asyncio.to_thread(c.vs.load)
    return {"code": 200, "data": {}}


@router.post("/settings/backups/export")
async def export_data():
    c = _c()
    from pathlib import Path
    from memory.naming import backup_filename
    target = Path(c.data_dir) / "backups" / ("export_" + backup_filename())
    # 全表导出 + zip 压缩为同步重操作，丢工作线程
    path = await asyncio.to_thread(c.backup.export_data, str(target))
    c.oplog.log("data_export", path)
    return {"code": 200, "data": {"path": path}}


@router.post("/settings/backups/import")
async def import_data(file: UploadFile = File(...)):
    c = _c()
    content = await file.read()
    try:
        await c.settings_svc.import_backup_bytes(content, file.filename or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"code": 200, "data": {}}


# ---- 3.8 系统状态 ---------------------------------------------------------
def _read_product_version() -> str:
    """从 pyproject.toml 读产品版本（单一事实源，不再硬编码）。"""
    import re
    from pathlib import Path
    try:
        text = (Path(__file__).resolve().parents[2] /
                "pyproject.toml").read_text(encoding="utf-8")
        m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
        return m.group(1) if m else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def _count_memory_md(data_dir) -> int:
    """磁盘 md 文件计数（与 recovery 重建同口径：排除 _index.md 与 _conflicts）。"""
    from pathlib import Path
    mem_dir = Path(data_dir) / "memories"
    if not mem_dir.exists():
        return 0
    return sum(1 for p in mem_dir.rglob("*.md")
               if p.name != "_index.md" and "_conflicts" not in p.parts)


@router.get("/settings/status")
async def status():
    c = _c()
    stats = c.palace.stats()
    sess = c.db.query_one("SELECT count(*) c FROM sessions")["c"]
    # 真实探测：PRAGMA 完整性检查与磁盘 md 计数均为同步重操作，丢工作线程
    db_ok = await asyncio.to_thread(c.db.integrity_check)
    md_count = await asyncio.to_thread(_count_memory_md, c.data_dir)
    # FTS5：探测查询（表缺失/损坏会直接抛异常）
    try:
        fts_count = c.db.query_one("SELECT count(*) c FROM memories_fts")["c"]
        fts_ok = True
    except Exception:  # noqa: BLE001
        fts_count, fts_ok = 0, False
    # 事件总线/FileWriter/调度器：读真实运行态
    bus_subs = c.bus.subscriber_count()
    fw_running = getattr(c.fw, "_running", False)
    fw_depth = c.fw._queue.qsize()
    sched_running = getattr(c.scheduler, "_running", False)
    # md-SQLite 一致性：索引计数与磁盘 md 文件数对比（lifecycle 全量口径）
    idx_count = c.db.query_one("SELECT count(*) c FROM memories")["c"]
    consistent = idx_count == md_count
    # 最近一次已应用的迁移脚本作为真实 schema 版本
    mig = c.db.query_one(
        "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1")
    subsystems = [
        {"name": "数据库（SQLite）", "status": "healthy" if db_ok else "unhealthy",
         "detail": "WAL 模式", "metric": ""},
        {"name": "向量缓存", "status": "healthy" if c.vs.loaded else "degraded",
         "detail": "numpy 内存", "metric": f"{c.vs.memory_mb():.1f}MB"},
        {"name": "FTS5 全文搜索", "status": "healthy" if fts_ok else "unhealthy",
         "detail": f"索引 {fts_count} 条" if fts_ok else "探测查询失败", "metric": ""},
        {"name": "事件总线", "status": "healthy" if bus_subs > 0 else "degraded",
         "detail": f"{bus_subs} 订阅者", "metric": ""},
        {"name": "FileWriter 队列", "status": "healthy" if fw_running else "unhealthy",
         "detail": "统一单写者", "metric": f"积压 {fw_depth} 条"},
        {"name": "调度器", "status": "healthy" if sched_running else "unhealthy",
         "detail": f"{len(c.scheduler.list_tasks())} 个任务", "metric": ""},
        {"name": "md-SQLite 一致性", "status": "healthy" if consistent else "degraded",
         "detail": f"索引 {idx_count} / 文件 {md_count}", "metric": ""},
    ]
    sts = {s["status"] for s in subsystems}
    overall = ("unhealthy" if "unhealthy" in sts
               else "degraded" if "degraded" in sts else "healthy")
    return {"code": 200, "data": {
        "overall": overall, "first_installed": c.config.get_raw("first_installed", ""),
        "subsystems": subsystems,
        "system_info": {"product_version": _read_product_version(),
                        "schema_version": mig["version"] if mig else "无",
                        "memory_count": stats["total"], "session_count": sess}}}


# ---- 3.9 接入渠道 ---------------------------------------------------------
@router.get("/settings/platforms")
async def list_platforms():
    rows = _c().db.query_all("SELECT * FROM platforms")
    return {"code": 200, "data": [
        {"id": r["id"], "platform_type": r["platform_type"], "status": r["status"],
         "enabled": bool(r["enabled"]), "bound": bool(r["credential_id"]),
         "detail": "监听 localhost:8000"
         if r["id"] == "web_default" else ("ClawBot 扫码绑定接入"
         if r["platform_type"] == "weixin" else ""),
         "last_failure": r["last_failure_time"],
         "failure_reason": r["last_failure_reason"]} for r in rows]}


@router.post("/settings/platforms")
async def add_platform(request: Request):
    body = await read_json_object(request)
    try:
        pid = _c().settings_svc.add_platform(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"code": 200, "data": {"id": pid}}


@router.get("/settings/platforms/{pid}/detail")
async def platform_detail(pid: str):
    """返回渠道完整配置供编辑回显（含解密后的凭证）。"""
    try:
        data = _c().settings_svc.get_platform_detail(pid)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"code": 200, "data": data}


@router.put("/settings/platforms/{pid}")
async def edit_platform(pid: str, request: Request):
    body = await read_json_object(request)
    try:
        await _c().settings_svc.edit_platform(pid, body)
    except ValueError as exc:
        status = 404 if "不存在" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return {"code": 200, "data": {}}


@router.post("/settings/platforms/test-push")
async def test_push():
    """发送一条测试系统通知到当前 IM 渠道，验证主动推送通道是否畅通。"""
    try:
        data = await _c().settings_svc.test_push()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"推送失败：{e}") from e
    return {"code": 200, "data": data}


@router.post("/settings/platforms/weixin/qrcode")
async def weixin_qrcode():
    """获取微信 ClawBot 登录二维码（iLink 直连）。"""
    try:
        data = await _c().settings_svc.request_weixin_qrcode()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"获取二维码失败：{e}") from e
    return {"code": 200, "data": data}


@router.get("/settings/platforms/weixin/qrcode/status")
async def weixin_qrcode_status(qrcode: str = ""):
    """轮询扫码状态；confirmed 后把 bot_token/baseurl 写入凭证（加密存储）。"""
    try:
        data = await _c().settings_svc.poll_weixin_qrcode_status(qrcode)
    except ValueError as exc:
        status = 404 if "不存在" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"查询扫码状态失败：{e}") from e
    return {"code": 200, "data": data}


@router.delete("/settings/platforms/{pid}")
async def delete_platform(pid: str):
    if pid == "web_default":
        raise HTTPException(status_code=400, detail="Web 默认渠道不可删除")
    _c().db.execute("DELETE FROM platforms WHERE id=?", (pid,))
    return {"code": 200, "data": {}}


@router.post("/settings/platforms/{pid}/enable")
async def enable_platform(pid: str):
    try:
        disabled = await _c().settings_svc.enable_platform(pid)
    except ValueError as exc:
        status = 404 if "不存在" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return {"code": 200, "data": {"disabled": disabled}}


@router.post("/settings/platforms/{pid}/disable")
async def disable_platform(pid: str):
    if pid == "web_default":
        raise HTTPException(status_code=400, detail="Web 默认渠道不可禁用")
    c = _c()
    c.db.execute("UPDATE platforms SET enabled=0 WHERE id=?", (pid,))
    if hasattr(c, "adapters"):
        await c.adapters.reload()
    return {"code": 200, "data": {}}


@router.post("/settings/platforms/test")
async def test_platform(request: Request):
    """仅测试 IM 平台连通性，不入库不启用。"""
    body = await read_json_object(request)
    ptype = body.get("platform_type", "")
    cfg = {"bot_token": body.get("bot_token"), "app_secret": body.get("app_secret"),
           "whitelist_user_id": body.get("whitelist_user_id", ""),
           "callback_url": body.get("callback_url")}
    data = await _c().settings_svc.test_platform_connectivity(ptype, cfg)
    return {"code": 200, "data": data}


@router.post("/settings/platforms/{pid}/resume")
async def resume_platform(pid: str):
    c = _c()
    c.db.execute(
        "UPDATE platforms SET status='healthy', failure_count=0 WHERE id=?", (pid,))
    if hasattr(c, "adapters"):
        await c.adapters.reload()
    return {"code": 200, "data": {}}


# ---- 3.10 定时任务 --------------------------------------------------------
@router.get("/settings/tasks")
async def list_tasks():
    c = _c()
    tasks = c.scheduler.list_tasks()
    # 调度/描述文本根据当前参数动态生成，与参数页保持一致
    review = c.config.get("passive_review_interval_days", 3)
    os_days = c.config.get("output_style_review_interval_days", 7)
    from memory import _constants as _mem_const
    os_batch = _mem_const.OUTPUT_STYLE_SIGNAL_BATCH_THRESHOLD
    sig_keep = c.config.get("output_style_signal_retention_days", 90)
    backup_keep = c.config.get("backup_retention_count", 3)
    dynamic = {
        # 夜间维护链（每天 02:00 链首，后续由前驱完成事件驱动）
        "auto_backup": f"夜间维护链链首 · 每天 02:00 · 保留最近 {backup_keep} 份",
        "dedup_cleanup": "夜间维护链 · 备份完成后触发 · 清理 24h 前去重记录",
        "temp_cleanup": "夜间维护链 · 清理 7 天前聊天渠道收到的临时文件缓存（图片已自动保存的不受影响）",
        "log_cleanup": f"夜间维护链 · 任务日志保 1 个月/操作日志 90 天/signal {sig_keep} 天",
        "conflict_cleanup": "夜间维护链 · 清理 30 天前已解决矛盾",
        "failed_rescan": "夜间维护链 · 重扫 failed 写入并重试",
        # 记忆维护链
        "passive_review": f"记忆维护链链首 · 每 {review} 天 03:00",
        "lint_check": "记忆维护链第 2 环 · 回顾完成触发（含技能提炼与归档）",
        "profile_rebuild": "记忆维护链第 3 环 · Lint 完成触发",
        # 独立任务
        "output_style_build": f"独立任务 · 每 {os_days} 天 或 满 {os_batch} 条 signal",
    }
    for t in tasks:
        if t["task_id"] in dynamic:
            t["schedule"] = dynamic[t["task_id"]]
    return {"code": 200, "data": tasks}


@router.post("/settings/tasks/{task_id}/run")
async def run_task(task_id: str):
    await _c().scheduler.run_task(task_id, trigger_source="manual")
    return {"code": 200, "data": {}}


@router.get("/settings/tasks/{task_id}/logs")
async def task_logs(task_id: str):
    return {"code": 200, "data": _c().scheduler.task_logs(task_id)}
