"""
Distiller —— 提炼引擎（产品文档 §提炼引擎/§事实归属原则/§跨时间信息 / 开发文档 §6.5）。

判定顺序：归属判定 → 跨时间合并检查 → 相似度过滤 → 通过 FileWriter 写入
归属优先级：技能 > SOUL 反馈 > 会话级事实 > 待验证推断 > 已验证经验 > 外部导入
跨时间合并（仅 verified/inferred/imported 走）：
  相似度 > dedup_merge_threshold(0.85) → LLM 判定关系：
    same → 合并升 confidence（首次不升，第 2 次起才升）
    evolved → 合并到同一 md，详情分层保留新旧观点，不升 confidence
    contradicts → 保留两条 + contradicts + 双方 disputed + conflict 文件
  主题不同 dedup_link_threshold(0.6)~0.85 → 新建 + related
  低于 0.6 → 新建
比较范围：新记忆向量在 numpy 缓存取 top-20 候选，只在候选内判定
向量获取：Distiller 内部同步取一次 Embedding（不在 FileWriter 队列内）
取向量失败：降级 BM25 去重（合并 0.75 / 建引用 0.5-0.75 双区间），
vector_status='pending' 由补偿协程后补
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable
from infrastructure.timeutil import now_cst

logger = logging.getLogger("second_person.distiller")

ATTRIBUTION_CONFIDENCE = {"inferred": "low",
                          "verified": "medium", "imported": "medium"}

# BM25 降级去重双区间（产品文档 §Embedding 不可用时的去重降级）
BM25_MERGE_THRESHOLD = 0.75
BM25_LINK_THRESHOLD = 0.5


def normalize_entities(raw: list) -> tuple[list[str], dict[str, str]]:
    """实体归一：兼容 ["名称"] 与 [{"name":..,"type":..}] 两种输出。
    返回 (名称列表, {名称: 类型})。"""
    names: list[str] = []
    types: dict[str, str] = {}
    for e in raw or []:
        if isinstance(e, dict):
            name = (e.get("name") or "").strip()
            if not name:
                continue
            names.append(name)
            if e.get("type"):
                types[name] = str(e["type"]).strip()
        elif isinstance(e, str) and e.strip():
            names.append(e.strip())
    return names, types


class Distiller:
    def __init__(self, db, palace, vector_store, file_writer, linker, conflict_detector,
                 config, *, extract_fn: Callable[[str, str], Awaitable[dict]],
                 embed_fn: Callable[[list[str]], Awaitable[list[list[float]]]],
                 skill_draft_fn: Callable[[dict],
                                          Awaitable[None]] | None = None,
                 soul_feedback_fn: Callable[[dict],
                                            Awaitable[None]] | None = None,
                 merge_judge_fn: Callable[[dict, dict],
                                          Awaitable[dict]] | None = None):
        self.db = db
        self.palace = palace
        self.vs = vector_store
        self.fw = file_writer
        self.linker = linker
        self.conflict = conflict_detector
        self.config = config
        self.extract_fn = extract_fn
        self.embed_fn = embed_fn
        self.skill_draft_fn = skill_draft_fn
        self.soul_feedback_fn = soul_feedback_fn
        self.merge_judge_fn = merge_judge_fn
        # 回溯去重本次会话已删除的重复条目（FileWriter 删除异步，
        # 删除未落库前 palace.get 仍可返回行，靠此集合避免重复处理）。
        self._rededup_deleted: set[str] = set()

    async def distill(self, text: str, source_type: str = "memory") -> list[str]:
        """提炼一段文本，返回新建/更新的 memory_id 列表。"""
        result = await self.extract_fn(text, source_type)
        items = result.get("items", []) if isinstance(result, dict) else []
        written: list[str] = []
        for item in items:
            # 文档/知识导入统一按外部知识入库，避免被误判为
            # session_fact/skill 而丢弃（参见 project_tech_stack 记忆）
            if source_type == "knowledge":
                item["attribution"] = "imported"
            mid = await self._route(item, source_type)
            if mid:
                written.append(mid)
        return written

    async def distill_preview(self, text: str, source_type: str = "memory") -> list[dict]:
        """预览模式：只提炼 + 归属判定，不写入。返回待确认 items。
        仅返回会写入记忆的条目（skill/soul_feedback/session_fact 仍按原路径处理）。"""
        result = await self.extract_fn(text, source_type)
        items = result.get("items", []) if isinstance(result, dict) else []
        preview: list[dict] = []
        for item in items:
            if source_type == "knowledge":
                item["attribution"] = "imported"
            attribution = item.get("attribution", "verified")
            if attribution in ("skill", "soul_feedback", "session_fact"):
                # 非记忆归属：照常走旁路（技能草稿/SOUL 待确认），不进预览清单
                await self._route(item, source_type)
                continue
            preview.append(item)
        return preview

    async def write_item(self, item: dict, source_type: str = "knowledge") -> str | None:
        """预览确认后写入单条（归属已判定，直接走记忆写入路径）。"""
        attribution = item.get("attribution", "imported")
        return await self._write_memory(item, attribution, source_type)

    async def _route(self, item: dict[str, Any], source_type: str) -> str | None:
        attribution = item.get("attribution", "verified")
        if attribution == "skill":
            if self.skill_draft_fn:
                await self.skill_draft_fn(item)
            return None
        if attribution == "soul_feedback":
            if self.soul_feedback_fn:
                await self.soul_feedback_fn(item)
            return None
        if attribution == "session_fact":
            return None  # 只留在会话摘要，不入 L3
        # verified / inferred / imported → 写记忆
        return await self._write_memory(item, attribution, source_type)

    async def _write_memory(self, item: dict, attribution: str, source_type: str) -> str | None:
        title = item.get("title", "")[:30]
        summary = item.get("summary", "")[:30]
        entities, entity_types = normalize_entities(item.get("entities", []))
        item["entities"] = entities
        confidence = item.get("confidence") or ATTRIBUTION_CONFIDENCE.get(
            attribution, "medium")
        stype = "knowledge" if attribution == "imported" else source_type

        # 取向量（同步，不入队列）
        embedding = None
        try:
            embedding = (await self.embed_fn([f"{title} {summary}"]))[0]
        except Exception:  # noqa: BLE001
            logger.info("Distiller 取向量失败，降级 BM25 去重")

        # 跨时间合并判定（BM25 降级时切换双区间阈值 0.75/0.5）
        if embedding is not None:
            merge_thr = self.config.get("dedup_merge_threshold", 0.85)
            link_thr = self.config.get("dedup_link_threshold", 0.6)
        else:
            merge_thr, link_thr = BM25_MERGE_THRESHOLD, BM25_LINK_THRESHOLD
        best_id, best_score = self._find_best_candidate(
            embedding, title, summary)

        if best_id and best_score >= merge_thr:
            # 合并前先由 LLM 判定关系：相同/演变/矛盾/相关（防止高相似矛盾被静默合并，
            # 也防止同主题不同侧面的互补记忆被误判矛盾或被静默合并）
            relation = await self._judge_relation(item, best_id)
            if relation == "contradicts":
                mid = await self._create(item, confidence, stype, embedding,
                                         entities, entity_types, wait=True)
                try:
                    await self.conflict.mark_conflict(mid, best_id, title)
                except Exception:  # noqa: BLE001
                    logger.warning("矛盾标记失败：%s vs %s", mid, best_id,
                                   exc_info=True)
                return mid
            if relation == "evolved":
                return await self._merge_evolved(best_id, item)
            if relation == "related":
                # 同主题不同侧面：各自独立成条，建 related 引用
                mid = await self._create(item, confidence, stype, embedding,
                                         entities, entity_types)
                await self.linker.add_link(mid, best_id, "related")
                return mid
            return await self._merge_into(best_id, item, confidence)

        # 新建
        mid = await self._create(item, confidence, stype, embedding,
                                 entities, entity_types)
        if best_id and link_thr <= best_score < merge_thr:
            await self.linker.add_link(mid, best_id, "related")
        return mid

    async def _judge_relation(self, item: dict, target_id: str) -> str:
        """LLM 判定新信息与已有记忆的关系；LLM 不可用时回退 same（维持旧行为）。"""
        if not self.merge_judge_fn:
            return "same"
        row = self.palace.get(target_id)
        if not row:
            return "same"
        existing = {"title": row["title"], "summary": row["summary"] or "",
                    "detail": self._fetch_detail(target_id)[:300]}
        try:
            data = await self.merge_judge_fn(
                {"title": item.get("title", ""), "summary": item.get("summary", ""),
                 "detail": item.get("detail", "")[:300]}, existing)
            relation = (data or {}).get("relation", "same")
            return relation if relation in (
                "same", "evolved", "contradicts", "related") else "same"
        except Exception:  # noqa: BLE001
            logger.info("合并关系判定失败，回退 same")
            return "same"

    def _fetch_detail(self, memory_id: str) -> str:
        """从 FTS 索引取记忆 detail 正文（避免读盘解析 md）。"""
        try:
            row = self.db.query_one(
                "SELECT detail FROM memories_fts WHERE memory_id=?", (memory_id,))
            return (row["detail"] or "") if row else ""
        except Exception:  # noqa: BLE001
            return ""

    def _find_best_candidate(self, embedding, title, summary) -> tuple[str | None, float]:
        if embedding is not None and self.vs.loaded and self.vs.dim:
            cands = self.vs.top_similar(embedding, n=20)
            if cands:
                return cands[0]
        # BM25 降级去重（阈值语义不同，调用方用 0.75 判断）
        q = f"{title} {summary}".strip()
        import re
        tokens = re.findall(r"[\w\u4e00-\u9fff]+", q)
        if not tokens:
            return None, 0.0
        match = " OR ".join(f'"{t}"' for t in tokens[:20])
        try:
            rows = self.db.query_all(
                "SELECT memory_id, -bm25(memories_fts) s FROM memories_fts "
                "WHERE memories_fts MATCH ? ORDER BY s DESC LIMIT 1", (match,))
        except Exception:  # noqa: BLE001
            return None, 0.0
        if rows:
            # 归一化到 0-1 粗略映射（BM25 降级区间：合并 0.75）
            return rows[0]["memory_id"], min(1.0, rows[0]["s"] / 10.0)
        return None, 0.0

    async def _create(self, item, confidence, source_type, embedding, entities,
                      entity_types: dict | None = None, wait: bool = False) -> str:
        seq = self.palace.next_memory_seq()
        from .naming import memory_id as mk_mid, normalize_domain
        mid = mk_mid(seq)
        now = now_cst()
        fm = {
            "id": mid, "title": item.get("title", "")[:30],
            # 源头净化：LLM 蒸馏可能产出含反斜杠/多段式的脏 domain（v3 修复）
            "domain": normalize_domain(item.get("domain", "general")),
            "confidence": confidence,
            "lifecycle": "active", "source_type": source_type,
            "access_count": 0, "created_at": now.strftime("%Y-%m-%d"),
            "updated_at": now.strftime("%Y-%m-%d"),
            "links": [], "entities": entities,
            "created_by": "distiller",
            "dedup_pending": embedding is None,
        }
        await self.fw.submit("memory", {
            "op": "create", "frontmatter": fm,
            "summary": item.get("summary", "")[:30], "detail": item.get("detail", ""),
            "change_log": f"[{now:%Y-%m-%d}] 首次创建，来源：{item.get('reason', '提炼')}",
            "embedding": embedding, "entities": entities,
            "entity_types": entity_types or {},
            "reason": item.get("reason", ""),
            # 文档/URL 导入（source_type=knowledge ⇔ attribution=imported）
            # 时间线记为导入事件，其余走默认 created
            "timeline_event": "imported" if source_type == "knowledge" else None,
        }, wait=wait)
        return mid

    async def _merge_evolved(self, target_id: str, item: dict) -> str:
        """观点演变 → 合并到同一 md，详情段分层保留新旧观点，不升 confidence。"""
        from pathlib import Path
        from .md_file import parse_memory_md
        row = self.palace.get(target_id)
        if not row:
            return await self._create(item, item.get("confidence", "medium"),
                                      "memory", None, item.get("entities", []))
        f = Path(self.fw.data_dir) / row["md_path"]
        doc = await asyncio.to_thread(
            lambda: parse_memory_md(f.read_text(encoding="utf-8")))
        now = now_cst()
        old_detail = doc.detail.strip()
        # 已分层过的旧详情不重复包裹“当前观点”头，只降级为历史段
        # （### 为当前写入格式；## 兄容早期数据，当时与结构段标题同级）
        for head in ("### 当前观点", "## 当前观点"):
            if old_detail.startswith(head):
                old_detail = old_detail.replace(
                    head, f"### 历史观点 [{row['updated_at'] or ''}]", 1)
                break
        else:
            old_detail = f"### 历史观点 [{row['updated_at'] or ''}]\n{old_detail}"
        doc.detail = (f"### 当前观点 [{now:%Y-%m-%d} 起]\n"
                      f"{item.get('detail', '').strip()}\n\n{old_detail}")
        doc.frontmatter["updated_at"] = now.strftime("%Y-%m-%d")
        new_summary = item.get("summary", "")[:30] or doc.summary
        doc.change_history.insert(
            0, f"[{now:%Y-%m-%d}] 观点演变：{item.get('summary', '')[:30]}")
        await self.fw.submit("memory", {
            "op": "update", "memory_id": target_id, "frontmatter": doc.frontmatter,
            "summary": new_summary, "detail": doc.detail,
            "change_history": doc.change_history, "links": doc.links,
            "entities": doc.entities, "reason": "观点演变分层保留",
            "timeline_event": "evolved"})
        return target_id

    async def _merge_into(self, target_id: str, item: dict, confidence: str) -> str:
        """完全相同 → 合并到已有 md；首次合并不升 confidence，第 2 次起才升。"""
        from .md_file import parse_memory_md
        from pathlib import Path
        row = self.palace.get(target_id)
        if not row:
            return await self._create(item, confidence, "memory", None, item.get("entities", []))
        f = Path(self.fw.data_dir) / row["md_path"]
        doc = await asyncio.to_thread(
            lambda: parse_memory_md(f.read_text(encoding="utf-8")))
        now = now_cst()
        merge_count = sum(1 for h in doc.change_history if "跨时间合并" in h)
        # 第 2 次起升级 confidence（medium→strong）
        new_conf = row["confidence"]
        if merge_count >= 1 and row["confidence"] == "medium":
            new_conf = "strong"
        doc.frontmatter["confidence"] = new_conf
        doc.change_history.insert(
            0, f"[{now:%Y-%m-%d}] 跨时间合并：{item.get('summary', '')[:30]}")
        await self.fw.submit("memory", {
            "op": "update", "memory_id": target_id, "frontmatter": doc.frontmatter,
            "summary": doc.summary, "detail": doc.detail,
            "change_history": doc.change_history, "links": doc.links,
            "entities": doc.entities, "reason": "跨时间合并",
            "timeline_event": "merged"})
        return target_id

    # ---- 回溯去重：向量补齐后对 dedup_pending 记忆重跑去重判定 -------------
    _LIVE = ("active", "stable", "stale")

    async def rededup_memory(self, mid: str) -> str | None:
        """对一条向量刚补齐的 dedup_pending 记忆重跑去重（开发文档 §6.7 缺口修复）。

        提炼当刻若 Embedding 不可用会降级 BM25 且置 dedup_pending=true；向量由补偿
        协程后补后，此处按与提炼一致的阈值语义重判：
          - 与某条相似度 ≥ 合并阈值 → 合并为一条并删除较新的重复，返回幸存者 id
          - 落在关联区间 → 建 related 引用并清除标记
          - 否则 → 仅清除 dedup_pending 标记
        以“id 更小者（创建更早）”为幸存目标，保证不同处理顺序下最终收敛到同一条。
        """
        if mid in self._rededup_deleted:
            return None
        row = self.palace.get(mid)
        if not row or row["lifecycle"] not in self._LIVE:
            return None
        vrow = self.db.query_one(
            "SELECT embedding FROM vectors WHERE memory_id=? AND vector_status='ready'",
            (mid,))
        if not vrow or not vrow["embedding"]:
            return None  # 向量尚未就绪，保持 pending，待补偿后再触发
        from .vector_store import deserialize_vector
        vec = deserialize_vector(vrow["embedding"])
        merge_thr = self.config.get("dedup_merge_threshold", 0.85)
        link_thr = self.config.get("dedup_link_threshold", 0.6)

        best_id, best_score = None, 0.0
        for cid, score in self.vs.top_similar(vec, n=20):
            if cid == mid or cid in self._rededup_deleted:
                continue
            crow = self.palace.get(cid)
            if not crow or crow["lifecycle"] not in self._LIVE:
                continue
            if score > best_score:
                best_id, best_score = cid, score

        if best_id and best_score >= merge_thr:
            # 幸存者取 id 更小者（更早创建），较新的一条并入后删除
            survivor, dup = (best_id, mid) if best_id < mid else (mid, best_id)
            await self._merge_existing(dup, survivor)
            await self._clear_dedup_pending(survivor)
            return survivor
        if best_id and best_score >= link_thr:
            await self.linker.add_link(mid, best_id, "related")
        await self._clear_dedup_pending(mid)
        return None

    async def rededup_all(self) -> dict:
        """离线全量回溯去重：扫描所有 dedup_pending 且向量就绪的记忆逐条重判。"""
        rows = self.db.query_all(
            "SELECT m.id FROM memories m JOIN vectors v ON v.memory_id=m.id "
            "WHERE m.dedup_pending=1 AND v.vector_status='ready' "
            "AND m.lifecycle IN ('active','stable','stale') ORDER BY m.id")
        scanned = len(rows)
        for r in rows:
            await self.rededup_memory(r["id"])
        merged = len(self._rededup_deleted)
        self._rededup_deleted.clear()
        return {"scanned": scanned, "merged": merged}

    async def _merge_existing(self, dup_id: str, survivor_id: str) -> None:
        """把 dup_id 合并进 survivor_id：更新幸存者变更历史/置信度/活跃度 →
        迁移出链 → 删除重复条目。"""
        from pathlib import Path
        from .md_file import parse_memory_md
        surv = self.palace.get(survivor_id)
        dup = self.palace.get(dup_id)
        if not surv or not dup:
            return
        sf = Path(self.fw.data_dir) / surv["md_path"]
        if not sf.exists():
            return
        sdoc = await asyncio.to_thread(
            lambda: parse_memory_md(sf.read_text(encoding="utf-8")))
        now = now_cst()
        merge_count = sum(1 for h in sdoc.change_history if "合并" in h)
        if merge_count >= 1 and surv["confidence"] == "medium":
            sdoc.frontmatter["confidence"] = "strong"
        sdoc.frontmatter["lifecycle"] = "active"       # 被重复强化，恢复活跃
        sdoc.frontmatter["dedup_pending"] = False
        sdoc.change_history.insert(
            0, f"[{now:%Y-%m-%d}] 回溯合并重复记忆：{dup['title']}")
        # 迁移 dup 的出链到幸存者（跳过指向幸存者/自身/已存在的）
        df = Path(self.fw.data_dir) / dup["md_path"]
        if df.exists():
            ddoc = await asyncio.to_thread(
                lambda: parse_memory_md(df.read_text(encoding="utf-8")))
            existing = {l.get("target") for l in sdoc.links}
            for l in (ddoc.links or []):
                t = l.get("target")
                if t and t not in (survivor_id, dup_id) and t not in existing:
                    sdoc.links.append(
                        {"target": t, "type": l.get("type", "related")})
                    existing.add(t)
        sdoc.frontmatter["links"] = sdoc.links
        await self.fw.submit("memory", {
            "op": "update", "memory_id": survivor_id, "frontmatter": sdoc.frontmatter,
            "summary": sdoc.summary, "detail": sdoc.detail,
            "change_history": sdoc.change_history, "links": sdoc.links,
            "entities": sdoc.entities, "reason": f"回溯合并重复 {dup_id}",
            "timeline_event": "merged"})
        await self.fw.submit("memory", {"op": "delete", "memory_id": dup_id})
        self._rededup_deleted.add(dup_id)

    async def _clear_dedup_pending(self, mid: str) -> None:
        """清除 dedup_pending 标记（未找到重复或已完成合并的幸存者）。"""
        from pathlib import Path
        from .md_file import parse_memory_md
        row = self.palace.get(mid)
        if not row:
            return
        f = Path(self.fw.data_dir) / row["md_path"]
        if not f.exists():
            return
        doc = await asyncio.to_thread(
            lambda: parse_memory_md(f.read_text(encoding="utf-8")))
        if doc.frontmatter.get("dedup_pending") is False:
            return  # 已清除，避免无谓写入触发 watcher
        doc.frontmatter["dedup_pending"] = False
        await self.fw.submit("memory", {
            "op": "update", "memory_id": mid, "frontmatter": doc.frontmatter,
            "summary": doc.summary, "detail": doc.detail,
            "change_history": doc.change_history, "links": doc.links,
            "entities": doc.entities, "reason": "回溯去重：清除 dedup_pending"})
