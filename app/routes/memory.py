"""记忆中心接口（开发文档 §二）。"""
from __future__ import annotations

import asyncio
import logging
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from app.contracts import read_json_object

from infrastructure.timeutil import now_cst, now_iso
from memory.md_file import parse_memory_md

router = APIRouter()
logger = logging.getLogger("second_person.routes.memory")


def _c():
    from app.main import get_container
    return get_container()


@router.post("/memory/list")
async def memory_list(request: Request):
    body = await read_json_object(request)
    c = _c()
    keyword = body.get("keyword")
    domain = body.get("domain")
    lifecycle = body.get("lifecycle", "active,stable,stale")
    confidence = body.get("confidence")
    important_only = body.get("important_only")
    page = body.get("page", 1)
    page_size = body.get("page_size", 20)

    life_list = [x.strip() for x in lifecycle.split(",") if x.strip()]
    ph = ",".join("?" * len(life_list))
    where = [f"lifecycle IN ({ph})"]
    params: list = list(life_list)
    if domain:
        where.append("domain=?")
        params.append(domain)
    if confidence:
        where.append("confidence=?")
        params.append(confidence)
    if important_only:
        where.append("is_important=1")
    # M2 §7.4：project_id 过滤参数
    #   缺省或 "any"          → 所有（保留现有行为）
    #   "global" 或 null      → 仅全局（project_id IS NULL）
    #   具体 proj_xxx         → 该项目 + 全局（用户视图更实用）
    #   具体 proj_xxx + only  → 仅该项目
    project_filter = body.get("project_id")
    project_scope = body.get("project_scope", "with_global")  # with_global / only
    if project_filter in (None, "", "any"):
        pass
    elif project_filter in ("global", "null"):
        where.append("project_id IS NULL")
    else:
        if project_scope == "only":
            where.append("project_id=?")
            params.append(project_filter)
        else:
            where.append("(project_id=? OR project_id IS NULL)")
            params.append(project_filter)
    clause = " AND ".join(where)

    start = (page - 1) * page_size
    if keyword:
        query_vec = None
        if _has_embed(c):
            try:
                query_vec = (await c.embed_fn([keyword]))[0]
            except Exception:  # noqa: BLE001
                pass  # embedding 调用失败，降级 FTS5 单路
        pre_result = await c.retriever.hybrid_presearch(keyword, query_vec)
        ids = [x.memory_id for x in pre_result.candidates]
        if not ids:
            rows = []
        else:
            idph = ",".join("?" * len(ids))
            rows = c.db.query_all(
                f"SELECT * FROM memories WHERE id IN ({idph}) AND {clause}",
                (*ids, *params))
            order = {mid: i for i, mid in enumerate(ids)}
            rows = sorted(rows, key=lambda r: order.get(r["id"], 999))
        # keyword 路径：total 是过滤后的命中数，而非全局总数
        total = len(rows)
        page_rows = rows[start:start + page_size]
    else:
        # SQL 级分页：避免记忆库增长后全表拉取在事件循环上线性变慢
        page_rows = c.db.query_all(
            f"SELECT * FROM memories WHERE {clause} "
            f"ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (*params, page_size, start))
        # 非 keyword 路径：total 用带相同 WHERE 的 COUNT(*)，分页计数才正确
        total = c.db.query_one(
            f"SELECT COUNT(*) cnt FROM memories WHERE {clause}", params)["cnt"]
    stats = c.palace.stats()
    score, _ = c.lint.health_score()
    return {"code": 200, "data": {
        "total": total,
        "stats": {"total_active": stats["total_active"],
                  "total_stable": stats["total_stable"],
                  "total_stale": stats["total_stale"],
                  "total_archived": stats["total_archived"],
                  "important_count": stats["important_count"],
                  "link_count": stats["link_count"], "health_score": score},
        "list": [{"id": r["id"], "title": r["title"], "summary": r["summary"],
                  "domain": r["domain"], "confidence": r["confidence"],
                  "lifecycle": r["lifecycle"], "is_important": bool(r["is_important"]),
                  "access_count": r["access_count"], "last_accessed": r["last_accessed"],
                  "verification_state": r.get("verification_state", "unverified"),
                  "freshness_state": r.get("freshness_state", "current"),
                  "usefulness_score": r.get("usefulness_score", 0),
                  "file_path": r["md_path"]} for r in page_rows]}}


def _has_embed(c) -> bool:
    return c.providers.snapshot_for("embedding") is not None


@router.get("/memory/candidates")
async def memory_candidates(status: str = "pending", limit: int = 100):
    """长期记忆候选池：候选不等于已写入记忆。"""
    c = _c()
    rows = c.memory_gate.list_candidates(status, limit)
    for row in rows:
        try:
            row["evidence"] = json.loads(row.pop("evidence_json") or "[]")
        except (TypeError, json.JSONDecodeError):
            row["evidence"] = []
    return {"code": 200, "data": rows}


@router.post("/memory/candidates/{candidate_id}/confirm")
async def confirm_memory_candidate(candidate_id: str):
    try:
        data = await _c().memory_svc.confirm_candidate(candidate_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"code": 200, "data": data}


@router.post("/memory/candidates/{candidate_id}/reject")
async def reject_memory_candidate(candidate_id: str, request: Request):
    body = await read_json_object(request)
    try:
        _c().memory_svc.reject_candidate(
            candidate_id, str(body.get("reason") or "用户拒绝"))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"code": 200, "data": {"candidate_id": candidate_id, "status": "rejected"}}


