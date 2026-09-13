"""跨轮文件工作集与操作卡片（turn-tail 注入）。

设计目标：
- 从 fs_observations 投影 Top-N 工作集，避免每轮重新探索路径
- session_file_cards 保留短操作痕迹（含 spill locator）
- 有内容时每轮注入（不进 recovery，不能静默，否则跨轮丢路径）
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from infrastructure.timeutil import now_cst

WORKING_SET_TOP_N = 8
FILE_CARDS_MAX = 12
FILE_CARDS_CHAR_BUDGET = 500

# 续写/改文件意图（有工作集时触发记忆降级；明确回忆语除外）
_FILE_EDIT_INTENT = [
    r"继续", r"接着", r"再改", r"改一下", r"改成", r"修改", r"编辑",
    r"那个文件", r"这个文件", r"刚才那个", r"同文件", r"同一文件",
    r"加上", r"删掉", r"删除", r"替换", r"更新", r"写到", r"保存",
    r"\.(?:html?|py|md|js|ts|tsx|vue|css|json|ya?ml|toml|txt)\b",
]


@dataclass(frozen=True)
class WorkingSetTurn:
    """一轮上下文组装用的工作集投影。"""
    entries: tuple[dict[str, Any], ...]
    working_set_emit: str | None      # None = 静默
    working_set_full: str | None      # 有观察时总有全文（压缩/降级用）
    file_cards_emit: str | None
    file_cards_full: str | None
    demote_memory: bool


class FileCardStore:
    """滚动文件操作卡片 + 工作集指纹状态。"""

    def __init__(self, db):
        self.db = db

    def append(self, session_id: str, path: str, op: str, *,
               version: str | None = None,
               spill_path: str | None = None) -> None:
        if not session_id or not path:
            return
        now = now_cst().isoformat(timespec="seconds")
        self.db.execute(
            "INSERT INTO session_file_cards("
            "session_id, path, op, version, spill_path, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (session_id, path, op, version, spill_path, now))
        self._trim(session_id)

    def _trim(self, session_id: str) -> None:
        rows = self.db.query_all(
            "SELECT id FROM session_file_cards WHERE session_id=? "
            "ORDER BY id DESC",
            (session_id,))
        if len(rows) <= FILE_CARDS_MAX:
            return
        drop_ids = [r["id"] for r in rows[FILE_CARDS_MAX:]]
        placeholders = ",".join("?" * len(drop_ids))
        self.db.execute(
            f"DELETE FROM session_file_cards WHERE id IN ({placeholders})",
            drop_ids)

    def list_recent(self, session_id: str, limit: int = FILE_CARDS_MAX
                    ) -> list[dict[str, Any]]:
        rows = self.db.query_all(
            "SELECT path, op, version, spill_path, created_at "
            "FROM session_file_cards WHERE session_id=? "
            "ORDER BY id DESC LIMIT ?",
            (session_id, limit))
        return [dict(r) for r in reversed(rows)]

    def get_state(self, session_id: str) -> tuple[str | None, str | None]:
        row = self.db.query_one(
            "SELECT fingerprint, cards_fingerprint "
            "FROM session_working_set_state WHERE session_id=?",
            (session_id,))
        if not row:
            return None, None
        return row["fingerprint"], row["cards_fingerprint"]

    def set_state(self, session_id: str, *, fingerprint: str | None = None,
                  cards_fingerprint: str | None = None) -> None:
        now = now_cst().isoformat(timespec="seconds")
        prev_fp, prev_cards = self.get_state(session_id)
        fp = fingerprint if fingerprint is not None else (prev_fp or "")
        cards = (cards_fingerprint if cards_fingerprint is not None
                 else prev_cards)
        self.db.execute(
            "INSERT INTO session_working_set_state("
            "session_id, fingerprint, cards_fingerprint, updated_at) "
            "VALUES(?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
            "fingerprint=excluded.fingerprint, "
            "cards_fingerprint=excluded.cards_fingerprint, "
            "updated_at=excluded.updated_at",
            (session_id, fp, cards, now))

    def invalidate_session(self, session_id: str) -> None:
        self.db.execute(
            "DELETE FROM session_file_cards WHERE session_id=?", (session_id,))
        self.db.execute(
            "DELETE FROM session_working_set_state WHERE session_id=?",
            (session_id,))


def is_file_edit_intent(query: str) -> bool:
    q = (query or "").strip()
    if not q:
        return False
    return any(re.search(p, q, re.IGNORECASE) for p in _FILE_EDIT_INTENT)


def should_demote_memory(query: str, entries: list[dict] | tuple) -> bool:
    """编辑态记忆降级：有工作集且像续写/刚改过文件时跳过全量检索。"""
    if not entries:
        return False
    from memory.retriever_gates import has_recall_intent
    if has_recall_intent(query or ""):
        return False
    has_mut = any(
        (e.get("last_op") or "") in ("write", "edit") for e in entries)
    return has_mut or is_file_edit_intent(query or "")


def fingerprint_entries(entries: list[dict] | tuple) -> str:
    payload = [
        {"path": e.get("target_key") or e.get("path"),
         "version": e.get("version"),
         "op": e.get("last_op") or e.get("op")}
        for e in entries
    ]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def fingerprint_cards(cards: list[dict] | tuple) -> str:
    payload = [
        {"path": c.get("path"), "op": c.get("op"),
         "version": c.get("version"), "spill": c.get("spill_path")}
        for c in cards
    ]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def render_working_set(entries: list[dict] | tuple) -> str | None:
    if not entries:
        return None
    lines = [
        "[文件工作集] 本会话近期已观察的文件。用户指「那个文件 / 继续改」时"
        "优先复用下列路径与 version；写前可用已有 version，冲突再 fs_read。",
    ]
    for e in entries:
        path = e.get("target_key") or e.get("path") or ""
        op = e.get("last_op") or "read"
        ver = e.get("version") or "?"
        when = e.get("observed_at") or ""
        age = f" @ {when}" if when else ""
        lines.append(f"- {op} `{path}` (v={ver}){age}")
    return "\n".join(lines)


def render_file_cards(cards: list[dict] | tuple,
                      *, budget: int = FILE_CARDS_CHAR_BUDGET) -> str | None:
    if not cards:
        return None
    header = "[文件工作痕迹] 近期成功的文件/溢写操作（无正文，仅定位）："
    body_lines: list[str] = []
    for c in cards:
        path = c.get("path") or ""
        op = c.get("op") or "?"
        ver = c.get("version")
        spill = c.get("spill_path")
        piece = f"- {op} `{path}`"
        if ver:
            piece += f" (v={ver})"
        if spill:
            piece += f" spill=`{spill}`"
        body_lines.append(piece)
    text = "\n".join([header, *body_lines])
    if len(text) <= budget:
        return text
    # 超预算：从最旧砍，保留标题 + 最新若干行
    kept_rev: list[str] = []
    for line in reversed(body_lines):
        trial = "\n".join([header, *reversed(kept_rev + [line])])
        if len(trial) > budget:
            break
        kept_rev.append(line)
    if not kept_rev:
        return header[:budget]
    return "\n".join([header, *reversed(kept_rev)])


def build_working_set_turn(*, observations, cards: FileCardStore,
                           session_id: str, user_message: str,
                           top_n: int = WORKING_SET_TOP_N) -> WorkingSetTurn:
    """组装本轮工作集投影并决定是否 emit。"""
    entries = []
    if observations is not None:
        try:
            entries = observations.list_recent(session_id, limit=top_n) or []
        except Exception:  # noqa: BLE001
            entries = []
    try:
        card_rows = cards.list_recent(session_id) if cards else []
    except Exception:  # noqa: BLE001
        card_rows = []

    ws_full = render_working_set(entries)
    fc_full = render_file_cards(card_rows)
    ws_fp = fingerprint_entries(entries) if entries else ""
    fc_fp = fingerprint_cards(card_rows) if card_rows else ""

    # 工作集/卡片必须每轮注入：它们只在 turn-tail，不会进入 recovery history；
    # 若「指纹静默」则下一轮模型会丢失路径（与 rediscovery 问题同构）。
    # 指纹仍写入 state，供观测与日后短 delta 优化。
    ws_emit = ws_full
    fc_emit = fc_full

    if cards is not None and (entries or card_rows):
        try:
            cards.set_state(
                session_id,
                fingerprint=ws_fp if entries else "",
                cards_fingerprint=fc_fp if card_rows else "",
            )
        except Exception:  # noqa: BLE001
            pass

    return WorkingSetTurn(
        entries=tuple(entries),
        working_set_emit=ws_emit,
        working_set_full=ws_full,
        file_cards_emit=fc_emit,
        file_cards_full=fc_full,
        demote_memory=should_demote_memory(user_message, entries),
    )


def recall_context_for_compact(ws: WorkingSetTurn | None) -> str | None:
    """压缩引擎强制带入的可召回上下文。"""
    if ws is None:
        return None
    parts = [p for p in (ws.working_set_full, ws.file_cards_full) if p]
    return "\n\n".join(parts) if parts else None


def append_recall_stub(summary: str, recall_context: str | None) -> str:
    """在压缩摘要末尾机械追加可召回定位，避免模型漏写。"""
    body = (summary or "").rstrip()
    stub_lines = ["", "## 可召回定位"]
    if recall_context:
        stub_lines.append(recall_context)
    else:
        stub_lines.append("(无)")
    # 若模型已写同名节，仍追加主机版（以主机为准）
    return body + "\n" + "\n".join(stub_lines)
