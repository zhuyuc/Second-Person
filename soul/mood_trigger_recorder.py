"""
情绪触发采集器（规则通道）—— 记录规则匹配到的情绪触发事件。

- 定位：纯记录器，只写 DB 不发起 LLM 调用
- 写入：每轮对话中规则匹配到的触发事件登记到 mood_triggers 表
- 读取：summarize_for_turn() 为 mood_judge_v2 提供本轮规则触发摘要
- 时间：统一使用 now_cst()（项目时间规范）
"""
from __future__ import annotations

from infrastructure.timeutil import now_cst


class MoodTriggerRecorder:
    def __init__(self, db):
        self.db = db

    def record(self, session_id: str, message_id: int | None,
               scope: str, source_type: str, event_key: str,
               attribution: str | None = None,
               mood_hint: str | None = None,
               intensity_hint: float | None = None,
               note: str = "") -> None:
        """登记一条规则触发事件。"""
        self.db.execute(
            "INSERT INTO mood_triggers(session_id,message_id,scope,source_type,"
            "event_key,attribution,mood_hint,intensity_hint,note,detected_by,"
            "create_time) VALUES(?,?,?,?,?,?,?,?,?,'rule',?)",
            (session_id, message_id, scope, source_type, event_key,
             attribution, mood_hint, intensity_hint, note,
             now_cst().isoformat(timespec="seconds")))

    def summarize_for_turn(self, session_id: str, message_id: int) -> str:
        """汇总本轮规则触发事件，生成供 mood_judge_v2 参考的摘要文本。"""
        rows = self.db.query_all(
            "SELECT scope, source_type, event_key, attribution, note "
            "FROM mood_triggers WHERE session_id=? AND message_id=? "
            "ORDER BY id DESC LIMIT 8", (session_id, message_id))
        if not rows:
            return "（本轮无规则触发登记）"
        lines = []
        for r in rows:
            attr = f" 归因={r['attribution']}" if r["attribution"] else ""
            note = f" - {r['note']}" if r["note"] else ""
            lines.append(
                f"- [{r['scope']}][{r['source_type']}] "
                f"{r['event_key']}{attr}{note}")
        return "\n".join(lines)