@router.post("/memory/candidates/expire")
async def expire_memory_candidates():
    count = _c().memory_gate.expire()
    return {"code": 200, "data": {"expired": count}}


@router.get("/memory/domains")
async def domains():
    c = _c()
    data = c.palace.domains()
    names = [r["domain"] for r in data]
    labels = c.domain_labeler.map_for(names)
    # 存量/漏网领域兜底补翻（fire-and-forget，下次请求生效）
    c.domain_labeler.schedule(*[d for d in names if d not in labels])
    for r in data:
        r["label"] = labels.get(r["domain"], r["domain"])
    return {"code": 200, "data": data}


@router.get("/memory/domain-labels")
async def domain_labels():
    """当前全部领域的中文标签映射（列表徽章/详情/图谱图例共用）。"""
    c = _c()
    names = [r["domain"] for r in c.palace.domains()]
    labels = c.domain_labeler.map_for(names)
    c.domain_labeler.schedule(*[d for d in names if d not in labels])
    return {"code": 200, "data": labels}


@router.get("/memory/detail")
async def detail(id: str):
    c = _c()
    row = c.palace.get(id)
    if not row:
        raise HTTPException(status_code=404, detail="记忆不存在")
    f = Path(c.data_dir) / row["md_path"]
    if not f.exists():
        return {"code": 200, "data": {"id": id, "summary": row["summary"], "degraded": True}}
    doc = parse_memory_md(f.read_text(encoding="utf-8"))
    linked = []
    for lk in doc.links:
        lr = c.palace.get(lk.get("target"))
        if lr:
            linked.append(
                {"id": lr["id"], "title": lr["title"], "type": lk.get("type")})
    # 被引用记录：记忆资产的使用凭证（时间 + 所在会话，可跳转回对话）
    cites = c.db.query_all(
        "SELECT ce.message_id, ce.session_id, ce.cited_at, s.title AS session_title "
        "FROM citation_events ce LEFT JOIN sessions s ON ce.session_id=s.session_id "
        "WHERE ce.memory_id=? ORDER BY ce.cited_at DESC LIMIT 50", (id,))
    evidence = c.db.query_all(
        "SELECT evidence_id,source_type,source_ref,locator,excerpt,captured_at,status "
        "FROM memory_evidence WHERE memory_id=? AND status='active' "
        "ORDER BY captured_at DESC LIMIT 50", (id,))
    revisions = c.db.query_all(
        "SELECT revision_id,revision_no,operation,reason,created_at "
        "FROM memory_revisions WHERE memory_id=? ORDER BY revision_no DESC LIMIT 20", (id,))
    return {"code": 200, "data": {
        "id": id, "frontmatter": doc.frontmatter, "summary": doc.summary,
        "detail": doc.detail, "change_history": doc.change_history,
        "linked_memories": linked,
        "access_count": row["access_count"], "last_accessed": row["last_accessed"],
        "evidence": evidence, "revisions": revisions,
        "governance": {"verification_state": row.get("verification_state", "unverified"),
                       "freshness_state": row.get("freshness_state", "current"),
                       "usefulness_score": row.get("usefulness_score", 0),
                       "review_after": row.get("review_after"),
                       "superseded_by": row.get("superseded_by")},
        "citations": [{"message_id": r["message_id"], "session_id": r["session_id"],
                       "session_title": r["session_title"] or r["session_id"],
                       "cited_at": r["cited_at"]} for r in cites]}}


