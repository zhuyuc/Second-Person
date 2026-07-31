"""Prompt 外部化加载器（项目"md 即一等公民"架构）。

系统 prompt 统一存放于各模块的 prompts/ 目录下 .md 文件（如
agent/prompts/intent_system.md），本模块负责按名加载与渲染。

设计要点：
- 占位符用 string.Template 的 ${var} 语法，绝不用 str.format——因为多数
  prompt 内嵌 JSON 示例的 { }，str.format 会直接报错。
- 无变量的 prompt 用 load_raw 原样返回；需注入变量的用 render。
- 按文件 mtime 缓存，外部编辑（dev 热改）后自动失效重载。
- 文件缺失/读失败直接抛异常（fail-fast），不静默降级。
"""
from __future__ import annotations

import logging
from pathlib import Path
from string import Template
from threading import RLock

logger = logging.getLogger("second_person.prompt")

# 项目根目录：infrastructure/ 的上一级
_ROOT = Path(__file__).resolve().parent.parent


class PromptLoader:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else _ROOT
        self._cache: dict[str, tuple[float, str]] = {}
        self._lock = RLock()

    def _path(self, name: str) -> Path:
        # name 形如 "agent/prompts/intent_system"（不含扩展名）
        return self.root / (name + ".md")

    def load_raw(self, name: str) -> str:
        """原样加载 prompt 文本（去除首尾空白）。文件缺失抛 FileNotFoundError。"""
        p = self._path(name)
        with self._lock:
            try:
                mtime = p.stat().st_mtime
            except OSError as e:
                raise FileNotFoundError(f"prompt 文件缺失或不可读: {p}") from e
            cached = self._cache.get(name)
            if cached and cached[0] == mtime:
                return cached[1]
            text = p.read_text(encoding="utf-8").strip()
            self._cache[name] = (mtime, text)
            return text

    def render(self, name: str, **kwargs: object) -> str:
        """加载并用 ${var} 占位符渲染。无 kwargs 时等价 load_raw。

        使用 safe_substitute：缺失变量原样保留、多余的 $ 不会抛错。
        """
        raw = self.load_raw(name)
        if not kwargs:
            return raw
        return Template(raw).safe_substitute(**kwargs)


# 模块级单例：各处 `from infrastructure.prompt_loader import PROMPTS`
PROMPTS = PromptLoader()
