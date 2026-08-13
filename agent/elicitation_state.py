"""
追问式补充信息模块 · ElicitationState 状态机 + asyncio.Event 暂停/恢复。

一、状态流转：
  pending → answered_all  (用户全部作答、LLM 已消费答案)
  pending → answered_partial  (部分作答但已关闭)
  pending → closed   (用户主动关闭 X 或中断)
  pending → expired  (超时)

二、暂停/恢复机制：
  ask_user 触发时 → 写 elicitations 表 → emit SSE elicitation → await event.wait()
  /answer 或 /close → 写答案 → event.set() 唤醒 pipeline
  pipeline 恢复后 → 检查 status → 决定下一步（构造 tool_result → LLM 二次调用 或 跳过）

三、超时自动过期：
  ElicitationState 内部记录 expires_at，外部定时任务或 pipeline 挂起时定期检查；
  超时后自动唤醒并标记 status=expired。

四、并发安全：
  每个 elicitation 实例通过 id (tool_use_id) 索引；
  同一 session 同时最多一个 pending elicitation（由调用方保证）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger("second_person.elicitation")


class ElicitationStatus(str, Enum):
    PENDING = "pending"
    ANSWERED_ALL = "answered_all"
    ANSWERED_PARTIAL = "answered_partial"
    CLOSED = "closed"
    EXPIRED = "expired"


@dataclass
class ElicitationState:
    """单个追问的运行时状态。持久化字段对应 elicitations 表。

    NOT persisted here: _wake_event (内存 asyncio.Event，服务重启丢失，由 pending 状态查询恢复)。
    """

    id: str                          # tool_use_id
    session_id: str
    questions_json: str              # ask_user 原样入参 JSON
    answers_json: str | None = None  # 前端提交的结构化答案 JSON
    status: ElicitationStatus = ElicitationStatus.PENDING
    trigger_source: str | None = None   # meta_cog / intent_low_conf / strategy_gap
    close_reason: str | None = None     # user_x / interrupt / session_switch
    platform: str = "web"
    created_at: float = field(default_factory=time.time)
    resolved_at: float | None = None
    expires_at: float = 0.0

    # ---- 内存状态（不持久化） ----
    _wake_event: asyncio.Event = field(
        default_factory=asyncio.Event, repr=False)

    async def wait(self, timeout: float | None = None) -> bool:
        """挂起 pipeline，等待答案到达。返回 True=正常唤醒，False=超时。"""
        if timeout is not None:
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=timeout)
                return True
            except asyncio.TimeoutError:
                return False
        await self._wake_event.wait()
        return True

    def wake(self) -> None:
        """唤醒 pipeline。幂等：重复调用不抛异常。"""
        self._wake_event.set()

    def answer(self, answers_json: str) -> None:
        """写入答案并唤醒。"""
        self.answers_json = answers_json
        self.status = ElicitationStatus.ANSWERED_ALL
        self.resolved_at = time.time()
        self.wake()

    def close(self, reason: str, answers_json: str | None = None) -> None:
        """关闭追问并唤醒。"""
        self.close_reason = reason
        self.status = ElicitationStatus.CLOSED
        self.resolved_at = time.time()
        if answers_json:
            self.answers_json = answers_json
        self.wake()

    def expire(self) -> None:
        """过期并唤醒。"""
        self.status = ElicitationStatus.EXPIRED
        self.resolved_at = time.time()
        self.wake()

    @property
    def is_resolved(self) -> bool:
        return self.status in (
            ElicitationStatus.ANSWERED_ALL,
            ElicitationStatus.ANSWERED_PARTIAL,
            ElicitationStatus.CLOSED,
            ElicitationStatus.EXPIRED,
        )

    @property
    def is_expired(self) -> bool:
        return self.status == ElicitationStatus.EXPIRED

    def to_db_dict(self) -> dict:
        """转为 elicitations 表写入 dict。"""
        return {
            "id": self.id,
            "session_id": self.session_id,
            "questions_json": self.questions_json,
            "answers_json": self.answers_json,
            "status": self.status.value,
            "trigger_source": self.trigger_source,
            "close_reason": self.close_reason,
            "platform": self.platform,
            "created_at": int(self.created_at),
            "resolved_at": int(self.resolved_at) if self.resolved_at else None,
            "expires_at": int(self.expires_at),
        }

    @classmethod
    def from_db_row(cls, row) -> ElicitationState:
        """从 elicitations 表查询结果恢复。"""
        if not isinstance(row, dict):
            row = dict(row)
        return cls(
            id=row["id"],
            session_id=row["session_id"],
            questions_json=row["questions_json"],
            answers_json=row.get("answers_json"),
            status=ElicitationStatus(row["status"]),
            trigger_source=row.get("trigger_source"),
            close_reason=row.get("close_reason"),
            platform=row.get("platform", "web"),
            created_at=float(row["created_at"]),
            resolved_at=float(row["resolved_at"]) if row.get(
                "resolved_at") else None,
            expires_at=float(row["expires_at"]),
        )


# ---- 全局注册表（内存） ----
# key = tool_use_id, value = ElicitationState
# 用于 /answer /close 唤醒对应 pipeline
_active_elicitations: dict[str, ElicitationState] = {}


def register(state: ElicitationState) -> None:
    _active_elicitations[state.id] = state


def get(tool_use_id: str) -> ElicitationState | None:
    return _active_elicitations.get(tool_use_id)


def unregister(tool_use_id: str) -> None:
    _active_elicitations.pop(tool_use_id, None)


def get_pending_for_session(db, session_id: str) -> ElicitationState | None:
    """查询 session 是否有 pending elicitation（从 DB 恢复）。"""
    row = db.query_one(
        "SELECT * FROM elicitations WHERE session_id=? AND status='pending' "
        "ORDER BY created_at DESC LIMIT 1",
        (session_id,))
    if row:
        state = ElicitationState.from_db_row(dict(row))
        # 检查是否已过期
        if state.expires_at < time.time():
            state.expire()
            _persist_status(db, state)
            return None
        return state
    return None


def _persist_status(db, state: ElicitationState) -> None:
    """更新 DB 中的状态字段。"""
    db.execute(
        "UPDATE elicitations SET status=?, answers_json=?, resolved_at=?, "
        "close_reason=? WHERE id=?",
        (state.status.value, state.answers_json,
         int(state.resolved_at) if state.resolved_at else None,
         state.close_reason, state.id))
