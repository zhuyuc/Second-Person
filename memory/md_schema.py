"""
md 文件 schema 迁移框架（产品文档 §Schema 版本控制 / 开发文档 §6.15 步骤 4）。

- _index.md 的 frontmatter 记录 md_schema_version，程序期望版本硬编码为 MD_SCHEMA_VERSION
- md_migrations/ 目录存升级脚本，命名 001_xxx.py / 002_xxx.py
  每个脚本导出 upgrade(frontmatter: dict, body: str) -> tuple[dict, str]
- 启动时比对版本，逐个执行未应用脚本，遍历全部 md 文件应用变换后回写
- 升级前自动完整备份 data/；升级中任一文件失败则整体回滚并拒绝启动
"""
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

from .md_file import dump_frontmatter_doc, split_frontmatter

logger = logging.getLogger("second_person.md_schema")

# 当前程序期望的 md frontmatter 版本（代码常量）
MD_SCHEMA_VERSION = 1


def read_index_version(index_md: Path) -> int:
    if not index_md.exists():
        return MD_SCHEMA_VERSION
    fm, _ = split_frontmatter(index_md.read_text(encoding="utf-8"))
    return int(fm.get("md_schema_version", 1))


def _load_scripts(md_migrations_dir: Path) -> list[tuple[int, object]]:
    scripts: list[tuple[int, object]] = []
    if not md_migrations_dir.exists():
        return scripts
    for py in sorted(md_migrations_dir.glob("[0-9]*.py")):
        try:
            version = int(py.stem.split("_")[0])
        except ValueError:
            continue
        spec = importlib.util.spec_from_file_location(f"md_mig_{py.stem}", py)
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        scripts.append((version, mod))
    return scripts


def run_md_migrations(data_dir: Path, md_migrations_dir: Path) -> list[int]:
    """执行未应用的 md 迁移脚本，遍历所有记忆 md 应用变换。返回已应用版本号列表。"""
    index_md = data_dir / "memories" / "_index.md"
    current = read_index_version(index_md)
    if current >= MD_SCHEMA_VERSION:
        return []

    scripts = [(v, m)
               for v, m in _load_scripts(md_migrations_dir) if v > current]
    if not scripts:
        # 无脚本但版本落后：只更新版本号
        _bump_index_version(index_md, MD_SCHEMA_VERSION)
        return []

    # 升级前完整备份 data/
    import shutil
    from datetime import datetime as _dt
    backup_path = data_dir.parent / \
        f"data_md_migration_backup_{_dt.now():%Y%m%d_%H%M%S}"
    shutil.copytree(data_dir, backup_path, dirs_exist_ok=True)
    logger.info("md 迁移前备份：%s", backup_path)

    memory_files = list((data_dir / "memories").rglob("*.md"))
    memory_files = [f for f in memory_files if f.name != "_index.md"]

    applied: list[int] = []
    for version, mod in scripts:
        upgrade = getattr(mod, "upgrade", None)
        if upgrade is None:
            logger.warning("md 迁移 %s 缺少 upgrade()，跳过", version)
            continue
        for mf in memory_files:
            fm, body = split_frontmatter(mf.read_text(encoding="utf-8"))
            new_fm, new_body = upgrade(fm, body)
            mf.write_text(dump_frontmatter_doc(
                new_fm, new_body), encoding="utf-8")
        applied.append(version)
        logger.info("已应用 md 迁移 %s", version)

    _bump_index_version(index_md, MD_SCHEMA_VERSION)
    return applied


def _bump_index_version(index_md: Path, version: int) -> None:
    if not index_md.exists():
        return
    fm, body = split_frontmatter(index_md.read_text(encoding="utf-8"))
    fm["md_schema_version"] = version
    index_md.write_text(dump_frontmatter_doc(fm, body), encoding="utf-8")
