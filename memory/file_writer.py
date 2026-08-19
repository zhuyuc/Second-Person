"""
FileWriter —— 统一单写者队列（产品文档 §统一单写者 / 开发文档 §6.1）。

所有 md 主副本文件与其 SQLite 派生索引的写入只允许 FileWriter 执行。
- 生产者（主对话 + 5 个系统 Agent）只提交写请求到队列，不直接写文件或 SQLite
- 6 类处理器：memory / profile / soul_style / context_entry / skill / index
- FIFO 消费，SQLite 事务包裹，出错重试 3 次
- 持久化：memory/profile/soul_style/skill 先落 pending_writes 表再消费；
          context_entry/index 走内存队列（可从其他数据重建）
- batch 模式：一个请求含多条更新，单事务处理
- index 请求合并：队列中连续的 index 请求自动合并为一次
- 失败：重试 3 次仍失败 → status=failed + 推系统通知 + 保留 payload
- 优雅停机：停收新请求 → 排空队列（含持久化类型全部消费）→ WAL checkpoint
- 崩溃恢复：启动消费 pending_writes 中 pending/failed 的残余请求
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Callable

from infrastructure.event_bus import (EVT_MEMORY_CREATED, EVT_MEMORY_UPDATED,
                                      EVT_PROFILE_REBUILT, EVT_SOUL_STYLE_UPDATED)

from .md_file import MemoryDoc, parse_memory_md, serialize_memory_md
from .naming import memory_filename, normalize_domain
from infrastructure.timeutil import now_cst

logger = logging.getLogger("second_person.file_writer")

PERSISTENT_TYPES = {"memory", "profile",
                    "soul_style", "skill", "response_strategy"}
MEMORY_TYPES = {"memory", "profile", "soul_style",
                "context_entry", "skill", "index", "response_strategy"}
QUEUE_MAX = 10000
MAX_RETRY = 3


class QueueFullError(RuntimeError):
    pass


class WriteFailedError(RuntimeError):
    """wait=True 时写入重试耗尽仍失败（调用方据此保留现场，如 pending 不移除）。"""


class WriteRequest:
    __slots__ = ("write_id", "write_type", "payload",
                 "batch", "source", "future")

    def __init__(self, write_id: int, write_type: str, payload: dict,
                 batch: bool, source: str):
        self.write_id = write_id
        self.write_type = write_type
        self.payload = payload
        self.batch = batch
        self.source = source
        self.future: asyncio.Future | None = None


class FileWriter:
    def __init__(self, db, palace, vector_store, data_dir: str | Path,
                 event_bus=None, notifier: Callable[[str, str], None] | None = None,
                 embedding_version: str = "v1"):
        self.db = db
        self.palace = palace
        self.vs = vector_store
        self.data_dir = Path(data_dir)
        self.bus = event_bus
        self.notify = notifier or (lambda t, m: None)
        self.embedding_version = embedding_version
        self._queue: asyncio.Queue[WriteRequest] = asyncio.Queue(
            maxsize=QUEUE_MAX)
        self._mem_seq = 0  # 内存类型的负数序号
        self._running = False
        self._consumer_task: asyncio.Task | None = None
        self._pending_index = False  # index 请求合并标志
        # 可注入的领域回调（避免对 P4 模块的硬依赖）
        self.index_rebuild_fn: Callable[[], None] | None = None
        self.context_entry_apply_fn: Callable[[dict], None] | None = None
        # IndexBuilder.mark_dirty
        self.mark_dirty_fn: Callable[[str], None] | None = None
        # FileWatcher.mark_internal（写盘前标记，watcher 收到后跳过避免重复索引）
        self.mark_internal_fn: Callable[[str], None] | None = None
        # 失败通知撤回（NotificationManager.resolve）：失败写入重放成功且
        # 无 failed 残留时把历史 filewriter_failed 横幅改写为已恢复
        self.resolve_failed_fn: Callable[[], None] | None = None

    def _mark_internal(self, path) -> None:
        if self.mark_internal_fn:
            try:
                self.mark_internal_fn(str(path))
            except Exception:  # noqa: BLE001
                pass

    def mark_internal(self, path) -> None:
        """公开标记入口：程序内部写盘前调用，watcher 抑制窗口内忽略该路径
        （供 SoulManager 等不经 FileWriter 队列直接写盘的模块使用）。"""
        self._mark_internal(path)

    def _resolve_failed_notice(self) -> None:
        """无 failed 残留时撤回历史失败通知（幂等，重复调用无副作用）。"""
        if not self.resolve_failed_fn:
            return
        try:
            row = self.db.query_one(
                "SELECT COUNT(*) AS n FROM pending_writes WHERE status='failed'")
            if row and row["n"] == 0:
                self.resolve_failed_fn()
        except Exception:  # noqa: BLE001
            logger.warning("失败通知撤回失败", exc_info=True)

    # ---- 提交入口 ---------------------------------------------------------
    async def submit(self, write_type: str, payload: dict, batch: bool = False,
                     source: str = "internal", wait: bool = False) -> int:
        if write_type not in MEMORY_TYPES:
            raise ValueError(f"未知 write_type: {write_type}")
        if write_type in PERSISTENT_TYPES:
            cur = self.db.execute(
                "INSERT INTO pending_writes(write_type,payload,status,retry_count,created_at)"
                " VALUES(?,?,'pending',0,?)",
                (write_type, json.dumps(payload, ensure_ascii=False),
                 now_cst().isoformat(timespec="seconds")))
            write_id = cur.lastrowid
        else:
            self._mem_seq -= 1
            write_id = self._mem_seq
        req = WriteRequest(write_id, write_type, payload, batch, source)
        # wait=True：调用方需在写入真正落库后再返回（如 UI 发起的删除/归档、
        # pending 确认落盘）；写入最终失败时抛 WriteFailedError，调用方可据此保留现场。
        if wait:
            req.future = asyncio.get_event_loop().create_future()
        try:
            self._queue.put_nowait(req)
        except asyncio.QueueFull as e:
            raise QueueFullError("FileWriter 队列已满") from e
        if req.future is not None:
            ok = await req.future
            if not ok:
                raise WriteFailedError(
                    f"{write_type} 写入失败（已重试 {MAX_RETRY} 次）")
        return write_id

    # ---- 生命周期 ---------------------------------------------------------
    async def start(self) -> None:
        self._running = True
        # 绑定主循环：_dispatch 在工作线程执行时，其内部 bus.publish_nowait
        # 的协程订阅者需投递回主循环（避免临时循环串扰）
        if self.bus and hasattr(self.bus, "bind_loop"):
            self.bus.bind_loop()
        await self._recover_pending()
        self._consumer_task = asyncio.create_task(self._consume_loop())
        # 启动兜底：若已无 failed 残留（失败项早已重放成功），
        # 顺手撤回历史遗留的失败横幅
        self._resolve_failed_notice()
        logger.info("FileWriter 已启动")

    async def stop(self, drain_timeout: float = 30.0) -> None:
        """优雅停机：停收新请求，排空队列，WAL checkpoint。"""
        self._running = False
        try:
            await asyncio.wait_for(self._queue.join(), timeout=drain_timeout)
        except asyncio.TimeoutError:
            logger.warning("FileWriter 排空超时，仍有未消费请求（留 pending_writes 供恢复）")
        if self._consumer_task:
            self._consumer_task.cancel()
        self.db.wal_checkpoint("TRUNCATE")

    async def drain(self) -> None:
        """等待当前已入队的写请求全部消费完（不停止消费循环）。"""
        await self._queue.join()

    async def _recover_pending(self) -> None:
        rows = self.db.query_all(
            "SELECT id,write_type,payload FROM pending_writes "
            "WHERE status IN ('pending','processing','failed') ORDER BY id")
        for r in rows:
            payload = json.loads(r["payload"])
            req = WriteRequest(r["id"], r["write_type"],
                               payload, False, "recover")
            await self._queue.put(req)
        if rows:
            logger.info("崩溃恢复：重放 %d 条残余写请求", len(rows))

    # ---- 消费循环 ---------------------------------------------------------
    async def _consume_loop(self) -> None:
        while True:
            req = await self._queue.get()
            try:
                await self._process_with_retry(req)
            finally:
                self._queue.task_done()

    async def _process_with_retry(self, req: WriteRequest) -> None:
        success = False
        try:
            # index 请求合并：若队列后面还有 index 请求，本次跳过合并到下次
            if req.write_type == "index":
                self._pending_index = True
                if any(getattr(q, "write_type", None) == "index"
                       # type: ignore[attr-defined]
                       for q in list(self._queue._queue)):
                    success = True  # 合并到后续请求执行，非失败
                    return
                self._pending_index = False

            for attempt in range(1, MAX_RETRY + 1):
                try:
                    # _dispatch 含大量同步文件读写 + 事务，丢工作线程避免阻塞事件循环；
                    # FileWriter 单消费者逐条 await，仍串行，不引入内部并发
                    await asyncio.to_thread(self._dispatch, req)
                    if req.write_type in PERSISTENT_TYPES:
                        await self.db.execute_async(
                            "UPDATE pending_writes SET status='done' WHERE id=?",
                            (req.write_id,))
                        # 重放/重扫成功：失败项全部消化后撤回失败横幅
                        if req.source in ("recover", "rescan"):
                            self._resolve_failed_notice()
                    success = True
                    return
                except Exception as e:  # noqa: BLE001
                    logger.exception("写入失败(第 %d 次)：type=%s",
                                     attempt, req.write_type)
                    if attempt >= MAX_RETRY:
                        if req.write_type in PERSISTENT_TYPES:
                            await self.db.execute_async(
                                "UPDATE pending_writes SET status='failed', retry_count=? "
                                "WHERE id=?", (attempt, req.write_id))
                        self.notify("filewriter_failed",
                                    f"写入失败：{req.write_type}（{e}），可在系统状态页重试")
                    await asyncio.sleep(0.1 * attempt)
        finally:
            # 唤醒等待写入完成的调用方（wait=True）；携带成败状态，
            # 失败时 submit 抛 WriteFailedError 让调用方保留现场
            if req.future is not None and not req.future.done():
                req.future.set_result(success)

    def _dispatch(self, req: WriteRequest) -> None:
        handler = {
            "memory": self._h_memory,
            "profile": self._h_profile,
            "soul_style": self._h_soul_style,
            "context_entry": self._h_context_entry,
            "skill": self._h_skill,
            "index": self._h_index,
            "response_strategy": self._h_response_strategy,
        }[req.write_type]
        if req.batch:
            for item in req.payload.get("items", []):
                handler(item)
        else:
            handler(req.payload)

    # ---- memory 处理器 ----------------------------------------------------
    def _h_memory(self, p: dict[str, Any]) -> None:
        op = p.get("op", "create")
        if op in ("create", "update"):
            self._memory_write(p, op)
        elif op == "add_link":
            self._memory_add_link(p)
        elif op == "delete":
            self._memory_delete(p["memory_id"])
        elif op == "archive":
            self._memory_move(p["memory_id"], to_archived=True)
        elif op == "restore":
            self._memory_move(p["memory_id"], to_archived=False)
        else:
            raise ValueError(f"未知 memory op: {op}")

    def _memory_write(self, p: dict[str, Any], op: str) -> None:
        fm = dict(p["frontmatter"])
        if op == "create" and not fm.get("id"):
            seq = self.palace.next_memory_seq()
            from .naming import memory_id as mk_mid
            fm["id"] = mk_mid(seq)
        mid = fm["id"]
        # 写层兜底净化（v3 修复）：历史残留/外部导入的脏 domain（含反斜杠等）
        # 在拼路径前规范化，避免 Windows mkdir 失败导致写请求永久重试
        fm["domain"] = normalize_domain(fm.get("domain", "general"))
        domain = fm["domain"]
        summary = p.get("summary", "")
        detail = p.get("detail", "")

        # 版本审计在覆盖文件前捕获旧快照；revision 只记录索引中可恢复的
        # 事实内容，不把向量或运行时计数混入版本 diff。
        before_snapshot = None
        if existing_row := self.palace.get(mid):
            old_path = self.data_dir / existing_row["md_path"]
            if old_path.exists():
                try:
                    old_doc = parse_memory_md(old_path.read_text(encoding="utf-8"))
                    before_snapshot = {
                        "frontmatter": old_doc.frontmatter,
                        "summary": old_doc.summary,
                        "detail": old_doc.detail,
                        "change_history": old_doc.change_history,
                    }
                except Exception:  # noqa: BLE001
                    logger.warning("读取记忆旧版本失败：%s", mid, exc_info=True)

        # 组装 md 文档 + 变更历史追加
        doc = MemoryDoc(frontmatter=fm, summary=summary, detail=detail,
                        change_history=list(p.get("change_history", [])))
        if p.get("change_log"):
            doc.change_history.insert(0, p["change_log"])

        # 目录移动：update 时 domain 变更需移动 md 文件
        mem_dir = self.data_dir / "memories" / domain
        mem_dir.mkdir(parents=True, exist_ok=True)
        if existing_row and existing_row["md_path"]:
            old_path = self.data_dir / existing_row["md_path"]
            fname = old_path.name
            new_path = mem_dir / fname
            if old_path.exists() and old_path != new_path:
                old_path.rename(new_path)
            md_path_abs = new_path
        else:
            # 幂等：同 mid 既有文件（上次事务失败重试/崩溃重放的残留）直接复用，
            # 多余同 mid 副本一并清理，避免重试产生 _2/_3 重复文件
            leftovers = sorted(mem_dir.glob(f"{mid}_*.md"))
            if leftovers:
                md_path_abs = leftovers[0]
                for extra in leftovers[1:]:
                    self._mark_internal(extra)
                    try:
                        extra.unlink()
                    except OSError:
                        pass
            else:
                existing = {f.name for f in mem_dir.glob("*.md")}
                fname = memory_filename(mid, fm.get("title", ""), existing)
                md_path_abs = mem_dir / fname
        md_rel = str(md_path_abs.relative_to(self.data_dir)).replace("\\", "/")

        created_file = not md_path_abs.exists()
        md_path_abs.write_text(serialize_memory_md(doc), encoding="utf-8")
        self._mark_internal(md_path_abs)

        try:
            with self.db.transaction() as conn:
                self.palace.upsert_index(conn, fm, summary, md_rel)
                self.palace.sync_fts(conn, mid, fm.get(
                    "title", ""), summary, detail, domain)
                self.palace.replace_links(
                    conn, mid, p.get("links", fm.get("links", [])))
                self.palace.sync_entities(conn, mid, p.get(
                    "entities", fm.get("entities", [])),
                    entity_types=p.get("entity_types"))
                self.palace.add_timeline(
                    conn, mid,
                    # 语义事件覆盖：演变/合并/导入等业务语义由调用方传入，
                    # 避免被 op 二分（create/update）抹平成 新建/更新
                    p.get("timeline_event")
                    or ("created" if op == "create" else "updated"),
                    p.get("reason", ""))
                rev_no = conn.execute(
                    "SELECT COALESCE(MAX(revision_no), 0) + 1 AS n "
                    "FROM memory_revisions WHERE memory_id=?", (mid,)).fetchone()["n"]
                after_snapshot = {
                    "frontmatter": fm,
                    "summary": summary,
                    "detail": detail,
                    "change_history": doc.change_history,
                }
                conn.execute(
                    "INSERT INTO memory_revisions(revision_id,memory_id,revision_no,"
                    "operation,before_json,after_json,reason,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (f"rev_{uuid.uuid4().hex[:12]}", mid, rev_no, op,
                     json.dumps(before_snapshot, ensure_ascii=False) if before_snapshot else None,
                     json.dumps(after_snapshot, ensure_ascii=False),
                     p.get("reason", ""), now_cst().isoformat(timespec="seconds")))
                for ev in p.get("evidence_refs", []) or []:
                    if not isinstance(ev, dict):
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO memory_evidence(evidence_id,memory_id,source_type,"
                        "source_ref,locator,excerpt,excerpt_hash,captured_at,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (ev.get("evidence_id") or f"ev_{uuid.uuid4().hex[:12]}", mid,
                         ev.get("source_type", "inference"), ev.get("source_ref"),
                         ev.get("locator"), ev.get("excerpt"), ev.get("excerpt_hash"),
                         ev.get("captured_at") or now_cst().isoformat(timespec="seconds"),
                         now_cst().isoformat(timespec="seconds")))
                # 没有上游证据定位时仍保留最小来源凭证，使自动沉淀的记忆
                # 不会成为“无来源”的黑箱。主动记忆会传入更精确的原文证据。
                if op == "create" and not p.get("evidence_refs"):
                    conn.execute(
                        "INSERT INTO memory_evidence(evidence_id,memory_id,source_type,"
                        "excerpt,captured_at,created_at) VALUES(?,?,?,?,?,?)",
                        (f"ev_{uuid.uuid4().hex[:12]}", mid,
                         fm.get("created_by", "distiller"), summary[:500],
                         now_cst().isoformat(timespec="seconds"),
                         now_cst().isoformat(timespec="seconds")))
                # vectors 占位行或直接写入 Distiller 已取到的向量
                emb = p.get("embedding")
                if emb:
                    conn.execute(
                        "INSERT INTO vectors(memory_id,embedding,vector_status,dim,"
                        "embedding_version,updated_at) VALUES(?,?,?,?,?,?) "
                        "ON CONFLICT(memory_id) DO UPDATE SET embedding=excluded.embedding,"
                        "vector_status='ready',dim=excluded.dim,updated_at=excluded.updated_at",
                        (mid, __import__("numpy").asarray(emb, dtype="float32").tobytes(),
                         "ready", len(emb), self.embedding_version,
                         now_cst().isoformat(timespec="seconds")))
                else:
                    conn.execute(
                        "INSERT OR IGNORE INTO vectors(memory_id,embedding,vector_status,"
                        "embedding_version,updated_at) VALUES(?,NULL,'pending',?,?)",
                        (mid, self.embedding_version,
                         now_cst().isoformat(timespec="seconds")))
        except Exception:
            # 事务失败时清理本次新建的 md，避免孤儿文件导致重试时被当作命名冲突
            if created_file:
                try:
                    md_path_abs.unlink()
                except OSError:
                    pass
            raise

        if p.get("embedding"):
            self.vs.add(mid, p["embedding"])
        # 提交 index 重建 + 发事件
        self._pending_index = True
        if self.mark_dirty_fn:
            self.mark_dirty_fn(domain)
        if self.bus:
            self.bus.publish_nowait(
                EVT_MEMORY_CREATED if op == "create" else EVT_MEMORY_UPDATED,
                {"memory_id": mid})

    def _memory_add_link(self, p: dict[str, Any]) -> None:
        """原子追加单条引用边：消费时才读最新 md，避免提交时快照竞态丢边。
        FIFO 保证此前排队的 create/update 已落盘；双方任一已删除则静默跳过。"""
        mid, target = p["memory_id"], p["target"]
        link_type = p.get("link_type", "related")
        row = self.palace.get(mid)
        if not row or not self.palace.get(target):
            logger.info("add_link 跳过：%s→%s 已不存在", mid, target)
            return
        f = self.data_dir / row["md_path"]
        if not f.exists():
            logger.warning("add_link 跳过：%s md 文件缺失", mid)
            return
        doc = parse_memory_md(f.read_text(encoding="utf-8"))
        links = [l for l in doc.links if isinstance(l, dict)]
        if any(l.get("target") == target for l in links):
            return  # 幂等：重试/重放不重复追加
        links.append({"target": target, "type": link_type})
        doc.frontmatter["links"] = links
        self._mark_internal(f)
        f.write_text(serialize_memory_md(doc), encoding="utf-8")
        with self.db.transaction() as conn:
            self.palace.add_link(conn, mid, target, link_type)
        self._pending_index = True
        if self.mark_dirty_fn:
            self.mark_dirty_fn(row["domain"])
        if self.bus:
            self.bus.publish_nowait(EVT_MEMORY_UPDATED, {"memory_id": mid})

    def _memory_delete(self, mid: str) -> None:
        row = self.palace.get(mid)
        # 先删文件再清索引：文件删除失败时 DB 事务可回滚，避免索引已清但文件残留
        if row and row["md_path"]:
            f = self.data_dir / row["md_path"]
            self._mark_internal(f)
            if f.exists():
                f.unlink()
        with self.db.transaction() as conn:
            for bl in self.palace.backlinks(mid):
                self._remove_link_in_md(bl["source_id"], mid)
            self.palace.delete_all_indexes(conn, mid)
        self.vs.remove(mid)
        # 来源记忆被删除 → 涉及它的 pending 矛盾自动 resolved，对侧恢复 confidence
        self._auto_resolve_conflicts(mid)
        if self.mark_dirty_fn and row:
            self.mark_dirty_fn(row["domain"])
        self._pending_index = True

    def _auto_resolve_conflicts(self, deleted_mid: str) -> None:
        """产品文档 §记忆删除的引用清理：矛盾来源 A/B 被删除时，
        该矛盾自动标记 resolved 并注明“来源记忆已删除”；幸存一侧从
        confidence_before_dispute 恢复置信度（无则 medium）。"""
        import re
        from .md_file import dump_frontmatter_doc, split_frontmatter
        cdir = self.data_dir / "memories" / "_conflicts"
        if not cdir.exists():
            return
        for f in cdir.glob("conflict_*.md"):
            try:
                fm, body = split_frontmatter(f.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if fm.get("status") != "pending":
                continue
            ids = re.findall(r"\[\[(mem_\w+)\]\]", body)
            if deleted_mid not in ids:
                continue
            fm["status"] = "resolved"
            fm["resolved_at"] = now_cst().strftime("%Y-%m-%d")
            body += f"\n- 裁决：来源记忆 {deleted_mid} 已删除，自动关闭 @ {fm['resolved_at']}\n"
            f.write_text(dump_frontmatter_doc(fm, body), encoding="utf-8")
            for other in ids:
                if other != deleted_mid:
                    self._restore_confidence_sync(other)

    def _restore_confidence_sync(self, mid: str) -> None:
        """同步恢复一条 disputed 记忆的置信度（FileWriter 内部，单写者不受损）。"""
        row = self.palace.get(mid)
        if not row or row["confidence"] != "disputed":
            return
        f = self.data_dir / row["md_path"]
        if not f.exists():
            return
        doc = parse_memory_md(f.read_text(encoding="utf-8"))
        before = doc.frontmatter.pop("confidence_before_dispute", "medium")
        doc.frontmatter["confidence"] = before or "medium"
        doc.frontmatter["links"] = [
            l for l in doc.links if isinstance(l, dict) and l.get("type") != "contradicts"]
        doc.links = doc.frontmatter["links"]
        doc.change_history.insert(
            0, f"[{now_cst():%Y-%m-%d}] 对立记忆已删除，恢复 confidence={doc.frontmatter['confidence']}")
        f.write_text(serialize_memory_md(doc), encoding="utf-8")
        self._mark_internal(f)
        with self.db.transaction() as conn:
            self.palace.upsert_index(
                conn, doc.frontmatter, doc.summary,
                str(f.relative_to(self.data_dir)).replace("\\", "/"))
            self.palace.replace_links(conn, mid, doc.links)

    def _memory_move(self, mid: str, to_archived: bool) -> None:
        row = self.palace.get(mid)
        if not row:
            return
        cur = self.data_dir / row["md_path"]
        domain = normalize_domain(row["domain"])
        if to_archived:
            dst_dir = self.data_dir / "memories" / "_archived" / domain
            new_life = "archived"
            evt = "archived"
        else:
            dst_dir = self.data_dir / "memories" / domain
            new_life = "active"
            evt = "updated"
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / cur.name
        if cur.exists():
            self._mark_internal(cur)
            self._mark_internal(dst)
            cur.rename(dst)
        md_rel = str(dst.relative_to(self.data_dir)).replace("\\", "/")
        with self.db.transaction() as conn:
            conn.execute("UPDATE memories SET lifecycle=?, md_path=?, updated_at=?, "
                         "stale_lint_runs=0 WHERE id=?",
                         (new_life, md_rel, now_cst().isoformat(timespec="seconds"), mid))
            self.palace.add_timeline(conn, mid, evt,
                                     "归档" if to_archived else "恢复")
            if to_archived:
                # 归档后不再属于孤立/重复口径，关闭其残留的 open 建议避免列表幽灵条目
                conn.execute(
                    "UPDATE lint_suggestions SET status='dismissed', dismiss_reason='archived', "
                    "resolved_at=? WHERE status='open' AND (primary_memory_id=? OR related_memory_id=?)",
                    (now_cst().isoformat(timespec="seconds"), mid, mid))
        if to_archived:
            self.vs.remove(mid)
        else:
            # 恢复对称性：vectors 表 ready 行回加内存缓存，否则重启前只剩 FTS 单路可查
            vrow = self.db.query_one(
                "SELECT embedding FROM vectors WHERE memory_id=? "
                "AND vector_status='ready' AND embedding IS NOT NULL", (mid,))
            if vrow:
                from .vector_store import deserialize_vector
                self.vs.add(mid, deserialize_vector(vrow["embedding"]))
        if self.mark_dirty_fn:
            self.mark_dirty_fn(domain)
        self._pending_index = True

    def _remove_link_in_md(self, source_mid: str, target_mid: str) -> None:
        row = self.palace.get(source_mid)
        if not row:
            return
        f = self.data_dir / row["md_path"]
        if not f.exists():
            return
        doc = parse_memory_md(f.read_text(encoding="utf-8"))
        links = [lk for lk in doc.links if isinstance(lk, dict) and lk.get("target") != target_mid]
        doc.frontmatter["links"] = links
        self._mark_internal(f)
        f.write_text(serialize_memory_md(doc), encoding="utf-8")

    # ---- profile 处理器 ---------------------------------------------------
    def _h_profile(self, p: dict[str, Any]) -> None:
        path = self.data_dir / "profile" / "user_profile.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(p["content"], encoding="utf-8")
        if self.bus:
            self.bus.publish_nowait(EVT_PROFILE_REBUILT, {})

    # ---- response_strategy 处理器（v3 §画像扩展） -------------------------
    def _h_response_strategy(self, p: dict[str, Any]) -> None:
        """写入 RESPONSE_STRATEGY.md：按 context_label 场景分区 upsert。

        payload: {"scene": "opinion", "entry": "已确认偏好内容"}；
        同场景旧条目被新条目替换（用户偏好逐场景覆盖，v3 §八）。
        """
        scene = str(p.get("scene") or "other").strip()[:20]
        entry = str(p.get("entry") or "").strip()
        if not entry:
            return
        path = self.data_dir / "profile" / "RESPONSE_STRATEGY.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        header = f"## {scene}"
        block = f"{header}\n{entry}\n"
        if header in text:
            # 替换既有场景段：截取到下一个 "## " 之前
            start = text.index(header)
            nxt = text.find("\n## ", start)
            end = len(text) if nxt == -1 else nxt
            text = text[:start] + block + text[end:]
        else:
            text = (text.rstrip() + "\n\n" + block) if text.strip() else block
        self.mark_internal(path)  # 防 watcher 误判为外部修改
        path.write_text(text.rstrip() + "\n", encoding="utf-8")

    # ---- soul_style 处理器 ------------------------------------------------
    def _h_soul_style(self, p: dict[str, Any]) -> None:
        # 由 soul 管理器注入具体落盘逻辑；此处提供基础落盘
        from soul.soul_manager import apply_soul_style_write  # 延迟导入
        # 程序内部写入（确认人格/回滚/手动编辑）：标记 internal，避免 watcher
        # 将自身写入误判为外部修改而推送 “soul_reloaded” 通知
        self.mark_internal(Path(self.data_dir) / "soul" / "SOUL_STYLE.md")
        apply_soul_style_write(self.data_dir, p)
        if self.bus:
            self.bus.publish_nowait(EVT_SOUL_STYLE_UPDATED, {
                                    "section": p.get("section")})

    # ---- context_entry 处理器 --------------------------------------------
    def _h_context_entry(self, p: dict[str, Any]) -> None:
        if self.context_entry_apply_fn:
            self.context_entry_apply_fn(p.get("patch", {}))

    # ---- skill 处理器 -----------------------------------------------------
    def _h_skill(self, p: dict[str, Any]) -> None:
        from soul.skill_manager import apply_skill_write  # 延迟导入
        apply_skill_write(self.data_dir, self.db, p)

    # ---- index 处理器 -----------------------------------------------------
    def _h_index(self, p: dict[str, Any]) -> None:
        if self.index_rebuild_fn:
            self.index_rebuild_fn()

    async def flush_pending_index(self) -> None:
        """由后置钩子调用：若有累积的 index 重建标志，提交一次 index 请求。"""
        if self._pending_index:
            self._pending_index = False
            await self.submit("index", {})

    async def rescan_failed(self) -> int:
        """重扫 failed 状态的 pending_writes，重置为 pending 并重新入队（夜间维护链）。"""
        rows = self.db.query_all(
            "SELECT id,write_type,payload FROM pending_writes WHERE status='failed' ORDER BY id")
        for r in rows:
            self.db.execute(
                "UPDATE pending_writes SET status='pending', retry_count=0 WHERE id=?",
                (r["id"],))
            payload = json.loads(r["payload"])
            await self._queue.put(WriteRequest(r["id"], r["write_type"], payload, False,
                                               "rescan"))
        if rows:
            logger.info("failed 写入重扫：重新入队 %d 条", len(rows))
        return len(rows)
