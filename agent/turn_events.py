"""Durable turn events and model-history projection for the Agent runtime."""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from infrastructure.timeutil import now_cst


def _now() -> str:
    return now_cst().isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def args_hash(params: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(params).encode("utf-8")).hexdigest()


class TurnEventStore:
    """The durable source of truth for one agent turn.

    Events intentionally keep a small, model-visible projection separate from
    operational metadata. The full tool payload stays in the event record or
    an artifact and is never reconstructed from a transient queue.
    """

    def __init__(self, db) -> None:
        self.db = db

    def start_turn(self, session_id: str, *, reasoning_effort: str,
                   max_steps: int, request_id: str | None = None,
                   langfuse_trace_id: str | None = None) -> dict[str, Any]:
        now = _now()
        turn_id = f"turn_{uuid.uuid4().hex}"
        self.db.execute(
            "INSERT INTO agent_turns(id,session_id,request_id,status,reasoning_effort,"
            "max_steps,current_step,langfuse_trace_id,created_at,updated_at) "
            "VALUES(?,?,?,'running',?,?,0,?,?,?)",
            (turn_id, session_id, request_id, reasoning_effort, max_steps,
             langfuse_trace_id, now, now))
        self.append(turn_id, "turn.started", actor="host", payload={
            "session_id": session_id, "reasoning_effort": reasoning_effort,
            "max_steps": max_steps, "request_id": request_id,
        })
        return self.get_turn(turn_id) or {"id": turn_id}

    def get_turn(self, turn_id: str) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM agent_turns WHERE id=?", (turn_id,))

    def active_turn(self, session_id: str) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM agent_turns WHERE session_id=? "
            "AND status IN ('running','awaiting_approval','awaiting_input') "
            "ORDER BY created_at DESC LIMIT 1", (session_id,))

    def append(self, turn_id: str, event_type: str, *, actor: str,
               payload: dict[str, Any], step: int = 0,
               call_id: str | None = None, model_visible: bool = False) -> dict[str, Any]:
        seq_row = self.db.query_one(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM agent_events WHERE turn_id=?",
            (turn_id,))
        event = {
            "id": f"evt_{uuid.uuid4().hex}", "turn_id": turn_id,
            "seq": int(seq_row["next_seq"]), "step": step,
            "type": event_type, "actor": actor, "call_id": call_id,
            "model_visible": bool(model_visible), "payload": payload,
            "created_at": _now(),
        }
        self.db.execute(
            "INSERT INTO agent_events(id,turn_id,seq,step,type,actor,call_id,model_visible,"
            "payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (event["id"], turn_id, event["seq"], step, event_type, actor,
             call_id, 1 if model_visible else 0, canonical_json(payload),
             event["created_at"]))
        return event

    def events(self, turn_id: str, *, after_seq: int = 0) -> list[dict[str, Any]]:
        rows = self.db.query_all(
            "SELECT * FROM agent_events WHERE turn_id=? AND seq>? ORDER BY seq",
            (turn_id, after_seq))
        events = []
        for row in rows:
            row["payload"] = json.loads(row.pop("payload_json") or "{}")
            row["model_visible"] = bool(row["model_visible"])
            events.append(row)
        return events

    def finish(self, turn_id: str, *, status: str, end_reason: str,
               step: int, payload: dict[str, Any] | None = None) -> None:
        now = _now()
        self.append(turn_id, "turn.finished", actor="host", step=step,
                    payload={"status": status, "end_reason": end_reason,
                             **(payload or {})})
        self.db.execute(
            "UPDATE agent_turns SET status=?,end_reason=?,current_step=?,updated_at=?,ended_at=? "
            "WHERE id=?", (status, end_reason, step, now, now, turn_id))

    def set_status(self, turn_id: str, status: str, *, step: int | None = None) -> None:
        if step is None:
            self.db.execute("UPDATE agent_turns SET status=?,updated_at=? WHERE id=?",
                            (status, _now(), turn_id))
        else:
            self.db.execute(
                "UPDATE agent_turns SET status=?,current_step=?,updated_at=? WHERE id=?",
                (status, step, _now(), turn_id))

    def model_messages(self, turn_id: str) -> list[dict[str, Any]]:
        """Project durable model-visible events into OpenAI-compatible messages."""
        messages: list[dict[str, Any]] = []
        for event in self.events(turn_id):
            if not event["model_visible"]:
                continue
            payload = event["payload"]
            if event["type"] == "user.message":
                messages.append({"role": "user", "content": payload.get("content", "")})
            elif event["type"] == "assistant.message":
                messages.append({"role": "assistant", "content": payload.get("content", "")})
            elif event["type"] == "assistant.tool_calls":
                messages.append({"role": "assistant", "content": payload.get("content", ""),
                                 "tool_calls": payload.get("tool_calls", [])})
            elif event["type"] == "tool.result":
                messages.append({"role": "tool", "tool_call_id": event["call_id"],
                                 "content": payload.get("model_content", "")})
        return messages

    def unresolved_calls(self, turn_id: str) -> list[dict[str, Any]]:
        calls = {e["call_id"]: e for e in self.events(turn_id)
                 if e["type"] == "tool.call" and e["call_id"]}
        terminal = {e["call_id"] for e in self.events(turn_id)
                    if e["type"] in {"tool.result", "tool.blocked"} and e["call_id"]}
        return [event for call_id, event in calls.items() if call_id not in terminal]
