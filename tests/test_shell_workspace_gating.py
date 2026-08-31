"""shell_exec 二次 gate 覆盖（v6 沙箱统一化）。

fs_* 家族历史上都有 workspace_resolver 的执行层围栏（writable_roots / read_roots），
只有 shell_exec 缺这一层——所以 read-only / workspace-write 档位下拿到 schema 后
仍然能真的下发命令执行。本次接入 workspace_resolver 后 shell_exec 走三档：
    read-only          → 拒（返回 SANDBOX_DENIED 结构，不抛异常）
    workspace-write    → 拒（同上）
    danger-full-access → 放行，cwd 由 policy 决定
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace


def _make_registry_with_shell(workspace: Path):
    """构造一个只挂 shell_exec 依赖足够小的 registry。"""
    from tools.base import ToolRegistry
    from tools.builtin import register_builtins
    from tools.sandbox import Sandbox

    class _NoopStore:
        def save(self, *a, **kw): return {"id": "x", "path": "x"}
        def top_hybrid(self, *a, **kw): return []
        def get(self, mid): return None

    class _NoopRetriever:
        pass

    class _NoopFileWriter:
        pass

    class _NoopConfig:
        def get(self, k, default=None): return default
        def get_raw(self, k, default=None): return default

    workspace.mkdir(parents=True, exist_ok=True)
    registry = ToolRegistry()
    register_builtins(
        registry,
        palace=_NoopStore(),
        retriever=_NoopRetriever(),
        file_writer=_NoopFileWriter(),
        sandbox=Sandbox(workspace),
        data_dir=workspace.parent,
        config=_NoopConfig(),
    )
    return registry


def _shell_tool(workspace: Path):
    return _make_registry_with_shell(workspace).get("shell_exec")


def _ws_ctx(shell_enabled: bool, shell_cwd: Path | None,
             mode: str = "workspace-write"):
    """Fake WorkspaceContext with only the fields shell_exec reads."""
    return SimpleNamespace(
        shell_enabled=shell_enabled,
        shell_cwd=shell_cwd,
        sandbox_mode=mode,
        session_id="__shell_test__",
    )


# -------------------------------------------------- schema wiring ----------

def test_shell_exec_marked_needs_workspace(tmp_path: Path):
    """spec.needs_workspace 打开后 ToolExecutor 会注入 _ws_ctx。"""
    tool = _shell_tool(tmp_path / "ws")
    assert tool.spec.needs_workspace is True


# -------------------------------------------------- runtime gating --------

def test_shell_exec_read_only_mode_is_denied(tmp_path: Path):
    tool = _shell_tool(tmp_path / "ws")
    ctx = _ws_ctx(shell_enabled=False, shell_cwd=None, mode="read-only")
    result = asyncio.run(tool.run(cmd="echo hi", _ws_ctx=ctx))
    assert result["returncode"] == -1
    assert "SANDBOX_DENIED" in result["stderr"]
    assert "read-only" in result["stderr"]


def test_shell_exec_workspace_write_mode_is_denied(tmp_path: Path):
    """workspace-write 档位不允许 shell（跟 fs 写入分离）。"""
    tool = _shell_tool(tmp_path / "ws")
    ctx = _ws_ctx(shell_enabled=False, shell_cwd=None, mode="workspace-write")
    result = asyncio.run(tool.run(cmd="echo hi", _ws_ctx=ctx))
    assert result["returncode"] == -1
    assert "SANDBOX_DENIED" in result["stderr"]


def test_shell_exec_danger_mode_runs(tmp_path: Path):
    """danger-full-access：放行，cwd 走 ctx.shell_cwd。"""
    ws = tmp_path / "ws"
    tool = _shell_tool(ws)
    ctx = _ws_ctx(shell_enabled=True, shell_cwd=ws,
                  mode="danger-full-access")
    import sys
    cmd = ("python -c \"print('ok')\"" if sys.platform == "win32"
           else "echo ok")
    result = asyncio.run(tool.run(cmd=cmd, _ws_ctx=ctx))
    assert result["returncode"] == 0
    assert "ok" in result["stdout"]


def test_shell_exec_without_ctx_falls_back_to_sandbox(tmp_path: Path):
    """无 ctx 时（老调用路径）退回 sandbox 全局单例，保留历史行为。"""
    ws = tmp_path / "ws"
    tool = _shell_tool(ws)
    import sys
    cmd = ("python -c \"print('legacy')\"" if sys.platform == "win32"
           else "echo legacy")
    result = asyncio.run(tool.run(cmd=cmd))
    assert result["returncode"] == 0
    assert "legacy" in result["stdout"]


def test_shell_exec_denied_message_carries_mode(tmp_path: Path):
    """拒绝消息里包含当前档位，方便 UI 展示与调试。"""
    tool = _shell_tool(tmp_path / "ws")
    for mode in ("read-only", "workspace-write"):
        ctx = _ws_ctx(shell_enabled=False, shell_cwd=None, mode=mode)
        result = asyncio.run(tool.run(cmd="echo x", _ws_ctx=ctx))
        assert mode in result["stderr"]
