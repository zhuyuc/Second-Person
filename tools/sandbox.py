"""
文件与命令沙箱（产品文档 §文件与命令类工具的边界 / 开发文档 §6.3）。

- file_read/write 可访问范围限定 workspace（默认 data/workspace/，可追加白名单）
- 路径先 realpath 规范化再校验，拒绝 .. 穿越与指向白名单外的符号链接
- shell_exec 工作目录固定 workspace；高危命令黑名单；超时 30s；剥离敏感环境变量
- 违规调用直接拒绝并把原因返回给 LLM，不直接终止本轮
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# 高危命令黑名单（HIGH_RISK_COMMANDS）
HIGH_RISK_PATTERNS = [
    r"\brm\s+-rf\b", r"\bmkfs\b", r"\bdd\b", r"\bshutdown\b", r"\breboot\b",
    r"\bchmod\s+777\b", r":\(\)\s*\{", r"\bmv\s+/\b", r">\s*/dev/sd",
    r"\bformat\b", r"\bdel\s+/[fsq]", r"Remove-Item", r"\bfdisk\b",
]

SENSITIVE_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.I)


class SandboxError(PermissionError):
    pass


class Sandbox:
    def __init__(self, workspace: str | Path, whitelist: list[str] | None = None):
        self.workspace = Path(workspace).resolve()
        self.whitelist = [Path(p).resolve() for p in (whitelist or [])]
        self.workspace.mkdir(parents=True, exist_ok=True)

    def _roots(self) -> list[Path]:
        return [self.workspace, *self.whitelist]

    def resolve_path(self, path: str) -> Path:
        """规范化并校验路径落在白名单内，否则抛 SandboxError。"""
        p = Path(path)
        if not p.is_absolute():
            p = self.workspace / p
        real = p.resolve()
        for root in self._roots():
            try:
                real.relative_to(root)
                return real
            except ValueError:
                continue
        raise SandboxError(
            f"路径越界，拒绝访问：{path}（允许根目录 {[str(r) for r in self._roots()]}）")

    def check_command(self, cmd: str) -> None:
        for pat in HIGH_RISK_PATTERNS:
            if re.search(pat, cmd, re.I):
                raise SandboxError(f"命令命中高危黑名单，拒绝执行：{cmd}")

    def clean_env(self) -> dict[str, str]:
        """剥离所有含 KEY/TOKEN/SECRET 的环境变量。"""
        return {k: v for k, v in os.environ.items() if not SENSITIVE_ENV.search(k)}
