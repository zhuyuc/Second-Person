"""Session-level sandbox policy — three modes, symmetric across project state.

档位定义（v6 沙箱统一化）：
  read-only          - 只查不改
  workspace-write    - 可写工作目录（默认）
  danger-full-access - 全盘可写 + shell

「有无项目」不影响档位是否可选，只影响 work_root 的锚点：
  项目会话 → work_root = project_root
  非项目会话 → work_root = legacy_workspace（+ whitelist）

策略优先级：
  session_policy_events 最新 sandbox_mode_change   （用户显式覆盖，最高）
  > sessions.sandbox_mode 字段
  > 项目档 projects.sandbox_mode（仅项目会话）
  > 默认 workspace-write
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("second_person.fs.policy")

# 唯一合法档位表（对所有会话通用）
VALID_MODES = ("read-only", "workspace-write", "danger-full-access")
# 已废弃档位 → 新档位映射（老数据/事件流兼容）
LEGACY_MODE_ALIASES = {
    "legacy-workspace": "workspace-write",
}
DEFAULT_MODE = "workspace-write"


def normalize_mode(mode: str | None) -> str:
    """把老档位归一到新枚举；未知值降级为 read-only 保守。"""
    if not mode:
        return DEFAULT_MODE
    if mode in VALID_MODES:
        return mode
    if mode in LEGACY_MODE_ALIASES:
        return LEGACY_MODE_ALIASES[mode]
    return "read-only"


@dataclass(frozen=True)
class SandboxPolicy:
    mode: str                             # 三档之一
    project_id: str | None                # None = 无项目会话
    project_root: Path | None             # 有项目 → 项目根；无项目 → None
    writable_roots: tuple[Path, ...] = field(default_factory=tuple)
    read_roots: tuple[Path, ...] = field(default_factory=tuple)
    shell_enabled: bool = False
    shell_cwd: Path | None = None

    def is_writable(self) -> bool:
        return self.mode != "read-only"

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "project_id": self.project_id,
            "project_root": str(self.project_root) if self.project_root else None,
            "writable_roots": [str(p) for p in self.writable_roots],
            "read_roots": [str(p) for p in self.read_roots],
            "shell_enabled": self.shell_enabled,
        }


class PolicyStore:
    """从会话 + 项目 + 事件流解析当前策略。永不抛异常；缺表/缺项目降级 read-only。"""

    def __init__(self, db, projects_store, config, *,
                 legacy_workspace: Path,
                 legacy_whitelist: list[Path] | None = None,
                 spill_read_root: Path | None = None):
        self.db = db
        self.projects = projects_store
        self.config = config
        self.legacy_workspace = Path(legacy_workspace).resolve()
        self.legacy_whitelist = [Path(p).resolve()
                                  for p in (legacy_whitelist or [])]
        # 工具结果溢写目录：只读并入 read_roots，供 fs_read/fs_grep 续读
        self.spill_read_root = (
            Path(spill_read_root).resolve() if spill_read_root else None)

    def resolve(self, session_id: str) -> SandboxPolicy:
        row = self.db.query_one(
            "SELECT project_id, sandbox_mode FROM sessions WHERE session_id=?",
            (session_id,))
        if not row:
            return self._policy(project=None, project_id=None, mode=DEFAULT_MODE)
        project_id = row["project_id"]
        session_override = row["sandbox_mode"]

        # 项目对象（如果有）
        proj = None
        if project_id and self.projects:
            proj = self.projects.get(project_id)
            if proj and proj.status != "active":
                # 项目丢失/归档 → 退化为只读，避免误伤
                return self._policy(project=proj, project_id=project_id,
                                    mode="read-only")

        # 优先级：session_policy_events > sessions.sandbox_mode > projects.sandbox_mode > 默认
        evt_mode = self._latest_event_mode(session_id)
        if evt_mode:
            return self._policy(project=proj, project_id=project_id,
                                mode=normalize_mode(evt_mode))
        if session_override:
            return self._policy(project=proj, project_id=project_id,
                                mode=normalize_mode(session_override))
        if proj and proj.sandbox_mode:
            return self._policy(project=proj, project_id=project_id,
                                mode=normalize_mode(proj.sandbox_mode))
        return self._policy(project=proj, project_id=project_id, mode=DEFAULT_MODE)

    # ------------------------------------------------------------------
    def _latest_event_mode(self, session_id: str) -> str | None:
        row = self.db.query_one(
            "SELECT payload FROM session_policy_events "
            "WHERE session_id=? AND event_type='sandbox_mode_change' "
            "ORDER BY id DESC LIMIT 1",
            (session_id,))
        if not row:
            return None
        try:
            return json.loads(row["payload"]).get("mode")
        except (json.JSONDecodeError, AttributeError):
            return None

    def _policy(self, *, project, project_id: str | None,
                mode: str) -> SandboxPolicy:
        """三档 × 有无项目 组成 6 种表格化输出，无特殊分支。

        工作根锚点：
            有项目 → project.path
            无项目 → legacy_workspace(+whitelist)
        """
        mode = normalize_mode(mode)
        project_root: Path | None = None
        # work_roots：archive 语义下项目根仍需暴露给只读，避免"归档即失联"
        if project is not None:
            project_root = Path(project.path).resolve()
            work_roots: tuple[Path, ...] = (project_root,)
        else:
            work_roots = (self.legacy_workspace, *self.legacy_whitelist)

        read_roots = self._with_spill_root(work_roots)
        if mode == "read-only":
            return SandboxPolicy(
                mode=mode, project_id=project_id, project_root=project_root,
                writable_roots=(), read_roots=read_roots,
                shell_enabled=False)
        if mode == "workspace-write":
            return SandboxPolicy(
                mode=mode, project_id=project_id, project_root=project_root,
                writable_roots=work_roots, read_roots=read_roots,
                shell_enabled=False)
        if mode == "danger-full-access":
            # 全盘：writable/read 空集 → 工具层判定跳围栏
            shell_cwd = project_root or self.legacy_workspace
            return SandboxPolicy(
                mode=mode, project_id=project_id, project_root=project_root,
                writable_roots=(), read_roots=(),
                shell_enabled=True, shell_cwd=shell_cwd)
        # normalize_mode 已归一，这里不会走到；兜底 read-only
        logger.warning("未知沙箱档位 %s，降级 read-only", mode)
        return SandboxPolicy(
            mode="read-only", project_id=project_id, project_root=project_root,
            writable_roots=(), read_roots=read_roots)

    def _with_spill_root(self, roots: tuple[Path, ...]) -> tuple[Path, ...]:
        if not self.spill_read_root:
            return roots
        if self.spill_read_root in roots:
            return roots
        return (*roots, self.spill_read_root)