@router.get("/memory/{mid}/revisions")
async def revisions(mid: str):
    c = _c()
    rows = c.db.query_all(
        "SELECT revision_id,revision_no,operation,before_json,after_json,reason,created_at "
        "FROM memory_revisions WHERE memory_id=? ORDER BY revision_no DESC", (mid,))
    return {"code": 200, "data": [{**r,
        "before": json.loads(r.pop("before_json")) if r.get("before_json") else None,
        "after": json.loads(r.pop("after_json")) if r.get("after_json") else None}
        for r in rows]}


@router.post("/memory/{mid}/rollback")
async def rollback(mid: str, request: Request):
    body = await read_json_object(request)
    c = _c()
    rev = c.db.query_one(
        "SELECT after_json FROM memory_revisions WHERE memory_id=? AND revision_id=?",
        (mid, body.get("revision_id")))
    if not rev or not rev.get("after_json"):
        raise HTTPException(status_code=404, detail="版本不存在")
    snapshot = json.loads(rev["after_json"])
    fm = snapshot.get("frontmatter") or {}
    if fm.get("id") != mid:
        raise HTTPException(status_code=400, detail="版本与记忆不匹配")
    await c.fw.submit("memory", {
        "op": "update", "memory_id": mid, "frontmatter": fm,
        "summary": snapshot.get("summary", ""), "detail": snapshot.get("detail", ""),
        "change_history": snapshot.get("change_history", []),
        "links": fm.get("links", []), "entities": fm.get("entities", []),
        "reason": "用户回滚到历史版本", "timeline_event": "updated",
    }, wait=True)
    c.oplog.log("memory_rollback", mid)
    return {"code": 200, "data": {}}


@router.post("/memory/feedback")
async def memory_feedback(request: Request):
    """记忆级反馈直接进入检索与治理闭环，不依赖回答整体评分。"""
    body = await read_json_object(request)
    try:
        await _c().memory_svc.memory_feedback(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"code": 200, "data": {}}


@router.get("/memory/governance")
async def governance(status: str = "open", limit: int = 100):
    c = _c()
    limit = min(max(1, limit), 200)
    rows = c.db.query_all(
        "SELECT g.*,m.title,m.summary,m.confidence,m.lifecycle FROM memory_governance_items g "
        "LEFT JOIN memories m ON m.id=g.primary_memory_id WHERE g.status=? "
        "ORDER BY g.priority DESC,g.created_at DESC LIMIT ?", (status, limit))
    for r in rows:
        r["detail"] = json.loads(r.pop("detail_json")) if r.get("detail_json") else {}
    return {"code": 200, "data": rows}


@router.post("/memory/governance/{item_id}/resolve")
async def resolve_governance(item_id: str, request: Request):
    body = await read_json_object(request)
    action = body.get("action", "dismiss")
    try:
        _c().memory_svc.resolve_governance(item_id, action)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"code": 200, "data": {}}


