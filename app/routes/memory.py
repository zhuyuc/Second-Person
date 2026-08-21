"""记忆中心接口（开发文档 §二）。"""
from __future__ import annotations

import logging
import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Request
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
        page_rows = rows[start:start + page_size]
    else:
        # SQL 级分页：避免记忆库增长后全表拉取在事件循环上线性变慢
        page_rows = c.db.query_all(
            f"SELECT * FROM memories WHERE {clause} "
            f"ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (*params, page_size, start))
    stats = c.palace.stats()
    score, _ = c.lint.health_score()
    return {"code": 200, "data": {
        "total": stats["total"],
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
        return {"code": 404, "message": "记忆不存在", "trace_id": None, "details": None}
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
        return {"code": 404, "message": "版本不存在", "trace_id": None, "details": None}
    snapshot = json.loads(rev["after_json"])
    fm = snapshot.get("frontmatter") or {}
    if fm.get("id") != mid:
        return {"code": 400, "message": "版本与记忆不匹配", "trace_id": None, "details": None}
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
    c = _c()
    mid = body.get("memory_id")
    kind = body.get("feedback_type")
    if kind not in {"irrelevant", "stale", "incorrect", "helpful"} or not c.palace.get(mid):
        return {"code": 400, "message": "无效的记忆反馈", "trace_id": None, "details": None}
    c.lifecycle.record_feedback(mid, kind, body.get("message_id"), body.get("query_text"))
    if kind == "stale":
        await c.lifecycle.downvote_stale(mid)
    elif kind == "incorrect":
        row = c.palace.get(mid)
        c.db.execute(
            "INSERT INTO memory_governance_items(item_id,item_type,primary_memory_id,"
            "priority,status,reason,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (f"gov_{uuid.uuid4().hex[:12]}", "memory_incorrect", mid,
             (row.get("access_count", 0) or 0) + 3, "open", "用户标记记忆内容不正确",
             json.dumps({"query": body.get("query_text")}, ensure_ascii=False), now_iso()))
    elif kind == "helpful":
        await c.lifecycle.upvote_upgrade(mid)
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
    if action not in {"dismiss", "reviewed"}:
        return {"code": 400, "message": "无效的治理动作", "trace_id": None, "details": None}
    _c().db.execute(
        "UPDATE memory_governance_items SET status=?,resolved_at=? WHERE item_id=? AND status='open'",
        ("dismissed" if action == "dismiss" else "resolved", now_iso(), item_id))
    return {"code": 200, "data": {}}


@router.put("/memory/{mid}/attributes")
async def attributes(mid: str, request: Request):
    body = await read_json_object(request)
    c = _c()
    row = c.palace.get(mid)
    if not row:
        return {"code": 404, "message": "记忆不存在", "trace_id": None, "details": None}
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
async def graph(limit: int = None):
    c = _c()
    max_nodes = min(limit or c.config.get("graph_max_nodes", 300), 500)
    max_edges = c.config.get("graph_max_edges", 2000)
    # 请求路径只做增量布点（O(新增实体数) 毫秒级），禁止全量重算阻塞事件循环；
    # 全量力导向精排由夜间维护链在工作线程兑底刷新。
    from memory.graph_layout import place_missing
    try:
        place_missing(c.db)
    except Exception:  # noqa: BLE001
        logger.warning("知识图谱增量布点失败，回退无坐标", exc_info=True)
    total_count = c.db.query_one(
        "SELECT COUNT(*) cnt FROM memory_entities")["cnt"]
    nodes = c.db.query_all(
        "SELECT e.entity_id,e.entity_name,e.entity_type,e.primary_domain,"
        "e.memory_count,g.x,g.y "
        "FROM memory_entities e LEFT JOIN graph_layout g ON e.entity_id=g.entity_id "
        "ORDER BY e.memory_count DESC LIMIT ?", (max_nodes,))
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
        return {"code": 404, "message": "实体不存在", "trace_id": None, "details": None}
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


@router.post("/memory/lint/run")
async def lint_run():
    c = _c()
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
        return {"code": 404, "message": "建议不存在", "trace_id": None, "details": None}
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
        return {"code": 400, "message": "无效的 resolution", "trace_id": None,
                "details": None}
    row = c.db.query_one(
        "SELECT * FROM lint_suggestions WHERE suggestion_id=? "
        "AND suggestion_type='duplicate' AND status='open'", (sid,))
    if not row:
        return {"code": 404, "message": "建议不存在或已处理", "trace_id": None,
                "details": None}
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
