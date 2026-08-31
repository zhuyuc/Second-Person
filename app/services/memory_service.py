"""记忆中心业务逻辑：反馈、治理、候选池等。"""
from __future__ import annotations

import json
import uuid

from infrastructure.timeutil import now_iso


class MemoryService:
    def __init__(self, container) -> None:
        self.c = container

    async def memory_feedback(self, body: dict) -> None:
        """记忆级反馈直接进入检索与治理闭环。"""
        c = self.c
        mid = body.get("memory_id")
        kind = body.get("feedback_type")
        if kind not in {"irrelevant", "stale", "incorrect", "helpful"} or not c.palace.get(mid):
            raise ValueError("无效的记忆反馈")
        c.lifecycle.record_feedback(mid, kind, body.get("message_id"), body.get("query_text"))
        if kind == "stale":
            await c.lifecycle.downvote_stale(mid)
        elif kind == "incorrect":
            already_open = c.db.query_one(
                "SELECT 1 FROM memory_governance_items WHERE primary_memory_id=? "
                "AND item_type='memory_incorrect' AND status='open' LIMIT 1", (mid,))
            if not already_open:
                row = c.palace.get(mid)
                c.db.execute(
                    "INSERT INTO memory_governance_items(item_id,item_type,primary_memory_id,"
                    "priority,status,reason,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (f"gov_{uuid.uuid4().hex[:12]}", "memory_incorrect", mid,
                     (row.get("access_count", 0) or 0) + 3, "open", "用户标记记忆内容不正确",
                     json.dumps({"query": body.get("query_text")}, ensure_ascii=False),
                     now_iso()))
        elif kind == "helpful":
            await c.lifecycle.upvote_upgrade(mid)

    def resolve_governance(self, item_id: str, action: str) -> None:
        if action not in {"dismiss", "reviewed"}:
            raise ValueError("无效的治理动作")
        self.c.db.execute(
            "UPDATE memory_governance_items SET status=?,resolved_at=? WHERE item_id=? AND status='open'",
            ("dismissed" if action == "dismiss" else "resolved", now_iso(), item_id))

    async def confirm_candidate(self, candidate_id: str) -> dict:
        c = self.c
        if not c.memory_gate.confirm(candidate_id):
            raise ValueError("候选不存在或已处理")
        written = await c.memory_gate.promote_ready(c.distiller)
        return {"candidate_id": candidate_id, "written": written}

    def reject_candidate(self, candidate_id: str, reason: str) -> None:
        if not self.c.memory_gate.reject(candidate_id, reason):
            raise ValueError("候选不存在或已处理")
