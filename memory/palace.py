"""
Palace —— 记忆索引仓储层（memories 及其派生索引表的 DAO）。

职责边界：只操作 SQLite 派生索引（memories / memories_fts / memory_links /
memory_entities / memory_entity_links / memory_timeline），不写 md 文件、不调 Embedding。
md 文件与索引的写入编排由 FileWriter 统一负责，Palace 提供索引层的原子操作供其调用。

关键口径（开发文档 §6.16）：
- 记忆总数 = active+stable+stale（archived/missing 单列）
- primary_domain：关联记忆按 domain 分组计数取最多，并列取字典序最小
- 孤立记忆：零入链（无别的记忆指向它，出链不计）
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .naming import entity_id as make_entity_id
from .naming import normalize_entity_name
from infrastructure.timeutil import now_cst

_COUNTABLE = ("active", "stable", "stale")


def _now() -> str:
    return now_cst().isoformat(timespec="seconds")


class Palace:
    def __init__(self, db):  # db: infrastructure.db.Database
        self.db = db

    # ---- 自增 id 分配 -----------------------------------------------------
    def next_memory_seq(self) -> int:
        row = self.db.query_one(
            "SELECT MAX(CAST(SUBSTR(id,5) AS INTEGER)) m"
            " FROM memories WHERE id LIKE 'mem_%'")
        return (row["m"] or 0) + 1

    # ---- memories 索引表 --------------------------------------------------
    def upsert_index(self, conn: sqlite3.Connection, fm: dict[str, Any],
                     summary: str, md_path: str) -> None:
        """在给定事务连接内 upsert memories 索引行（从 frontmatter 派生）。"""
        conn.execute(
            """INSERT INTO memories(id,title,summary,domain,confidence,lifecycle,
                 source_type,access_count,last_accessed,is_important,implicit_use_count,
                 md_missing,user_marked_stale,dedup_pending,created_by,md_path,
                 created_at,updated_at)
               VALUES(:id,:title,:summary,:domain,:confidence,:lifecycle,:source_type,
                 :access_count,:last_accessed,:is_important,:implicit_use_count,
                 :md_missing,:user_marked_stale,:dedup_pending,:created_by,:md_path,
                 :created_at,:updated_at)
               ON CONFLICT(id) DO UPDATE SET
                 title=excluded.title, summary=excluded.summary, domain=excluded.domain,
                 confidence=excluded.confidence, lifecycle=excluded.lifecycle,
                 source_type=excluded.source_type, is_important=excluded.is_important,
                 md_missing=excluded.md_missing, user_marked_stale=excluded.user_marked_stale,
                 dedup_pending=excluded.dedup_pending, md_path=excluded.md_path,
                 updated_at=excluded.updated_at""",
            {
                "id": fm["id"], "title": fm.get("title", ""), "summary": summary,
                "domain": fm.get("domain", "general"),
                "confidence": fm.get("confidence", "medium"),
                "lifecycle": fm.get("lifecycle", "active"),
                "source_type": fm.get("source_type", "memory"),
                "access_count": fm.get("access_count", 0),
                "last_accessed": fm.get("last_accessed"),
                "is_important": 1 if fm.get("is_important") else 0,
                "implicit_use_count": fm.get("implicit_use_count", 0),
                "md_missing": 1 if fm.get("md_missing") else 0,
                "user_marked_stale": 1 if fm.get("user_marked_stale") else 0,
                "dedup_pending": 1 if fm.get("dedup_pending") else 0,
                "created_by": fm.get("created_by", "distiller"),
                "md_path": md_path,
                "created_at": fm.get("created_at", _now()),
                "updated_at": _now(),
            },
        )

    def sync_fts(self, conn: sqlite3.Connection, memory_id: str, title: str,
                 summary: str, detail: str, domain: str) -> None:
        conn.execute(
            "DELETE FROM memories_fts WHERE memory_id=?", (memory_id,))
        conn.execute(
            "INSERT INTO memories_fts(memory_id,title,summary,detail,domain) "
            "VALUES(?,?,?,?,?)", (memory_id, title, summary, detail, domain))

    # ---- 交叉引用 ---------------------------------------------------------
    def replace_links(self, conn: sqlite3.Connection, source_id: str,
                      links: list[dict[str, str]]) -> None:
        conn.execute(
            "DELETE FROM memory_links WHERE source_id=?", (source_id,))
        for lk in links or []:
            # 防御脏数据：历史残留/LLM 产出可能含非 dict 元素，跳过而非崩溃
            if not isinstance(lk, dict):
                continue
            target, ltype = lk.get("target"), lk.get("type", "related")
            if target:
                conn.execute(
                    "INSERT OR IGNORE INTO memory_links(source_id,target_id,link_type) "
                    "VALUES(?,?,?)", (source_id, target, ltype))

    def add_link(self, conn: sqlite3.Connection, source: str, target: str,
                 link_type: str) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO memory_links(source_id,target_id,link_type) "
            "VALUES(?,?,?)", (source, target, link_type))

    def backlinks(self, memory_id: str) -> list[sqlite3.Row]:
        """反向边：所有指向 memory_id 的记忆。"""
        return self.db.query_all(
            "SELECT source_id, link_type FROM memory_links WHERE target_id=?",
            (memory_id,))

    def outlinks(self, memory_id: str) -> list[sqlite3.Row]:
        return self.db.query_all(
            "SELECT target_id, link_type FROM memory_links WHERE source_id=?",
            (memory_id,))

    # ---- 实体 -------------------------------------------------------------
    def sync_entities(self, conn: sqlite3.Connection, memory_id: str,
                      entities: list[str],
                      entity_types: dict[str, str] | None = None) -> None:
        """重建该记忆的实体关联，并重算受影响实体的 memory_count/primary_domain。
        entity_types 提供时同步写入 AI 分类的 entity_type。"""
        old = [r["entity_id"] for r in conn.execute(
            "SELECT entity_id FROM memory_entity_links WHERE memory_id=?", (memory_id,))]
        conn.execute(
            "DELETE FROM memory_entity_links WHERE memory_id=?", (memory_id,))

        affected: set[str] = set(old)
        entity_types = entity_types or {}
        for name in entities:
            if not name or not name.strip():
                continue
            eid = make_entity_id(name)
            affected.add(eid)
            etype = entity_types.get(name)
            exists = conn.execute(
                "SELECT entity_type FROM memory_entities WHERE entity_id=?",
                (eid,)).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO memory_entities(entity_id,entity_name,entity_type,"
                    "first_seen,memory_count,primary_domain) VALUES(?,?,?,?,0,NULL)",
                    (eid, normalize_entity_name(name), etype, _now()))
            elif etype and not exists["entity_type"]:
                # 已有实体无分类时补写，不覆盖已有分类
                conn.execute(
                    "UPDATE memory_entities SET entity_type=? WHERE entity_id=?",
                    (etype, eid))
            conn.execute(
                "INSERT OR IGNORE INTO memory_entity_links(memory_id,entity_id) "
                "VALUES(?,?)", (memory_id, eid))

        for eid in affected:
            self._recount_entity(conn, eid)

    def _recount_entity(self, conn: sqlite3.Connection, eid: str) -> None:
        rows = conn.execute(
            "SELECT m.domain FROM memory_entity_links l JOIN memories m "
            "ON l.memory_id=m.id WHERE l.entity_id=?", (eid,)).fetchall()
        count = len(rows)
        if count == 0:
            conn.execute(
                "DELETE FROM memory_entities WHERE entity_id=?", (eid,))
            return
        # primary_domain：计数最多，并列取字典序最小（保证确定性）
        tally: dict[str, int] = {}
        for r in rows:
            tally[r["domain"]] = tally.get(r["domain"], 0) + 1
        primary = sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        conn.execute(
            "UPDATE memory_entities SET memory_count=?, primary_domain=? WHERE entity_id=?",
            (count, primary, eid))

    # ---- 时间线 -----------------------------------------------------------
    def add_timeline(self, conn: sqlite3.Connection, memory_id: str,
                     event_type: str, detail: str = "") -> None:
        # 去重：同一秒内同一记忆同一事件不重复写入（防 FileWriter 队列双提交）
        now = _now()
        exists = conn.execute(
            "SELECT 1 FROM memory_timeline WHERE memory_id=? AND event_type=? AND event_time=?",
            (memory_id, event_type, now)).fetchone()
        if exists:
            return
        conn.execute(
            "INSERT INTO memory_timeline(memory_id,event_type,detail,event_time) "
            "VALUES(?,?,?,?)", (memory_id, event_type, detail, now))

    # ---- 查询 -------------------------------------------------------------
    def get(self, memory_id: str) -> sqlite3.Row | None:
        return self.db.query_one("SELECT * FROM memories WHERE id=?", (memory_id,))

    def get_many(self, ids: list[str]) -> dict[str, sqlite3.Row]:
        """批量取记忆索引行，返回 {id: row}；避免逐条 get 的 N+1 查询。
        占位符仅由 id 个数生成（非用户输入拼接），无注入风险。"""
        if not ids:
            return {}
        uniq = list(dict.fromkeys(ids))
        ph = ",".join("?" * len(uniq))
        rows = self.db.query_all(
            f"SELECT * FROM memories WHERE id IN ({ph})", tuple(uniq))
        return {r["id"]: r for r in rows}

    def stats(self) -> dict[str, int]:
        rows = self.db.query_all(
            "SELECT lifecycle, count(*) c FROM memories GROUP BY lifecycle")
        by = {r["lifecycle"]: r["c"] for r in rows}
        total = sum(by.get(k, 0) for k in _COUNTABLE)
        important = self.db.query_one(
            "SELECT count(*) c FROM memories WHERE is_important=1")["c"]
        link_count = self.db.query_one(
            "SELECT count(*) c FROM memory_links")["c"]
        return {
            "total": total,
            "total_active": by.get("active", 0),
            "total_stable": by.get("stable", 0),
            "total_stale": by.get("stale", 0),
            "total_archived": by.get("archived", 0),
            "total_missing": by.get("missing", 0),
            "important_count": important,
            "link_count": link_count,
        }

    def orphans(self) -> list[str]:
        """孤立记忆：零连接（无任何出链或入链）。口径由开发文档 §6.16 的
        “零入链”调整为无向：有出链的记忆已接入图谱，不应计入孤立。"""
        rows = self.db.query_all(
            "SELECT id FROM memories m WHERE lifecycle IN ('active','stable','stale') "
            "AND NOT EXISTS (SELECT 1 FROM memory_links l "
            "WHERE l.target_id=m.id OR l.source_id=m.id)")
        return [r["id"] for r in rows]

    def domains(self) -> list[dict[str, Any]]:
        rows = self.db.query_all(
            "SELECT domain, count(*) c FROM memories "
            "WHERE lifecycle IN ('active','stable','stale') "
            "GROUP BY domain ORDER BY c DESC")
        return [{"domain": r["domain"], "count": r["c"]} for r in rows]

    # ---- 物理删除的全部索引清理（同事务） --------------------------------
    def delete_all_indexes(self, conn: sqlite3.Connection, memory_id: str) -> None:
        """删除该记忆在所有索引表中的行；调用方需在同一事务内清理 md 与向量缓存。"""
        # 反向边：从指向它的记忆 frontmatter 移除由 FileWriter 负责，这里删索引边
        conn.execute("DELETE FROM memory_links WHERE source_id=? OR target_id=?",
                     (memory_id, memory_id))
        # 实体关联 + 重算
        affected = [r["entity_id"] for r in conn.execute(
            "SELECT entity_id FROM memory_entity_links WHERE memory_id=?", (memory_id,))]
        conn.execute(
            "DELETE FROM memory_entity_links WHERE memory_id=?", (memory_id,))
        for eid in affected:
            self._recount_entity(conn, eid)
        conn.execute(
            "DELETE FROM memories_fts WHERE memory_id=?", (memory_id,))
        conn.execute("DELETE FROM vectors WHERE memory_id=?", (memory_id,))
        conn.execute(
            "DELETE FROM memory_timeline WHERE memory_id=?", (memory_id,))
        # 引用溯源事件同步清理：否则删除后统计仍计入已删记忆、孤儿行永久膨胀
        conn.execute(
            "DELETE FROM citation_events WHERE memory_id=?", (memory_id,))
        # 清理指向它的 lint 建议（孤立/重复），否则删除后建议卡片会残留为幽灵条目
        conn.execute(
            "DELETE FROM lint_suggestions WHERE primary_memory_id=? OR related_memory_id=?",
            (memory_id, memory_id))
        conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        # raw_docs.extracted_memory_ids 数组移除
        for r in conn.execute(
                "SELECT id, extracted_memory_ids FROM raw_docs "
                "WHERE extracted_memory_ids LIKE ?", (f"%{memory_id}%",)):
            try:
                ids = json.loads(r["extracted_memory_ids"] or "[]")
            except json.JSONDecodeError:
                continue
            if memory_id in ids:
                ids.remove(memory_id)
                conn.execute("UPDATE raw_docs SET extracted_memory_ids=? WHERE id=?",
                             (json.dumps(ids, ensure_ascii=False), r["id"]))
