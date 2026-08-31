"""fs_observations 版本乐观锁（v5 §六 6.5）。

read 成功后 upsert (session_id, target_key, version)；
write/edit 时对比 expected_version（客户端传入）与文件当前 version + 会话观察记录，
不匹配则返 FS_STALE_VERSION 或 FS_NOT_OBSERVED。
"""
from __future__ import annotations

from infrastructure.timeutil import now_cst


class FsObservationStore:
    """薄封装 fs_observations 表读写。所有档位均记录 read 观察；
    fs_write/fs_edit 统一以此表 + expected_version 做乐观锁。"""

    def __init__(self, db):
        self.db = db

    def record(self, session_id: str, target_key: str, version: str) -> None:
        now = now_cst().isoformat(timespec="seconds")
        self.db.execute(
            "INSERT INTO fs_observations(session_id, target_key, version, observed_at) "
            "VALUES(?,?,?,?) ON CONFLICT(session_id, target_key) DO UPDATE SET "
            "version=excluded.version, observed_at=excluded.observed_at",
            (session_id, target_key, version, now))

    def get(self, session_id: str, target_key: str) -> str | None:
        row = self.db.query_one(
            "SELECT version FROM fs_observations WHERE session_id=? AND target_key=?",
            (session_id, target_key))
        return row["version"] if row else None

    def invalidate_target(self, target_key: str) -> int:
        """FileWatcher 侦测外部修改后，使所有会话对该 target 的 observation 失效。"""
        cur = self.db.execute(
            "DELETE FROM fs_observations WHERE target_key=?", (target_key,))
        return cur.rowcount if cur else 0

    def invalidate_session(self, session_id: str) -> int:
        cur = self.db.execute(
            "DELETE FROM fs_observations WHERE session_id=?", (session_id,))
        return cur.rowcount if cur else 0