@router.put("/memory/{mid}/attributes")
async def attributes(mid: str, request: Request):
    body = await read_json_object(request)
    c = _c()
    row = c.palace.get(mid)
    if not row:
        raise HTTPException(status_code=404, detail="记忆不存在")
    f = Path(c.data_dir) / row["md_path"]
    doc = parse_memory_md(f.read_text(encoding="utf-8"))
    if "is_important" in body:
        new_val = bool(body["is_important"])
        doc.frontmatter["is_important"] = new_val
        # 用户手动移出重要目录 → 置守卫标记，stable 升级不再自动置回；
        # 手动加回则清除守卫
        c.db.execute("UPDATE memories SET user_cleared_important=? WHERE id=?",
                     (0 if new_val else 1, mid))
    if "confidence" in body and body["confidence"] in ("strong", "medium", "low"):
        doc.frontmatter["confidence"] = body["confidence"]
    # archived 不在手动白名单内：归档必须走 archive op（移文件到 _archived/
    # + 清向量缓存 + 关闭 lint 建议），否则 md_path 与实际语义分叉导致恢复错乱
    if "lifecycle" in body and body["lifecycle"] in ("active", "stable", "stale"):
        doc.frontmatter["lifecycle"] = body["lifecycle"]
        if body["lifecycle"] != "stale":
            # 手动恢复活跃 → 自动归档周期计数清零
            c.db.execute(
                "UPDATE memories SET stale_lint_runs=0 WHERE id=?", (mid,))
    await c.fw.submit("memory", {"op": "update", "memory_id": mid,
                                 "frontmatter": doc.frontmatter, "summary": doc.summary,
                                 "detail": doc.detail, "change_history": doc.change_history,
                                 "links": doc.links, "entities": doc.entities,
                                 "reason": "手动调整属性"})
    c.oplog.log("memory_attr_edit", mid)
    return {"code": 200, "data": {}}


@router.post("/memory/archive")
async def archive(request: Request):
    body = await read_json_object(request)
    await _c().fw.submit("memory", {"op": "archive", "memory_id": body["id"]}, wait=True)
    return {"code": 200, "data": {}}


@router.post("/memory/restore")
async def restore(request: Request):
    body = await read_json_object(request)
    await _c().fw.submit("memory", {"op": "restore", "memory_id": body["id"]}, wait=True)
    return {"code": 200, "data": {}}


@router.post("/memory/delete")
async def delete(request: Request):
    body = await read_json_object(request)
    c = _c()
    await c.fw.submit("memory", {"op": "delete", "memory_id": body["id"]}, wait=True)
    c.oplog.log("memory_delete", body["id"])
    return {"code": 200, "data": {}}


@router.get("/memory/graph")
async def graph(limit: int = None, project_id: str = None,
                project_scope: str = "with_global"):
    """M2：project_id 参数化。
      缺省或 any    → 全部实体
      global / null → 仅全局
      具体 proj_xxx → 项目 + 全局 (with_global) 或 仅项目 (only)
    """
    c = _c()
    from memory import _constants as _mem_const
    max_nodes = min(limit or _mem_const.GRAPH_MAX_NODES, 500)
    max_edges = _mem_const.GRAPH_MAX_EDGES
    from memory.graph_layout import place_missing
    try:
        # place_missing 是同步 CPU/DB 重操作，跑在线程池避免阻塞事件循环
        # （与对话 SSE 同 loop，阻塞会冻结所有在线对话）
        await asyncio.to_thread(place_missing, c.db)
    except Exception:  # noqa: BLE001
        logger.warning("知识图谱增量布点失败，回退无坐标", exc_info=True)

    proj_where = ""
    proj_params: list = []
    if project_id in (None, "", "any"):
        pass
    elif project_id in ("global", "null"):
        proj_where = " WHERE e.project_id IS NULL"
    else:
        if project_scope == "only":
            proj_where = " WHERE e.project_id=?"
            proj_params.append(project_id)
        else:
            proj_where = " WHERE (e.project_id=? OR e.project_id IS NULL)"
            proj_params.append(project_id)

    total_count = c.db.query_one(
        "SELECT COUNT(*) cnt FROM memory_entities e" + proj_where,
        proj_params)["cnt"]
    nodes = c.db.query_all(
        "SELECT e.entity_id,e.entity_name,e.entity_type,e.primary_domain,"
        "e.memory_count,g.x,g.y "
        "FROM memory_entities e LEFT JOIN graph_layout g ON e.entity_id=g.entity_id"
        + proj_where +
        " ORDER BY e.memory_count DESC LIMIT ?",
        (*proj_params, max_nodes))
    node_ids = {n["entity_id"] for n in nodes}
    edges = c.db.query_all(
        "SELECT a.entity_id src, b.entity_id tgt, COUNT(*) w "
        "FROM memory_entity_links a JOIN memory_entity_links b "
        "ON a.memory_id=b.memory_id AND a.entity_id < b.entity_id "
        "GROUP BY a.entity_id, b.entity_id HAVING w>=1 ORDER BY w DESC LIMIT ?",
        (max_edges,))
    return {"code": 200, "data": {
        "nodes": [{"entity_id": n["entity_id"], "name": n["entity_name"],
                   "type": n["entity_type"], "domain": n["primary_domain"],
                   "memory_count": n["memory_count"],
                   "x": n["x"], "y": n["y"]} for n in nodes],
        "edges": [{"source": e["src"], "target": e["tgt"], "weight": e["w"]}
                  for e in edges if e["src"] in node_ids and e["tgt"] in node_ids],
        "total_count": total_count, "returned_count": len(nodes)}}


