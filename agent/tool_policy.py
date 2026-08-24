"""Host-owned tool risk and approval decisions."""
from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from infrastructure.timeutil import now_cst
from .turn_events import args_hash


RISK_LEVELS = frozenset({"read", "write", "destructive", "external_side_effect"})
APPROVAL_POLICIES = frozenset({"never", "once_per_turn", "every_call"})


@dataclass(frozen=True)
class ToolDecision:
    action: str
    risk_level: str
    reason: str = ""
    approval_id: str | None = None


class ToolPolicy:
    def __init__(self, db, config) -> None:
        self.db = db
        self.config = config
        self._waiters: dict[str, asyncio.Event] = {}

    def inspect(self, tool, params: dict[str, Any], *, turn_id: str,
                call_id: str) -> ToolDecision:
        spec = tool.spec
        risk = getattr(spec, "risk_level", "read")
        if spec.name in {"generate_document"} and risk == "read":
            risk = "write"
        if spec.destructive and risk == "read":
            risk = "destructive"
        if risk not in RISK_LEVELS:
            return ToolDecision("block", "destructive", "工具风险声明无效")
        if risk == "read":
            return ToolDecision("execute", risk)
        policy = getattr(spec, "approval_policy", "never")
        if spec.destructive and policy == "never":
            policy = "every_call"
        if policy not in APPROVAL_POLICIES:
            return ToolDecision("block", risk, "工具确认策略无效")
        if self.config.get("tool_writes_require_approval", True) and policy == "never":
            policy = "every_call"
        if policy == "never":
            return ToolDecision("execute", risk)
        digest = args_hash(params)
        approved = self.db.query_one(
            "SELECT id FROM tool_approvals WHERE turn_id=? AND tool_name=? "
            "AND normalized_args_hash=? AND status='approved' AND expires_at>? "
            "ORDER BY created_at DESC LIMIT 1",
            (turn_id, spec.name, digest,
             now_cst().isoformat(timespec="seconds")))
        if approved and policy == "once_per_turn":
            return ToolDecision("execute", risk, approval_id=approved["id"])
        approval_id = f"apr_{uuid.uuid4().hex}"
        expires = now_cst() + timedelta(minutes=self.config.get("tool_approval_ttl_minutes", 10))
        self.db.execute(
            "INSERT INTO tool_approvals(id,turn_id,call_id,tool_name,normalized_args_hash,"
            "risk_level,scope_json,status,expires_at,created_at) VALUES(?,?,?,?,?,?,?,'pending',?,?)",
            (approval_id, turn_id, call_id, spec.name, digest, risk,
             json.dumps(getattr(spec, "scope", {}) or {}, ensure_ascii=False),
             expires.isoformat(timespec="seconds"),
             now_cst().isoformat(timespec="seconds")))
        self._waiters[approval_id] = asyncio.Event()
        return ToolDecision("approval", risk, "需要用户确认", approval_id)

    async def wait(self, approval_id: str) -> bool:
        row = self.db.query_one("SELECT status,expires_at FROM tool_approvals WHERE id=?", (approval_id,))
        if not row:
            return False
        if row["status"] == "approved":
            return True
        if row["status"] != "pending" or row["expires_at"] <= now_cst().isoformat(timespec="seconds"):
            return False
        waiter = self._waiters.get(approval_id)
        if waiter is None:
            return False
        remaining = max(1.0, (datetime.fromisoformat(row["expires_at"]) - now_cst()).total_seconds())
        try:
            await asyncio.wait_for(waiter.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            self.decide(approval_id, approved=False)
        row = self.db.query_one("SELECT status FROM tool_approvals WHERE id=?", (approval_id,))
        return bool(row and row["status"] == "approved")

    def decide(self, approval_id: str, *, approved: bool) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM tool_approvals WHERE id=?", (approval_id,))
        if not row or row["status"] != "pending":
            return None
        status = "approved" if approved else "rejected"
        self.db.execute("UPDATE tool_approvals SET status=?,decided_at=? WHERE id=?",
                        (status, now_cst().isoformat(timespec="seconds"), approval_id))
        waiter = self._waiters.pop(approval_id, None)
        if waiter:
            waiter.set()
        row["status"] = status
        return row
