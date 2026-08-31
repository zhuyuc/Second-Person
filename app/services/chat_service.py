"""对话业务逻辑：标题/handoff/反馈等。"""
from __future__ import annotations

import json
import logging

from infrastructure.prompt_loader import PROMPTS

logger = logging.getLogger("second_person.chat_service")


class ChatService:
    def __init__(self, container) -> None:
        self.c = container

    async def generate_handoff(self, from_sid: str, to_sid: str) -> None:
        """后台异步生成 handoff 摘要，失败静默降级为 status=failed。"""
        from memory.handoff_summary import HandoffSummaryGenerator
        from langfuse.integration import get_tracer
        c = self.c
        tracer = get_tracer()
        trace = tracer.trace_start(
            "handoff.summary", session_id=to_sid,
            input={"from_session_id": from_sid, "to_session_id": to_sid})
        try:
            gen = HandoffSummaryGenerator(
                llm=c.llm, db=c.db, data_dir=c.data_dir,
                config=c.config, bus=c.bus, tracer=tracer, file_writer=c.fw)
            await gen.generate(from_sid, to_sid)
            trace.end(output={"status": "completed"})
        except Exception as e:  # noqa: BLE001
            trace.end(level="ERROR", status_message=str(e))
            logger.warning(
                "handoff 摘要生成失败 from=%s to=%s: %s", from_sid, to_sid, e)
            c.db.execute(
                "UPDATE sessions SET handoff_summary_path='__failed__' "
                "WHERE session_id=?", (to_sid,))

    async def generate_title(self, sid: str, message: str) -> None:
        """首条消息异步生成标题。"""
        c = self.c
        try:
            snap = c.providers.snapshot_for("agent")
            q = message.split(
                "\n---\n")[-1].strip() if "\n---\n" in message else message
            q = q[:500]
            if not snap:
                return

            async def _call_llm():
                try:
                    from infrastructure.json_repair import repair_json
                    resp = await c.llm.chat(snap, [
                        {"role": "system", "content": PROMPTS.load_raw(
                            "app/prompts/title_gen")},
                        {"role": "user", "content": q}], source="title_gen",
                        session_id=sid, json_mode=True)
                    raw = resp["content"].strip()
                    try:
                        obj = repair_json(raw)
                        return (obj.get("title") or "")[:15]
                    except (ValueError, AttributeError):
                        return raw[:15]
                except Exception:  # noqa: BLE001
                    return None

            result = await _call_llm()
            if result:
                c.sessions.set_auto_title(sid, result)
        except Exception:  # noqa: BLE001
            logger.warning("会话标题生成失败 session=%s", sid, exc_info=True)

    async def handle_feedback(self, message_id: int, feedback: int,
                              reason: str | None) -> None:
        """处理点赞/点踩反馈。"""
        c = self.c
        c.sessions.set_feedback(message_id, feedback)
        if feedback == 2:
            c.signals.set_explicit_reaction(message_id, 2)
            if reason:
                await self.handle_downvote(message_id, reason)
        elif feedback == 1:
            c.signals.set_explicit_reaction(message_id, 1)
            await self.handle_upvote(message_id)

    async def handle_upvote(self, message_id: int) -> None:
        """点赞：对该回复引用的每条记忆执行 confidence 升级。"""
        c = self.c
        row = c.db.query_one(
            "SELECT session_id, citations FROM conversations WHERE id=?", (message_id,))
        cites = json.loads(row["citations"]) if row and row["citations"] else []
        for cit in cites:
            if cit.get("id"):
                await c.lifecycle.upvote_upgrade(cit["id"])
        if hasattr(c, "mood_trigger") and c.mood_trigger and row:
            c.mood_trigger.record(
                session_id=row["session_id"], message_id=message_id,
                scope="ai", source_type="evaluation", event_key="user_thumbs_up",
                attribution="other", mood_hint="pleased", intensity_hint=0.4,
                note="用户点赞")

    async def handle_downvote(self, message_id: int, reason: str) -> None:
        c = self.c
        row = c.db.query_one(
            "SELECT session_id, content, citations FROM conversations WHERE id=?",
            (message_id,))
        cites = json.loads(row["citations"]) if row and row["citations"] else []
        if reason == "memory_stale":
            for cit in cites:
                await c.lifecycle.downvote_stale(cit.get("id"))
        elif reason == "tone_wrong":
            if hasattr(c, "conflict_scanner") and c.conflict_scanner:
                context_snippet = (row["content"] or "")[:300] if row else ""
                c.conflict_scanner.enqueue_tone_review(
                    message_id,
                    row["session_id"] if row else "",
                    context_snippet,
                )
        if hasattr(c, "mood_trigger") and c.mood_trigger and row:
            c.mood_trigger.record(
                session_id=row["session_id"], message_id=message_id,
                scope="ai", source_type="evaluation", event_key="user_thumbs_down",
                mood_hint="concerned", intensity_hint=0.4,
                note=f"用户点踩：{reason or '无原因'}")
