"""Response-shape and feedback signal persistence."""
from __future__ import annotations

import re

from infrastructure.timeutil import now_cst


IMPLICIT_KEYWORDS = [
    "太长了", "太短了", "说人话", "别啰嗦", "继续说", "展开讲讲", "给个表格",
    "别列 bullet", "简短", "详细点",
]


def collect_signal_shape(content: str) -> dict:
    paragraphs = [p for p in (content or "").split("\n\n") if p.strip()]
    bullets = len(re.findall(r"^\s*[-*]\s", content or "", flags=re.M))
    code_blocks = (content or "").count("```") // 2
    tables = len(re.findall(r"^\|.*\|", content or "", flags=re.M))
    position = "middle"
    if paragraphs:
        if any(word in paragraphs[0] for word in ("建议", "结论", "总的来说", "简单说")):
            position = "start"
        elif any(word in paragraphs[-1] for word in ("建议", "结论", "综上")):
            position = "end"
    return {"char_count": len(content or ""), "paragraph_count": len(paragraphs),
            "bullet_count": bullets, "code_block_count": code_blocks,
            "table_count": tables, "conclusion_position": position}


def detect_implicit_keywords(next_user_message: str) -> str:
    return ";".join(keyword for keyword in IMPLICIT_KEYWORDS
                     if keyword in (next_user_message or ""))


class SignalCollector:
    def __init__(self, db):
        self.db = db

    def record_shape(self, message_id: int, shape: dict, context_label: str) -> int:
        cur = self.db.execute(
            "INSERT INTO response_signals(message_id,char_count,paragraph_count,"
            "bullet_count,code_block_count,table_count,conclusion_position,"
            "context_label,create_time) VALUES(?,?,?,?,?,?,?,?,?)",
            (message_id, shape["char_count"], shape["paragraph_count"],
             shape["bullet_count"], shape["code_block_count"], shape["table_count"],
             shape["conclusion_position"], context_label,
             now_cst().isoformat(timespec="seconds")),
        )
        return cur.lastrowid

    def backfill_reaction(self, message_id: int, implicit_reaction: str,
                          keywords: str) -> None:
        row = self.db.query_one(
            "SELECT id,explicit_keywords FROM response_signals WHERE message_id=? "
            "ORDER BY id DESC LIMIT 1", (message_id,))
        if not row:
            return
        existing = row["explicit_keywords"] or ""
        merged = ";".join(filter(None, [existing, keywords]))
        self.db.execute(
            "UPDATE response_signals SET implicit_reaction=?, explicit_keywords=? WHERE id=?",
            (implicit_reaction, merged, row["id"]))

    def set_explicit_reaction(self, message_id: int, reaction: int) -> None:
        self.db.execute(
            "UPDATE response_signals SET explicit_reaction=? WHERE message_id=?",
            (reaction, message_id))
