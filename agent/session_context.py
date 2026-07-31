"""
会话存储与上下文加载（产品文档 §L2/§第 2 步上下文加载/§会话管理 / 开发文档 §6.19）。

- L2 会话记忆：conversations 表存原文，sessions 存元数据
- 上下文加载：CONTEXT_ENTRY 冻结快照 + SOUL 必读注入 + 会话摘要拼装(Head-Summary-Tail)
- 会话恢复：Tail 起点由 last_compressed_message_id 水位决定
- 会话标题：首条消息后异步生成，title_source=manual 时丢弃自动结果
- response_signal 两阶段采集
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from memory.md_file import dump_frontmatter_doc, split_frontmatter
from memory.naming import session_id as make_session_id


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class SessionStore:
    def __init__(self, db, data_dir):
        self.db = db
        self.data_dir = Path(data_dir)

    # ---- 会话 CRUD --------------------------------------------------------
    def create_session(self, channel: str = None) -> str:
        """channel：IM 渠道会话记录来源平台（feishu/telegram 等），Web 端为 None。"""
        rows = self.db.query_all("SELECT session_id FROM sessions")
        seq = len(rows) + 1
        while True:
            sid = make_session_id(seq)
            if not self.db.query_one("SELECT 1 FROM sessions WHERE session_id=?", (sid,)):
                break
            seq += 1
        self.db.execute(
            "INSERT INTO sessions(session_id,title,title_source,last_active,"
            "message_count,channel) VALUES(?,?,'auto',?,0,?)",
            (sid, "新会话", _now(), channel))
        return sid

    def rename(self, sid: str, title: str) -> None:
        self.db.execute(
            "UPDATE sessions SET title=?, title_source='manual' WHERE session_id=?",
            (title[:50], sid))

    def set_auto_title(self, sid: str, title: str) -> None:
        """异步标题生成回填；若已 manual 则丢弃。"""
        row = self.db.query_one(
            "SELECT title_source FROM sessions WHERE session_id=?", (sid,))
        if row and row["title_source"] == "manual":
            return
        self.db.execute(
            "UPDATE sessions SET title=? WHERE session_id=?", (title[:50], sid))

    def list_sessions(self, keyword: str = None, page: int = 1,
                      page_size: int = 20) -> dict:
        if keyword:
            ids = [r["session_id"] for r in self.db.query_all(
                "SELECT DISTINCT session_id FROM conversations c "
                "JOIN conversations_fts f ON c.id=f.rowid WHERE conversations_fts MATCH ?",
                (self._fts(keyword),))]
            if not ids:
                return {"total": 0, "list": []}
            ph = ",".join("?" * len(ids))
            rows = self.db.query_all(
                f"SELECT * FROM sessions WHERE session_id IN ({ph}) "
                f"ORDER BY pinned DESC, last_active DESC", ids)
        else:
            rows = self.db.query_all(
                "SELECT * FROM sessions ORDER BY pinned DESC, last_active DESC")
        total = len(rows)
        start = (page - 1) * page_size
        page_rows = rows[start:start + page_size]
        return {"total": total, "list": [{
            "session_id": r["session_id"], "title": r["title"],
            "last_active": r["last_active"], "message_count": r["message_count"],
            "compressed": bool(r["compressed_summary_path"]),
            "pinned": bool(r["pinned"]),
            "channel": r["channel"],
            "title_source": r["title_source"]} for r in page_rows]}

    def set_pinned(self, sid: str, pinned: bool) -> None:
        self.db.execute(
            "UPDATE sessions SET pinned=?, pinned_at=? WHERE session_id=?",
            (1 if pinned else 0, _now() if pinned else None, sid))

    def delete_session(self, sid: str) -> None:
        self._cleanup_images(
            "SELECT images FROM conversations WHERE session_id=? AND images IS NOT NULL",
            (sid,))
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM conversations WHERE session_id=?", (sid,))
            conn.execute("DELETE FROM sessions WHERE session_id=?", (sid,))
            conn.execute(
                "DELETE FROM platform_sessions WHERE session_id=?", (sid,))
        summary = self.data_dir / "sessions" / f"{sid}.md"
        if summary.exists():
            summary.unlink()

    # ---- 消息 -------------------------------------------------------------
    def append_message(self, sid: str, role: str, content: str,
                       message_type: str = "normal", citations: list | None = None,
                       notification_type: str = None,
                       thinking: str | None = None,
                       images: list[str] | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO conversations(session_id,role,message_type,notification_type,"
            "content,citations,feedback,create_time,thinking,images) "
            "VALUES(?,?,?,?,?,?,0,?,?,?)",
            (sid, role, message_type, notification_type, content,
             json.dumps(citations, ensure_ascii=False) if citations else None,
             _now(), thinking,
             json.dumps(images) if images else None))
        self.db.execute(
            "UPDATE sessions SET last_active=?, message_count=message_count+1 "
            "WHERE session_id=?", (_now(), sid))
        return cur.lastrowid

    def get_messages(self, sid: str, before_id: int = None, limit: int = 50) -> list[dict]:
        if before_id:
            rows = self.db.query_all(
                "SELECT * FROM conversations WHERE session_id=? AND id<? "
                "ORDER BY id DESC LIMIT ?", (sid, before_id, limit))
        else:
            rows = self.db.query_all(
                "SELECT * FROM conversations WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (sid, limit))
        rows = list(reversed(rows))
        out = []
        for r in rows:
            cites = json.loads(r["citations"]) if r["citations"] else []
            # 历史引用只存 id：补充记忆标题，前端可直接展示/点击查详情
            for cit in cites:
                if cit.get("id") and not cit.get("title"):
                    mrow = self.db.query_one(
                        "SELECT title FROM memories WHERE id=?", (cit["id"],))
                    if mrow:
                        cit["title"] = mrow["title"]
            out.append({"id": r["id"], "role": r["role"],
                        "message_type": r["message_type"],
                        "content": r["content"], "citations": cites,
                        "feedback": r["feedback"], "create_time": r["create_time"],
                        "thinking": r["thinking"],
                        # 持久化图片：文件名转为可访问 URL，刷新后历史消息可回看
                        "images": [f"/chat-images/{f}" for f in
                                   json.loads(r["images"])] if r["images"] else []})
        return out

    def set_feedback(self, message_id: int, feedback: int) -> None:
        self.db.execute("UPDATE conversations SET feedback=? WHERE id=?",
                        (feedback, message_id))

    def delete_turn(self, sid: str, assistant_message_id: int) -> int:
        """重新生成前清理：删除指定 assistant 回复及其紧邻的上一条用户消息。

        返回实际删除的消息条数（0 表示消息不存在或不属于该会话）。
        conversations_fts 由 AFTER DELETE 触发器自动同步。
        """
        arow = self.db.query_one(
            "SELECT id FROM conversations WHERE id=? AND session_id=? "
            "AND role='assistant'", (assistant_message_id, sid))
        if not arow:
            return 0
        urow = self.db.query_one(
            "SELECT id FROM conversations WHERE session_id=? AND role='user' "
            "AND id<? ORDER BY id DESC LIMIT 1", (sid, assistant_message_id))
        ids = [assistant_message_id] + ([urow["id"]] if urow else [])
        ph = ",".join("?" * len(ids))
        self._cleanup_images(
            f"SELECT images FROM conversations WHERE id IN ({ph}) "
            "AND images IS NOT NULL", tuple(ids))
        self.db.execute(
            f"DELETE FROM conversations WHERE id IN ({ph})", tuple(ids))
        self.db.execute(
            f"DELETE FROM response_signals WHERE message_id IN ({ph})", tuple(ids))
        self.db.execute(
            "UPDATE sessions SET message_count=MAX(message_count-?,0) "
            "WHERE session_id=?", (len(ids), sid))
        return len(ids)

    def latest_active_session(self) -> str | None:
        row = self.db.query_one(
            "SELECT session_id FROM sessions ORDER BY last_active DESC LIMIT 1")
        return row["session_id"] if row else None

    def _cleanup_images(self, sql: str, params: tuple) -> None:
        """删除消息前同步清理其持久化图片文件（失败不阻断删除流程）。"""
        try:
            for r in self.db.query_all(sql, params):
                for fname in json.loads(r["images"]):
                    p = self.data_dir / "chat_images" / fname
                    if p.exists():
                        p.unlink()
        except Exception:  # noqa: BLE001
            pass

    # ---- 会话恢复（Head-Summary-Tail） -----------------------------------
    def load_recovery_context(self, sid: str, buffer_rounds: int = 20,
                              head_protected: int = 3) -> list[dict]:
        """返回消息列表（含 id 字段供压缩水位推进，送 LLM 前需剔除）。

        Head：会话最初 head_protected 条（存在压缩摘要时才拼入，保护背景设定）；
        Summary：压缩摘要（有则拼入）；Tail：水位之后的最近 buffer_rounds 轮原文。
        """
        row = self.db.query_one(
            "SELECT compressed_summary_path,last_compressed_message_id FROM sessions "
            "WHERE session_id=?", (sid,))
        if not row:
            return []
        summary_text = ""
        if row["compressed_summary_path"]:
            p = self.data_dir / row["compressed_summary_path"]
            if p.exists():
                _, summary_text = split_frontmatter(
                    p.read_text(encoding="utf-8"))
        watermark = row["last_compressed_message_id"]
        # Tail：取最近若干轮（DESC 取最新后反转，避免长会话丢最新消息）；
        # 系统通知不属于对话内容，不进 context
        if watermark:
            tail_rows = self.db.query_all(
                "SELECT id,role,content FROM (SELECT id,role,content FROM conversations "
                "WHERE session_id=? AND id>? AND message_type='normal' "
                "ORDER BY id DESC LIMIT ?) ORDER BY id",
                (sid, watermark, buffer_rounds * 2))
        else:
            tail_rows = self.db.query_all(
                "SELECT id,role,content FROM (SELECT id,role,content FROM conversations "
                "WHERE session_id=? AND message_type='normal' "
                "ORDER BY id DESC LIMIT ?) ORDER BY id",
                (sid, buffer_rounds * 2))
        msgs = []
        if summary_text:
            # Protected Head：已被压缩覆盖的最初几条原文保留不动（水位之前）
            head_rows = self.db.query_all(
                "SELECT id,role,content FROM conversations WHERE session_id=? "
                "AND id<=? AND message_type='normal' ORDER BY id LIMIT ?",
                (sid, watermark or 0, head_protected)) if watermark else []
            for r in head_rows:
                if r["role"] in ("user", "assistant"):
                    msgs.append({"role": r["role"], "content": r["content"],
                                 "id": r["id"]})
            msgs.append({"role": "system",
                         "content": f"[CONTEXT COMPACTION] 会话历史摘要：\n{summary_text}"})
        for r in tail_rows:
            if r["role"] in ("user", "assistant"):
                msgs.append({"role": r["role"], "content": r["content"],
                             "id": r["id"]})
        return msgs

    def save_summary(self, sid: str, summary_body: str, last_msg_id: int) -> None:
        """压缩摘要落盘：frontmatter（含水位，供索引丢失时从 md 重建）+ 五段正文。"""
        sdir = self.data_dir / "sessions"
        sdir.mkdir(parents=True, exist_ok=True)
        rel = f"sessions/{sid}.md"
        srow = self.db.query_one(
            "SELECT title, message_count, last_active FROM sessions WHERE session_id=?",
            (sid,))
        fm = {"session_id": sid, "title": (srow["title"] if srow else "") or "",
              "message_count": srow["message_count"] if srow else 0,
              "compressed": True, "compressed_at": _now(),
              "last_compressed_message_id": last_msg_id,
              "compression_failed": False}
        (self.data_dir / rel).write_text(
            dump_frontmatter_doc(fm, summary_body), encoding="utf-8")
        self.db.execute(
            "UPDATE sessions SET compressed_summary_path=?, last_compressed_message_id=? "
            "WHERE session_id=?", (rel, last_msg_id, sid))

    def mark_compression_failed(self, sid: str) -> None:
        """压缩失败：摘要 md 的 frontmatter 记 compression_failed: true，下次触发重试。"""
        sdir = self.data_dir / "sessions"
        sdir.mkdir(parents=True, exist_ok=True)
        p = sdir / f"{sid}.md"
        if p.exists():
            fm, body = split_frontmatter(p.read_text(encoding="utf-8"))
        else:
            fm, body = {"session_id": sid, "compressed": False}, ""
        fm["compression_failed"] = True
        p.write_text(dump_frontmatter_doc(fm, body), encoding="utf-8")

    @staticmethod
    def _fts(keyword: str) -> str:
        tokens = re.findall(r"[\w\u4e00-\u9fff]+", keyword)
        return " OR ".join(f'"{t}"' for t in tokens[:10]) or '""'