@router.get("/memory/graph/entity/{entity_id}/memories")
async def entity_memories(entity_id: str):
    c = _c()
    rows = c.db.query_all(
        "SELECT m.id,m.title,m.summary FROM memory_entity_links l "
        "JOIN memories m ON l.memory_id=m.id WHERE l.entity_id=?", (entity_id,))
    return {"code": 200, "data": [dict(r) for r in rows]}


@router.get("/memory/graph/entity/{entity_id}/neighbors")
async def entity_neighbors(entity_id: str, limit: int = 30, exclude_ids: str = ""):
    """邻居扩展（v3.0 §5.3）：中心节点 + 共现邻居（含坐标）+ 集内边。"""
    c = _c()
    limit = min(max(1, limit), 100)
    excluded = {x for x in exclude_ids.split(",") if x}
    from memory.graph_layout import place_missing
    try:
        place_missing(c.db)
    except Exception:  # noqa: BLE001
        logger.warning("邻居扩展前增量布点失败", exc_info=True)

    center = c.db.query_one(
        "SELECT e.entity_id,e.entity_name,e.entity_type,e.primary_domain,"
        "e.memory_count,g.x,g.y FROM memory_entities e "
        "LEFT JOIN graph_layout g ON e.entity_id=g.entity_id WHERE e.entity_id=?",
        (entity_id,))
    if not center:
        raise HTTPException(status_code=404, detail="实体不存在")
    # 共现邻居按 co_count 降序
    nb_rows = c.db.query_all(
        "SELECT b.entity_id tgt, COUNT(*) w FROM memory_entity_links a "
        "JOIN memory_entity_links b ON a.memory_id=b.memory_id AND b.entity_id!=a.entity_id "
        "WHERE a.entity_id=? GROUP BY b.entity_id ORDER BY w DESC", (entity_id,))
    picked = [r["tgt"] for r in nb_rows
              if r["tgt"] not in excluded and r["tgt"] != entity_id][:limit]
    neighbors = []
    if picked:
        ph = ",".join("?" * len(picked))
        nrows = c.db.query_all(
            f"SELECT e.entity_id,e.entity_name,e.entity_type,e.primary_domain,"
            f"e.memory_count,g.x,g.y FROM memory_entities e "
            f"LEFT JOIN graph_layout g ON e.entity_id=g.entity_id "
            f"WHERE e.entity_id IN ({ph})", picked)
        nmap = {r["entity_id"]: r for r in nrows}
        neighbors = [nmap[i] for i in picked if i in nmap]  # 保持 co_count 序
    # 集内边（中心 + 邻居两两共现）
    node_set = [entity_id] + picked
    edges = []
    if len(node_set) > 1:
        ph = ",".join("?" * len(node_set))
        erows = c.db.query_all(
            f"SELECT a.entity_id src, b.entity_id tgt, COUNT(*) w "
            f"FROM memory_entity_links a JOIN memory_entity_links b "
            f"ON a.memory_id=b.memory_id AND a.entity_id < b.entity_id "
            f"WHERE a.entity_id IN ({ph}) AND b.entity_id IN ({ph}) "
            f"GROUP BY a.entity_id,b.entity_id", node_set + node_set)
        edges = [{"source": e["src"], "target": e["tgt"], "weight": e["w"]}
                 for e in erows]

    def _node(r):
        return {"entity_id": r["entity_id"], "name": r["entity_name"],
                "type": r["entity_type"], "domain": r["primary_domain"],
                "memory_count": r["memory_count"], "x": r["x"], "y": r["y"]}
    return {"code": 200, "data": {
        "center": _node(center),
        "neighbors": [_node(r) for r in neighbors],
        "edges": edges}}


