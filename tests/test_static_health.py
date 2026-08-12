"""静态健康门禁：pyflakes 全量扫描后端包，零容忍。

背景（2026-08 后端代码健康度优化）：曾出现 GapResult 未导入、
staticmethod 内引用 self、_Trace.end 缺参等静态可检出的运行时 Bug，
本门禁保证同类问题（未定义名、参数错误、缺失占位符、死导入等）
不再进入主干。含 noqa 注释的行视为刻意保留（如预留导入），跳过。
"""
import ast
import subprocess
import sys
from pathlib import Path

import pytest

checker = pytest.importorskip("pyflakes.checker")

BACKEND_DIRS = ["agent", "app", "connectors", "gateway", "infrastructure",
                "memory", "langfuse/integration", "plugins", "scheduler",
                "soul", "tools", "user_profile"]
ROOT = Path(__file__).resolve().parent.parent
_TIME_NOW_ALLOWED = {
    "infrastructure/timeutil.py",          # 统一时间源本身
    "langfuse/integration/tracer.py",   # Langfuse 需要 aware ISO 时间戳
    "tools/builtin.py",                   # datetime_now 工具支持用户指定时区
}


def _iter_py_files():
    for d in BACKEND_DIRS:
        for p in (ROOT / d).rglob("*.py"):
            if "__pycache__" in p.parts or "venv" in p.parts:
                continue
            yield p
    yield ROOT / "start.py"


def test_pyflakes_zero_issues():
    problems = []
    for path in _iter_py_files():
        src = path.read_text(encoding="utf-8")
        lines = src.splitlines()
        try:
            tree = ast.parse(src, filename=str(path))
        except SyntaxError as e:
            problems.append(
                f"{path.relative_to(ROOT)}:{e.lineno}: SyntaxError: {e.msg}")
            continue
        w = checker.Checker(tree, str(path))
        for msg in w.messages:
            line_text = lines[msg.lineno -
                              1] if 0 < msg.lineno <= len(lines) else ""
            if "noqa" in line_text:
                continue
            problems.append(
                f"{path.relative_to(ROOT)}:{msg.lineno}: "
                f"{msg.message % msg.message_args}")
    assert not problems, "pyflakes 静态门禁未通过：\n" + "\n".join(problems)


def test_no_direct_datetime_now_in_backend():
    """业务代码统一使用 infrastructure.timeutil，避免本地时区漂移。"""
    patterns = ("datetime.now(", "_dt.now(", "datetime('now'")
    problems = []
    for path in _iter_py_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in _TIME_NOW_ALLOWED:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if any(p in line for p in patterns):
                problems.append(f"{rel}:{lineno}: 禁止直接使用本地 now：{line.strip()}")
    assert not problems, "时间规范门禁未通过：\n" + "\n".join(problems)


def test_llm_calls_have_explicit_source():
    """所有 LLM 调用必须显式标记 source，保证 token_usage 与 Langfuse 可追踪。"""
    methods = {"chat", "stream", "function_call"}
    problems = []
    for path in _iter_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in methods):
                continue
            receiver = node.func.value
            is_llm = ((isinstance(receiver, ast.Name) and receiver.id == "llm")
                      or (isinstance(receiver, ast.Attribute)
                          and receiver.attr == "llm"))
            if is_llm and not any(k.arg == "source" for k in node.keywords):
                problems.append(
                    f"{rel}:{node.lineno}: llm.{node.func.attr} 缺少 source 参数")
    assert not problems, "LLM source 门禁未通过：\n" + "\n".join(problems)


def test_vulture_no_high_confidence_dead_code():
    pytest.importorskip("vulture")
    cmd = [sys.executable, "-m", "vulture", *BACKEND_DIRS, "start.py",
           "--min-confidence", "80"]
    result = subprocess.run(cmd, cwd=ROOT, text=True,
                            capture_output=True, check=False)
    assert result.returncode == 0, (
        "vulture 死代码门禁未通过：\n" + result.stdout + result.stderr)
