"""
会话存储与上下文加载（产品文档 §L2/§第 2 步上下文加载/§会话管理 / 开发文档 §6.19）。

- L2 会话记忆：conversations 表存原文，sessions 存元数据
- 上下文加载：CONTEXT_ENTRY 冻结快照 + SOUL 必读注入 + 会话摘要拼装(Head-Summary-Tail)
- 会话恢复：Tail 起点由 last_compressed_message_id 水位决定
- 会话标题：首条消息后异步生成，title_source=manual 时丢弃自动结果
- response_signal 两阶段采集
- 会话继承关系与 handoff 摘要管理（会话上下文管理方案 v2）
- readonly 会话写入保护
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from memory.md_file import dump_frontmatter_doc, split_frontmatter
from memory.naming import session_id as make_session_id
from agent.context_signals import detect_fake_claim, detect_proposal_sentence
from infrastructure.prompt_loader import PROMPTS
from infrastructure.timeutil import now_cst

_UNSET = object()


def _now() -> str:
    return now_cst().isoformat(timespec="seconds")


class SessionStore:
    def __init__(self, db, data_dir):
        self.db = db
        self.data_dir = Path(data_dir)

    # ---- 会话 CRUD --------------------------------------------------------
    def create_session(self, channel: str = None, from_session: str = None) -> str:
        """channel：IM 渠道会话记录来源平台（feishu/telegram 等），Web 端为 None。
        from_session：handoff 前驱会话 ID（null 表示无前驱）。"""
        row = self.db.query_one(
            "SELECT MAX(CAST(SUBSTR(session_id,6) AS INTEGER)) m"
            " FROM sessions WHERE session_id LIKE 'sess_%'")
        seq = (row["m"] or 0) + 1
        while True:
            sid = make_session_id(seq)
            if not self.db.query_one("SELECT 1 FROM sessions WHERE session_id=?", (sid,)):
                break
            seq += 1
        now = _now()
        self.db.execute(
            "INSERT INTO sessions(session_id,title,title_source,last_active,"
            "message_count,channel,from_session,created_at) VALUES(?,?,'auto',?,0,?,?,?)",
            (sid, "新对话", now, channel, from_session, now))
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
            "title_source": r["title_source"],
            "readonly": bool(r["readonly"]),
            "from_session": r["from_session"],
            "handoff_status": _handoff_status_from_row(r),
            "succeeded_by": r["succeeded_by"],
        } for r in page_rows]}

    def set_pinned(self, sid: str, pinned: bool) -> None:
        self.db.execute(
            "UPDATE sessions SET pinned=?, pinned_at=? WHERE session_id=?",
            (1 if pinned else 0, _now() if pinned else None, sid))

    def delete_session(self, sid: str) -> None:
        self._cleanup_images(
            "SELECT images FROM conversations WHERE session_id=? AND images IS NOT NULL",
            (sid,))
        with self.db.transaction() as conn:
            # SQLite 外键在本项目由应用层维护；长文交付任务必须随会话删除，
            # 否则章节正文和问题模型会成为不可访问的孤立数据。
            conn.execute(
                "DELETE FROM delivery_sections WHERE job_id IN "
                "(SELECT id FROM delivery_jobs WHERE session_id=?)", (sid,))
            conn.execute(
                "DELETE FROM delivery_jobs WHERE session_id=?", (sid,))
            conn.execute(
                "DELETE FROM conversations WHERE session_id=?", (sid,))
            conn.execute("DELETE FROM sessions WHERE session_id=?", (sid,))
            conn.execute(
                "DELETE FROM platform_sessions WHERE session_id=?", (sid,))
            # 关联数据一并清理，避免残留：引用事件 / 回顾候选 / token 用量
            conn.execute(
                "DELETE FROM citation_events WHERE session_id=?", (sid,))
            conn.execute(
                "DELETE FROM review_candidates WHERE session_id=?", (sid,))
            conn.execute(
                "DELETE FROM memory_write_candidates WHERE session_id=? "
                "AND status IN ('pending','rejected','expired')", (sid,))
            conn.execute(
                "DELETE FROM token_usage WHERE session_id=?", (sid,))
        summary = self.data_dir / "sessions" / f"{sid}.md"
        if summary.exists():
            summary.unlink()

    def append_message(self, sid: str, role: str, content: str,
                       message_type: str = "normal", citations: list | None = None,
                       notification_type: str = None,
                       thinking: str | None = None,
                       images: list[str] | None = None,
                        visuals: list | None = None,
                        strategy_snapshot: dict | None = None,
                        skeleton_snapshot: dict | None = None,
                        analysis_metadata: dict | None = None,
                        next_step_shown: dict | None = None,
                       parent_id: int | None = _UNSET,
                       version_group_id: int | None = None,
                       is_active: int = 1) -> int:
        # readonly 写入保护（会话上下文管理方案 v2 §旧会话状态管理）
        row = self.db.query_one(
            "SELECT readonly FROM sessions WHERE session_id=?", (sid,))
        if row and row["readonly"]:
            raise ValueError("会话已结束，不可写入新消息")
        # parent_id 未指定时自动推断：指向同 session 最新一条消息
        if parent_id is _UNSET:
            prev = self.db.query_one(
                "SELECT id FROM conversations WHERE session_id=? "
                "ORDER BY id DESC LIMIT 1", (sid,))
            parent_id = prev["id"] if prev else None
        cur = self.db.execute(
            "INSERT INTO conversations(session_id,role,message_type,notification_type,"
            "content,citations,feedback,create_time,thinking,images,visuals,"
            "response_strategy_json,cognitive_skeleton_json,analysis_metadata_json,"
            "protected_from_compression,next_step_shown,parent_id,version_group_id,is_active) "
            "VALUES(?,?,?,?,?,?,0,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, role, message_type, notification_type, content,
             json.dumps(citations, ensure_ascii=False) if citations else None,
             _now(), thinking,
             json.dumps(images) if images else None,
             json.dumps(visuals, ensure_ascii=False) if visuals else None,
             json.dumps(strategy_snapshot,
                        ensure_ascii=False) if strategy_snapshot else None,
              json.dumps(skeleton_snapshot,
                         ensure_ascii=False) if skeleton_snapshot else None,
              json.dumps(analysis_metadata,
                         ensure_ascii=False) if analysis_metadata else None,
              int(self._should_protect(role, content)),
             json.dumps(next_step_shown,
                        ensure_ascii=False) if next_step_shown else None,
             parent_id, version_group_id, is_active)
        )
        msg_id = cur.lastrowid
        # version_group_id 未指定时默认为自身 id（首次创建，无兄弟版本）
        if version_group_id is None:
            self.db.execute(
                "UPDATE conversations SET version_group_id=? WHERE id=?",
                (msg_id, msg_id))
        self.db.execute(
            "UPDATE sessions SET last_active=?, message_count=message_count+1 "
            "WHERE session_id=?", (_now(), sid))
        return msg_id

    def get_messages(self, sid: str, before_id: int = None, limit: int = 50) -> list[dict]:
        # 仅加载活跃分支（is_active=1 或 NULL 兼容未迁移数据）
        if before_id:
            rows = self.db.query_all(
                "SELECT * FROM conversations WHERE session_id=? AND id<? "
                "AND (is_active=1 OR is_active IS NULL) "
                "ORDER BY id DESC LIMIT ?", (sid, before_id, limit))
        else:
            rows = self.db.query_all(
                "SELECT * FROM conversations WHERE session_id=? "
                "AND (is_active=1 OR is_active IS NULL) "
                "ORDER BY id DESC LIMIT ?",
                (sid, limit))
        rows = list(reversed(rows))
        out = []
        for r in rows:
            cites = json.loads(r["citations"]) if r["citations"] else []
            for cit in cites:
                if cit.get("id") and not cit.get("title"):
                    mrow = self.db.query_one(
                        "SELECT title FROM memories WHERE id=?", (cit["id"],))
                    if mrow:
                        cit["title"] = mrow["title"]
            # 版本信息：同组兄弟数量和当前索引
            vgroup = r["version_group_id"] or r["id"]
            siblings = self.db.query_all(
                "SELECT id FROM conversations WHERE version_group_id=? ORDER BY id",
                (vgroup,))
            sibling_ids = [s["id"] for s in siblings]
            sibling_count = len(sibling_ids)
            sibling_index = sibling_ids.index(
                r["id"]) if r["id"] in sibling_ids else 0
            out.append({"id": r["id"], "role": r["role"],
                        "message_type": r["message_type"],
                        "notification_type": r["notification_type"],
                        "content": r["content"], "citations": cites,
                         "feedback": r["feedback"], "create_time": r["create_time"],
                         "thinking": r["thinking"],
                         "analysis_metadata": (json.loads(r["analysis_metadata_json"])
                                               if r["analysis_metadata_json"] else None),
                        "images": [f"/chat-images/{f}" for f in
                                   json.loads(r["images"])] if r["images"] else [],
                        "visuals": json.loads(r["visuals"]) if r["visuals"] else [],
                        "parent_id": r["parent_id"],
                        "version_group_id": vgroup,
                        "sibling_count": sibling_count,
                        "sibling_index": sibling_index,
                        "has_branches": sibling_count > 1})
        return out

    def switch_version(self, sid: str, version_group_id: int,
                       target_message_id: int) -> None:
        """切换版本：同组全部 is_active=0，目标 is_active=1，
        并递归激活目标下游最新的活跃子节点链。"""
        # 同组全部停用
        self.db.execute(
            "UPDATE conversations SET is_active=0 "
            "WHERE version_group_id=? AND session_id=?",
            (version_group_id, sid))
        # 目标激活
        self.db.execute(
            "UPDATE conversations SET is_active=1 WHERE id=? AND session_id=?",
            (target_message_id, sid))
        # 旧活跃版本的下游链全部停用
        # （同组其他兄弟的子孙树需要停用，避免和新分支的下游混淆）
        siblings = self.db.query_all(
            "SELECT id FROM conversations WHERE version_group_id=? AND id!=?",
            (version_group_id, target_message_id))
        for sib in siblings:
            self._deactivate_downstream(sib["id"])
        # 递归激活目标的下游链（每个分叉点选择最近一次活跃的子节点）
        self._activate_downstream(target_message_id, sid)

    def _deactivate_downstream(self, parent_id: int) -> None:
        """递归停用某节点的所有子孙。"""
        children = self.db.query_all(
            "SELECT id FROM conversations WHERE parent_id=?", (parent_id,))
        for c in children:
            self.db.execute(
                "UPDATE conversations SET is_active=0 WHERE id=?", (c["id"],))
            self._deactivate_downstream(c["id"])

    def _activate_downstream(self, parent_id: int, sid: str) -> None:
        """递归激活下游链：每个分叉点选择 id 最大（最新）的子节点。"""
        children = self.db.query_all(
            "SELECT id, version_group_id FROM conversations "
            "WHERE parent_id=? AND session_id=? ORDER BY id DESC",
            (parent_id, sid))
        if not children:
            return
        # 按 version_group 分组，每组选最新的一条激活
        seen_groups = set()
        for c in children:
            vg = c["version_group_id"] or c["id"]
            if vg in seen_groups:
                continue
            seen_groups.add(vg)
            # 同组全部停用再激活最新的
            self.db.execute(
                "UPDATE conversations SET is_active=0 "
                "WHERE version_group_id=?", (vg,))
            self.db.execute(
                "UPDATE conversations SET is_active=1 WHERE id=?", (c["id"],))
            self._activate_downstream(c["id"], sid)
            break  # 每个分叉点只走一条路径

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

    # ---- 提议—确认闭环：pending 提议读回/消费 -------------------------

    def get_pending_proposal(self, sid: str) -> dict | None:
        """读回本会话待确认的下一步提议（提议—确认闭环）。

        仅当最近一条活跃 assistant 消息携带 status=pending 的 next_step_shown
        时返回 {"message_id":…, "text":…}；隔轮未确认的提议因后续 assistant
        消息覆盖而自然过期，无需显式 TTL。兼容旧格式 {"text":…}（无 status 视同 pending）。

        存量惰性自愈：读不到 pending 时，回扫最近 2 条活跃 assistant 消息尾部
        （旧代码产出的提议轮 next_step_shown 为空）；命中提议句即补落 pending，
        遇假承诺回复（过去式假声明/未来式空头承诺）继续向前回溯。
        任何异常返回 None（对话零阻塞铁律）。
        """
        try:
            rows = self.db.query_all(
                "SELECT id, content, next_step_shown FROM conversations "
                "WHERE session_id=? AND role='assistant' "
                "AND (is_active=1 OR is_active IS NULL) "
                "ORDER BY id DESC LIMIT 2", (sid,))
            for row in rows:
                if row["next_step_shown"]:
                    try:
                        data = json.loads(row["next_step_shown"])
                    except (json.JSONDecodeError, TypeError):
                        data = None
                    if isinstance(data, dict) and (data.get("text") or "").strip():
                        if data.get("status", "pending") == "pending":
                            return {"message_id": row["id"],
                                    "text": str(data["text"]).strip()}
                        # 已消费/已过期的提议不回溯（属于正常闭环终态）
                        return None
                # 惰性自愈：尾部提议句扫描（假承诺句不作提议，继续回溯）
                content = row["content"] or ""
                if detect_fake_claim(content):
                    continue
                proposal = detect_proposal_sentence(content)
                if proposal:
                    healed = {"text": proposal, "kind": "proposal",
                              "status": "pending"}
                    self.db.execute(
                        "UPDATE conversations SET next_step_shown=? WHERE id=?",
                        (json.dumps(healed, ensure_ascii=False), row["id"]))
                    return {"message_id": row["id"], "text": proposal}
            return None
        except Exception:  # noqa: BLE001
            return None

    def consume_pending_proposal(self, message_id: int) -> None:
        """将已承接的提议标记为 consumed（用户确认后本轮结束调用）。

        仅覆盖 next_step_shown 的 status 字段，保留 text/kind；
        行不存在（重新生成删消息等场景）时静默无操作。异常不阻断主链路。
        """
        try:
            row = self.db.query_one(
                "SELECT next_step_shown FROM conversations WHERE id=?",
                (message_id,))
            if not row or not row["next_step_shown"]:
                return
            data = json.loads(row["next_step_shown"])
            if isinstance(data, dict) and data.get("status", "pending") == "pending":
                data["status"] = "consumed"
                self.db.execute(
                    "UPDATE conversations SET next_step_shown=? WHERE id=?",
                    (json.dumps(data, ensure_ascii=False), message_id))
        except Exception:  # noqa: BLE001
            pass

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
    def load_recovery_context(self, sid: str) -> list[dict]:
        """返回消息列表（含 id 字段供压缩水位推进，送 LLM 前需剔除）。

        仅加载当前活跃分支的消息（is_active=1）。
        Head：会话最初 2 轮（存在压缩摘要时才拼入）；
        Summary：压缩摘要（有则拼入）；Tail：水位之后的全部原文。
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
        head_msgs = 4  # 2 轮 × 每轮 2 条（user + assistant）

        # 活跃分支过滤条件
        active_filter = "AND (is_active=1 OR is_active IS NULL)"

        # Tail：水位之后的全部活跃原文
        if watermark:
            tail_rows = self.db.query_all(
                "SELECT id,role,content FROM conversations "
                f"WHERE session_id=? AND id>? AND message_type='normal' {active_filter} ORDER BY id",
                (sid, watermark))
        else:
            tail_rows = self.db.query_all(
                "SELECT id,role,content FROM conversations "
                f"WHERE session_id=? AND message_type='normal' {active_filter} ORDER BY id",
                (sid,))
        msgs = []
        # 第一优先级：protected + 活跃的消息完整原文
        protected_rows = self.db.query_all(
            "SELECT id,role,content FROM conversations "
            "WHERE session_id=? AND message_type='normal' "
            f"AND protected_from_compression=1 {active_filter} ORDER BY id",
            (sid,))
        for r in protected_rows:
            if r["role"] in ("user", "assistant"):
                msgs.append({"role": r["role"], "content": r["content"],
                             "id": r["id"]})
        if summary_text:
            head_rows = self.db.query_all(
                "SELECT id,role,content FROM conversations WHERE session_id=? "
                f"AND id<=? AND message_type='normal' {active_filter} ORDER BY id LIMIT ?",
                (sid, watermark or 0, head_msgs)) if watermark else []
            for r in head_rows:
                if r["role"] in ("user", "assistant"):
                    msgs.append({"role": r["role"], "content": r["content"],
                                 "id": r["id"]})
            msgs.append({"role": "system",
                         "content": PROMPTS.load_raw("agent/prompts/compact_prefix")
                         + "\n" + summary_text})
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
    def _should_protect(role: str, content: str) -> bool:
        """判断消息是否应标记为 protected_from_compression。

        规则：
        - 用户消息 ≥500 字 → protected
        - AI 回复 ≥3 个 ## → protected
        - AI 回复 ≥100 行代码 → protected
        - AI 回复 ≥1000 字 + 结构化信号（≥5 列表/≥2 代码块/≥8 段落）→ protected
        """
        if role == "user":
            return len(content) >= 500
        if role == "assistant":
            if len(re.findall(r'^##\s+', content, re.MULTILINE)) >= 3:
                return True
            code_lines = sum(
                len(block.split('\n'))
                for block in re.findall(r'```[\s\S]*?```', content))
            if code_lines >= 100:
                return True
            if len(content) >= 1000:
                bullets = len(re.findall(
                    r'^\s*[-*+]\s+', content, re.MULTILINE))
                blocks = content.count('```') // 2
                paras = len([p for p in content.split('\n\n') if p.strip()])
                if bullets >= 5 or blocks >= 2 or paras >= 8:
                    return True
        return False

    def search_history(
        self,
        session_id: str,
        query: str,
        top_k: int = 5,
        role_filter: str | None = None,
        time_window_messages: int = 200,
    ) -> list[dict]:
        """FTS5 trigram 全文检索会话历史，bm25() 排名。

        Args:
            session_id: 目标会话
            query: 搜索查询（原生文本，直接传给 FTS5 MATCH）
            top_k: 返回条数上限
            role_filter: None=不过滤, "user"=仅用户, "assistant"=仅AI
            time_window_messages: 时间窗（最近 N 条消息内检索）
        """
        if not query or not query.strip():
            return []

        conditions = [
            "conversations_fts MATCH ?",
            "c.session_id = ?",
            "c.message_type = 'normal'",
        ]
        params = [query.strip(), session_id]

        if role_filter:
            conditions.append("c.role = ?")
            params.append(role_filter)

        # 时间窗：仅在最近 N 条消息内检索
        max_id_row = self.db.query_one(
            "SELECT MAX(id) as max_id FROM conversations WHERE session_id=?",
            (session_id,))
        if max_id_row and max_id_row["max_id"]:
            min_id = max(0, max_id_row["max_id"] - time_window_messages)
            conditions.append("c.id > ?")
            params.append(min_id)

        where = " AND ".join(conditions)
        sql = f"""
            SELECT c.id, c.role, c.content, c.create_time,
                   c.protected_from_compression,
                   bm25(conversations_fts, 0, 1, 0) AS score
            FROM conversations c
            JOIN conversations_fts ON c.id = conversations_fts.rowid
            WHERE {where}
            ORDER BY score LIMIT ?
        """
        params.append(top_k)

        rows = self.db.query_all(sql, tuple(params))
        return [
            {
                "id": r["id"],
                "role": r["role"],
                "content": r["content"],
                "score": r["score"],
                "protected": bool(r["protected_from_compression"]),
                "created_at": r["create_time"],
            }
            for r in rows
        ]


def _handoff_status_from_row(r) -> str | None:
    """从 sessions 行派生 handoff 摘要状态（会话上下文管理方案 v2）。
    返回 None / 'generating' / 'ready' / 'failed'。"""
    path = r["handoff_summary_path"]
    if not path:
        return None
    from pathlib import Path as _Path
    from memory.md_file import split_frontmatter
    try:
        p = _Path(path)
        if not p.exists():
            return "generating"
        fm, _ = split_frontmatter(p.read_text(encoding="utf-8"))
        return fm.get("status", "ready")
    except Exception:  # noqa: BLE001
        return "failed"