@router.get("/memory/timeline")
async def timeline(event_type: str = None, days: int = 7):
    c = _c()
    from datetime import timedelta
    cutoff = (now_cst() - timedelta(days=days)
              ).isoformat(timespec="seconds")
    q = ("SELECT t.memory_id, t.event_type, t.event_time, t.detail,"
         " m.title, m.summary FROM memory_timeline t"
         " LEFT JOIN memories m ON t.memory_id=m.id"
         " WHERE t.event_time>=?")
    params = [cutoff]
    if event_type:
        q += " AND t.event_type=?"
        params.append(event_type)
    q += " ORDER BY t.event_time DESC"
    rows = c.db.query_all(q, params)
    result = []
    for r in rows:
        result.append({"memory_id": r["memory_id"], "event_type": r["event_type"],
                       "event_time": r["event_time"], "detail": r["detail"],
                       "title": r["title"] or r["memory_id"],
                       "summary": r["summary"] or ""})
    return {"code": 200, "data": result}


@router.get("/memory/health")
async def health():
    c = _c()
    counts = c.lint.counts()
    score, breakdown = c.lint.health_score(counts)
    stats = c.palace.stats()
    return {"code": 200, "data": {
        "health_score": score, "score_breakdown": breakdown,
        "stats": {"total": stats["total"], "archived": stats["total_archived"],
                  "pending_confirm": counts["low_unconfirmed"],
                  "disputed": counts["disputed"], "stale": counts["stale"],
                  "orphan": counts["orphan"], "duplicate": counts["duplicate"],
                  "missing": counts["missing"], "failed_writes": counts["failed_writes"],
                  "draft_skills": c.skills.draft_count()},
        "lint_details": c.lint.lint_details(counts)}}


# /memory/lint/run 全量 LLM lint，无锁可被重复点击拖垮 API 配额与 CPU。
# 进程级互斥：同一时刻只跑一个 lint 任务，并发请求返回 409。
_lint_run_lock = asyncio.Lock()


@router.post("/memory/lint/run")
async def lint_run():
    c = _c()
    if _lint_run_lock.locked():
        raise HTTPException(status_code=409, detail="已有 lint 任务在运行，请稍后再试")
    async with _lint_run_lock:
        from memory.naming import task_id as mk
        tid = mk("lint")
        await c.lint_agent.run(tid)
    return {"code": 200, "data": {"task_id": tid}}


