"""默认 + .gitignore 合并的忽略规则（v5 §六 A6）。

自实现一个简化版 .gitignore 匹配（避免引入 pathspec 依赖）。
只支持 gitignore 常见语法子集：
- 星号通配（* ? [] 用 fnmatch）
- 目录后缀 /
- 前置 ! 反选（否定）
- 前置 / 表示项目根锚定
- 空行 / # 注释
"""
from __future__ import annotations

import fnmatch
from pathlib import Path

DEFAULT_IGNORE = [
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".pytest_cache", "dist", "build", "target",
    ".next", ".nuxt", ".idea", ".vscode", ".DS_Store",
]


class IgnoreMatcher:
    """项目根级忽略匹配器。构造成本可忽略；请求路径缓存。"""

    def __init__(self, project_root: Path, extra: list[str] | None = None):
        self.root = project_root
        patterns: list[str] = list(DEFAULT_IGNORE)
        # 读项目根的 .gitignore（仅一份，不递归读子目录）
        gitignore = project_root / ".gitignore"
        if gitignore.is_file():
            try:
                for line in gitignore.read_text(
                        encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    patterns.append(line)
            except OSError:
                pass
        if extra:
            patterns.extend(extra)
        # 拆成正/负两组，保留顺序
        self._rules = [(p.lstrip("!"), p.startswith("!")) for p in patterns]

    def match(self, path: Path) -> bool:
        """path 相对 project_root 的路径是否被忽略。目录判定用 posix 风格路径。"""
        try:
            rel = path.relative_to(self.root)
        except ValueError:
            return False
        rel_posix = rel.as_posix()
        parts = rel.parts
        matched = False
        for pat, negated in self._rules:
            hit = False
            # 目录后缀
            is_dir_pat = pat.endswith("/")
            core = pat.rstrip("/")
            # 根锚定
            anchored = core.startswith("/")
            core = core.lstrip("/")
            if anchored:
                hit = fnmatch.fnmatch(rel_posix, core) or rel_posix.startswith(core + "/")
            else:
                # 任意层级下的段匹配
                for seg in parts:
                    if fnmatch.fnmatch(seg, core):
                        hit = True
                        break
                if not hit:
                    hit = fnmatch.fnmatch(rel_posix, core) \
                        or fnmatch.fnmatch(rel_posix, "*/" + core) \
                        or fnmatch.fnmatch(rel_posix, "*/" + core + "/*")
            if hit:
                if is_dir_pat and not path.is_dir():
                    continue
                matched = not negated
        return matched
