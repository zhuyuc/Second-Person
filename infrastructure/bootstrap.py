"""
目录初始化 —— 首次启动的 data/ 目录结构与默认文件生成（幂等）。

对齐产品文档 §首次启动的目录初始化 与 开发文档 §6.15 步骤 2：
- 检测到 data/ 不存在或不完整时，按固定清单创建全部子目录
- 生成默认 config.yaml、空 _index.md、CONTEXT_ENTRY.md
- 逻辑幂等：已存在的目录与文件不覆盖
- SQL 建表与 seed 由 migrations 执行（见 db.py），此处只负责文件系统骨架
"""
from __future__ import annotations

from pathlib import Path

import yaml

from .config_manager import default_config

# data/ 下固定子目录清单（产品文档明确列举）
DATA_SUBDIRS = [
    "memories",
    "memories/_archived",
    "memories/_conflicts",
    "sessions",
    "profile",
    "soul",
    "soul/SOUL_STYLE_HISTORY",
    "skills",
    "raw_docs",
    "backups",
    "temp",
    "temp/attachments",
    "workspace",
]

# 空 _index.md 初始内容（frontmatter 记录 md_schema_version 与计数口径）
EMPTY_INDEX_MD = """---
total: 0
active: 0
stable: 0
stale: 0
archived: 0
important: 0
link_count: 0
md_schema_version: 1
---
"""

EMPTY_SKILLS_INDEX_MD = """---
total: 0
active: 0
draft: 0
archived: 0
pending_confirm: []
md_schema_version: 1
---
## active
## draft
"""

# CONTEXT_ENTRY.md 固定四段骨架（开发文档 §6.19）
INITIAL_CONTEXT_ENTRY = """# Second Person Context Entry

## 系统状态
- 记忆总数：0 条
- 待处理：0 条矛盾记忆
- SOUL_STYLE 版本：未初始化

## 阅读顺序
1. SOUL_CORE → 核心人格（必读）
2. SOUL_STYLE → 当前风格（必读）
3. 意识提示 → 重要记忆关键词（必读）
4. 当前会话摘要 → 继续已有对话时加载（按需）
5. 记忆检索 → 根据用户输入触发三级联动（按需）
6. 工具描述 → 根据意图判断加载哪些（按需）

## 近期变化摘要

## 待处理
"""


def init_data_dir(data_dir: str | Path) -> dict[str, bool]:
    """幂等创建 data/ 目录结构与默认文件。返回各项是否为新建。"""
    data = Path(data_dir)
    created: dict[str, bool] = {}

    for sub in DATA_SUBDIRS:
        p = data / sub
        created[sub] = not p.exists()
        p.mkdir(parents=True, exist_ok=True)

    # 默认 config.yaml
    cfg_path = data / "config.yaml"
    if not cfg_path.exists():
        cfg_path.write_text(
            yaml.safe_dump(default_config(),
                           allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        created["config.yaml"] = True

    # 空 _index.md（记忆全局目录）
    idx_path = data / "memories" / "_index.md"
    if not idx_path.exists():
        idx_path.write_text(EMPTY_INDEX_MD, encoding="utf-8")
        created["memories/_index.md"] = True

    # 技能 _index.md
    skills_idx = data / "skills" / "_index.md"
    if not skills_idx.exists():
        skills_idx.write_text(EMPTY_SKILLS_INDEX_MD, encoding="utf-8")
        created["skills/_index.md"] = True

    # CONTEXT_ENTRY.md
    ctx_path = data / "CONTEXT_ENTRY.md"
    if not ctx_path.exists():
        ctx_path.write_text(INITIAL_CONTEXT_ENTRY, encoding="utf-8")
        created["CONTEXT_ENTRY.md"] = True

    return created