@router.post("/memory/lint/suggestions/accept")
async def accept_suggestion(request: Request):
    body = await read_json_object(request)
    c = _c()
    sid = body["suggestion_id"]
    row = c.db.query_one(
        "SELECT * FROM lint_suggestions WHERE suggestion_id=?", (sid,))
    if not row:
        raise HTTPException(status_code=404, detail="建议不存在")
    if row["suggestion_type"] == "orphan":
        # 采纳语义 = 用户确认该记忆内容正确、应当保留；建链只是尽力而为的附带动作。
        # 无论是否找到相似记忆都关闭建议，不能让“无候选”顶回用户的确认；
        # 后续若出现相似记忆，提炼/矛盾检测/批量补链路径仍会自动建链。
        linked = await c.linker.suggest_and_link_orphan(row["primary_memory_id"])
        c.db.execute(
            "UPDATE lint_suggestions SET status='adopted', resolved_at=? "
            "WHERE suggestion_id=?", (now_iso(), sid,))
        return {"code": 200, "data": {"linked": bool(linked), "linked_to": linked}}
    # duplicate → 合并：删除重复方，幸存者记 merged 时间线事件
    await c.fw.submit("memory", {"op": "delete", "memory_id": row["related_memory_id"]}, wait=True)
    with c.db.transaction() as conn:
        c.palace.add_timeline(conn, row["primary_memory_id"], "merged",
                              f"采纳重复建议，合并 {row['related_memory_id']}")
    c.db.execute("UPDATE lint_suggestions SET status='adopted', resolved_at=? "
                 "WHERE suggestion_id=?", (now_iso(), sid,))
    return {"code": 200, "data": {"linked": True}}


@router.post("/memory/lint/suggestions/dismiss")
async def dismiss_suggestion(request: Request):
    body = await read_json_object(request)
    c = _c()
    c.db.execute("UPDATE lint_suggestions SET status='dismissed', dismiss_reason=?, "
                 "resolved_at=? WHERE suggestion_id=?",
                 (body.get("reason"), now_iso(), body["suggestion_id"]))
    return {"code": 200, "data": {}}


@router.post("/memory/lint/duplicates/resolve")
async def resolve_duplicate(request: Request):
    """重复检测四选一裁决（交互与矛盾处理对齐）：keep_a/keep_b 删另一条并给
    幸存者记 merged 时间线；keep_both 视为非重复关闭建议；delete_both 两条都删。
    删除复用单条记忆物理删除链（图谱边/md/向量/矛盾自愈同事务清理）。"""
    body = await read_json_object(request)
    c = _c()
    sid, res = body["suggestion_id"], body.get("resolution")
    if res not in ("keep_a", "keep_b", "keep_both", "delete_both"):
        raise HTTPException(status_code=400, detail="无效的 resolution")
    row = c.db.query_one(
        "SELECT * FROM lint_suggestions WHERE suggestion_id=? "
        "AND suggestion_type='duplicate' AND status='open'", (sid,))
    if not row:
        raise HTTPException(status_code=404, detail="建议不存在或已处理")
    a, b = row["primary_memory_id"], row["related_memory_id"]
    if res == "keep_both":
        c.db.execute(
            "UPDATE lint_suggestions SET status='dismissed', "
            "dismiss_reason='not_duplicate', resolved_at=? WHERE suggestion_id=?",
            (now_iso(), sid))
        return {"code": 200, "data": {"deleted": []}}
    to_delete = {"keep_a": [b], "keep_b": [a], "delete_both": [a, b]}[res]
    survivor = {"keep_a": a, "keep_b": b, "delete_both": None}[res]
    for mid in to_delete:
        # 已被其它路径删除的幂等跳过，不阻断裁决落地
        if c.palace.get(mid) is None:
            continue
        await c.fw.submit("memory", {"op": "delete", "memory_id": mid}, wait=True)
        c.oplog.log("memory_delete", mid)
    if survivor:
        with c.db.transaction() as conn:
            c.palace.add_timeline(conn, survivor, "merged",
                                  f"重复裁决保留本条，删除 {to_delete[0]}")
    c.db.execute("UPDATE lint_suggestions SET status='adopted', resolved_at=? "
                 "WHERE suggestion_id=?", (now_iso(), sid))
    return {"code": 200, "data": {"deleted": to_delete}}


@router.get("/memory/conflicts")
async def conflicts():
    return {"code": 200, "data": _c().conflict.list_pending()}


@router.post("/memory/conflicts/resolve")
async def resolve_conflict(request: Request):
    body = await read_json_object(request)
    c = _c()
    await c.conflict.resolve(body["conflict_id"], body["resolution"])
    c.oplog.log("conflict_resolve",
                f"{body['conflict_id']} → {body['resolution']}")
    return {"code": 200, "data": {}}
