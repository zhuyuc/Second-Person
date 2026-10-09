"""FTS5 查询辅助：把用户输入转成安全的 MATCH 表达式。

历史：原实现在 memory/retriever.py 内私有；随会话搜索接入而抽公共，
避免 agent/ 与 memory/ 交叉依赖。

conversations_fts 使用 tokenize='trigram'：SQLite 规定 MATCH 串
不足 3 个 unicode 字符时永远不命中，短中文（如「你好」）需走 LIKE 回退。
"""
from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"[\w一-鿿]+")
# trigram tokenizer 的硬下限（按 unicode 字符计，不是字节）
TRIGRAM_MIN_CHARS = 3


def fts_escape(query: str, max_tokens: int = 8) -> str:
    """将用户查询转为 FTS5 安全的 MATCH 表达式。

    - 提取字母数字与 CJK token；忽略标点/操作符，防止意外触发 FTS5 语法
    - 每个 token 单独加双引号（防止内部字符被识别为语法），再以 AND 连接
    - 限制最多 max_tokens 个 token，防止超长查询打爆解析
    """
    tokens = _TOKEN_RE.findall(query or "")
    if not tokens:
        return ""
    return " AND ".join(f'"{t}"' for t in tokens[:max_tokens])


def fts_meaningful_len(query: str) -> int:
    """字母数字 + CJK 的有效字符数（去掉标点空白）。"""
    return sum(len(t) for t in _TOKEN_RE.findall(query or ""))


def fts_trigram_too_short(query: str) -> bool:
    """trigram 下是否因过短而必然 MATCH 失败。"""
    n = fts_meaningful_len(query)
    return 0 < n < TRIGRAM_MIN_CHARS


def like_escape(text: str) -> str:
    """LIKE … ESCAPE '\\' 用的模式转义。"""
    return (text or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
