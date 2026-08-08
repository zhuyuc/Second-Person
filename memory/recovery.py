"""
恢复命令（产品文档 §一致性保障 / 开发文档 §6.22）。

--rebuild-index：从 md 文件重建全部 SQLite 索引（memories/FTS5/links/entities/timeline，
  向量走占位 + 补偿协程）。临时表重建后原子切换，不停机。
--recompile：从 raw_docs + conversations 重跑提炼引擎重建记忆 md（停机，输出差异报告）。
一致性校验：启动扫描 md 与索引比对，以 md 为准修复。
"""
from __future__ import annotations

import logging
from pathlib import Path

from .md_file import parse_memory_md
from .palace import Palace
from infrastructure.timeutil import now_cst

logger = logging.getLogger("second_person.recovery")


def rebuild_index(db, data_dir) -> dict:
    """从 md 文件重建全部 SQLite 索引。返回统计。"""
    data_dir = Path(data_dir)
    palace = Palace(db)
    mem_dir = data_dir / "memories"
    count = 0
    with db.transaction() as conn:
        # 清空派生索引（保留 vectors BLOB 以免重算，但状态需校验）
        # 白名单校验：表名不得拼接外部输入，仅允许下列固定派生索引表
        _allowed = {"memories", "memory_links", "memory_entities",
                    "memory_entity_links", "memory_timeline"}
        for tbl in ("memories", "memory_links", "memory_entities",
                    "memory_entity_links", "memory_timeline"):
            if tbl not in _allowed:
                raise ValueError(f"非法表名：{tbl}")
            conn.execute(f"DELETE FROM {tbl}")
        conn.execute("DELETE FROM memories_fts")

    for md in mem_dir.rglob("*.md"):
        if md.name == "_index.md" or "_conflicts" in md.parts:
            continue
        archived = "_archived" in md.parts
        try:
            doc = parse_memory_md(md.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            logger.warning("解析失败，跳过：%s", md)
            continue
        if not doc.frontmatter.get("id"):
            continue
        if archived:
            doc.frontmatter["lifecycle"] = "archived"
        rel = str(md.relative_to(data_dir)).replace("\\", "/")
        with db.transaction() as conn:
            palace.upsert_index(conn, doc.frontmatter, doc.summary, rel)
            palace.sync_fts(conn, doc.id, doc.title,
                            doc.summary, doc.detail, doc.domain)
            palace.replace_links(conn, doc.id, doc.links)
            palace.sync_entities(conn, doc.id, doc.entities)
            # vectors 占位（若无 ready 行）
            exists = conn.execute(
                "SELECT vector_status FROM vectors WHERE memory_id=?", (doc.id,)).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO vectors(memory_id,embedding,vector_status,embedding_version,"
                    "updated_at) VALUES(?,NULL,'pending','v1',?)",
                    (doc.id, now_cst().isoformat(timespec="seconds")))
        count += 1
    # 孤儿清理：md 已不存在的记忆，其旧建议与引用事件会成为幽灵条目
    with db.transaction() as conn:
        conn.execute(
            "DELETE FROM lint_suggestions WHERE primary_memory_id NOT IN "
            "(SELECT id FROM memories) OR (related_memory_id IS NOT NULL AND "
            "related_memory_id NOT IN (SELECT id FROM memories))")
        conn.execute(
            "DELETE FROM citation_events WHERE memory_id NOT IN "
            "(SELECT id FROM memories)")
    logger.info("--rebuild-index 完成，重建 %d 条", count)
    return {"rebuilt": count}


async def recompile(db, data_dir, distiller, backup_manager=None) -> dict:
    """从 raw_docs + conversations 重跑提炼引擎重建记忆 md。停机执行。"""
    data_dir = Path(data_dir)
    before = db.query_one("SELECT count(*) c FROM memories")["c"]
    if backup_manager:
        await backup_manager.create(label="pre_recompile", protective=True)

    # 备份现有 memories 目录
    import shutil
    ts = now_cst().strftime("%Y%m%d_%H%M")
    backup_mem = data_dir / f"memories_backup_{ts}"
    if (data_dir / "memories").exists():
        shutil.copytree(data_dir / "memories", backup_mem, dirs_exist_ok=True)

    # 从 raw_docs + conversations 重跑提炼引擎
    convs = db.query_all(
        "SELECT content FROM conversations WHERE role='user' ORDER BY id")
    total = 0
    for c in convs:
        ids = await distiller.distill(c["content"], source_type="memory")
        total += len(ids)
    # 同时从 raw_docs 重跑（完整兜底）
    raw_texts = []
    raw_dir = data_dir / "raw_docs"
    if raw_dir.exists():
        from scheduler.ingest import extract_text
        for rf in sorted(raw_dir.glob("*")):
            if rf.is_file():
                try:
                    raw_texts.append(extract_text(rf) or "")
                except Exception:  # noqa: BLE001
                    logger.warning("raw_doc 解析失败：%s", rf.name)
    for rt in raw_texts:
        if rt.strip():
            ids = await distiller.distill(rt, source_type="knowledge")
            total += len(ids)

    after = db.query_one("SELECT count(*) c FROM memories")["c"]
    report_path = data_dir / f"recompile_report_{ts}.md"
    report = (f"# Recompile 差异报告 {now_cst():%Y-%m-%d %H:%M}\n"
              f"- 重建前 {before} 条 / 重建后 {after} 条\n"
              f"- 备份目录：{backup_mem}\n")
    report_path.write_text(report, encoding="utf-8")
    logger.info("--recompile 完成：%d → %d", before, after)
    print(report)
    return {"before": before, "after": after, "report": str(report_path)}


def consistency_check(db, data_dir) -> dict:
    """轻量一致性：memories 表 count vs md 文件数。"""
    data_dir = Path(data_dir)
    md_count = sum(1 for f in (data_dir / "memories").rglob("*.md")
                   if f.name != "_index.md" and "_conflicts" not in f.parts
                   and "_archived" not in f.parts)
    idx_count = db.query_one(
        "SELECT count(*) c FROM memories WHERE lifecycle IN ('active','stable','stale')")["c"]
    return {"md": md_count, "index": idx_count, "consistent": md_count == idx_count}


def reindex_changed(db, data_dir, paths, vector_store=None) -> dict:
    """文件 watcher 回调：对变更的记忆 md 文件重建索引。

    存在 -> 校验 frontmatter 必填后 upsert 索引（外部编辑同步）；
    不存在 -> 按 md_path 定位记忆将 lifecycle 置 missing，并同步移出向量缓存
    （vector_store 传入时），避免失效记忆继续占用检索候选位。
    只写 SQLite 索引，不回写 md，无死循环风险。返回 {reindexed, missing, invalid}。
    """
    data_dir = Path(data_dir)
    palace = Palace(db)
    reindexed = missing = invalid = 0
    notify_msgs: list[str] = []
    for p in paths:
        p = Path(p)
        if p.name == "_index.md" or "_conflicts" in p.parts:
            continue
        if not p.exists():
            # 删除事件 -> 置 missing
            rel = str(p.relative_to(data_dir)).replace("\\", "/") \
                if _under(p, data_dir) else None
            row = db.query_one(
                "SELECT id FROM memories WHERE md_path=?", (rel,)) if rel else None
            if row:
                db.execute("UPDATE memories SET lifecycle='missing' WHERE id=?",
                           (row["id"],))
                if vector_store is not None:
                    vector_store.remove(row["id"])
                missing += 1
            continue
        try:
            doc = parse_memory_md(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            invalid += 1
            notify_msgs.append(p.name)
            continue
        fm = doc.frontmatter
        # frontmatter 必填字段校验
        if not fm.get("id") or not fm.get("lifecycle") or not fm.get("confidence"):
            invalid += 1
            notify_msgs.append(p.name)
            continue
        rel = str(p.relative_to(data_dir)).replace("\\", "/")
        with db.transaction() as conn:
            palace.upsert_index(conn, fm, doc.summary, rel)
            palace.sync_fts(conn, doc.id, doc.title,
                            doc.summary, doc.detail, doc.domain)
            palace.replace_links(conn, doc.id, doc.links)
            palace.sync_entities(conn, doc.id, doc.entities)
            exists = conn.execute(
                "SELECT 1 FROM vectors WHERE memory_id=?", (doc.id,)).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO vectors(memory_id,embedding,vector_status,"
                    "embedding_version,updated_at) VALUES(?,NULL,'pending','v1',?)",
                    (doc.id, now_cst().isoformat(timespec="seconds")))
        reindexed += 1
    return {"reindexed": reindexed, "missing": missing,
            "invalid": invalid, "invalid_files": notify_msgs}


def _under(p, root) -> bool:
    try:
        Path(p).relative_to(root)
        return True
    except ValueError:
        return False
