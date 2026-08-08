"""静态健康门禁：pyflakes 全量扫描后端包，零容忍。

背景（2026-08 后端代码健康度优化）：曾出现 GapResult 未导入、
staticmethod 内引用 self、_Trace.end 缺参等静态可检出的运行时 Bug，
本门禁保证同类问题（未定义名、参数错误、缺失占位符、死导入等）
不再进入主干。含 noqa 注释的行视为刻意保留（如预留导入），跳过。
"""
import ast
from pathlib import Path

import pytest

checker = pytest.importorskip("pyflakes.checker")

BACKEND_DIRS = ["agent", "app", "connectors", "gateway", "infrastructure",
                "memory", "observability_langfuse", "plugins", "scheduler",
                "soul", "tools", "user_profile"]
ROOT = Path(__file__).resolve().parent.parent


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
