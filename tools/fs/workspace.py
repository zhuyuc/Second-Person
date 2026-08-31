"""WorkspaceResolver：session_id → WorkspaceContext（v5 §六 6.4）。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .policy import PolicyStore, SandboxPolicy


@dataclass(frozen=True)
class WorkspaceContext:
    """一次工具调用的上下文快照。所有 fs_* 工具接受该对象。"""
    session_id: str
    project_id: str | None
    project_root: Path | None
    sandbox_mode: str
    writable_roots: tuple[Path, ...]
    read_roots: tuple[Path, ...]
    shell_enabled: bool
    shell_cwd: Path | None

    @classmethod
    def from_policy(cls, session_id: str, policy: SandboxPolicy) -> "WorkspaceContext":
        return cls(
            session_id=session_id,
            project_id=policy.project_id,
            project_root=policy.project_root,
            sandbox_mode=policy.mode,
            writable_roots=policy.writable_roots,
            read_roots=policy.read_roots,
            shell_enabled=policy.shell_enabled,
            shell_cwd=policy.shell_cwd,
        )

    def cwd(self) -> Path:
        """相对路径的默认基准。有项目 → 项目根；否则 → legacy workspace。"""
        if self.project_root:
            return self.project_root
        if self.writable_roots:
            return self.writable_roots[0]
        return Path.cwd()

    def is_danger(self) -> bool:
        return self.sandbox_mode == "danger-full-access"

    def is_readonly(self) -> bool:
        return self.sandbox_mode == "read-only"


class WorkspaceResolver:
    """会话上下文解析器。ToolExecutor 每次工具调用前调 resolve(sid)。"""

    def __init__(self, policy_store: PolicyStore):
        self.policy = policy_store

    def resolve(self, session_id: str) -> WorkspaceContext:
        return WorkspaceContext.from_policy(
            session_id, self.policy.resolve(session_id))
