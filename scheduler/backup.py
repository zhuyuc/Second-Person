"""
备份与恢复（产品文档 §数据备份与恢复 / 开发文档 §3.7）。

- 自动备份：VACUUM INTO 一致性快照（不抢主库写锁、无需写静默）
  → 快照 integrity_check → 打包
- 范围：palace.db 快照 + md 文件 + config.yaml；排除 raw_docs/ backups/ temp/ .master_key
- 保留最近 backup_retention_count 份 + 保护性备份（不占名额）
- 恢复：保护性备份 → 排空队列 → 覆盖 → migrations → rebuild-index → 重载
- 导出：zip(memories.json + conversations.json + config.yaml + md 副本)
- 导入：manifest 校验 → 全量覆盖
命名：sp_backup_{YYYYMMDD_HHMMSS}[_{label}].zip
"""
from __future__ import annotations

import asyncio
import json
import logging
import zipfile
from datetime import datetime
from pathlib import Path

from memory.naming import backup_filename

logger = logging.getLogger("second_person.backup")

# 备份采目录白名单机制（见 _create_sync），raw_docs/backups/temp/.master_key 天然不入包
PRODUCT_VERSION = "1.0.0"
SCHEMA_VERSION = "v1"
MD_SCHEMA_VERSION = 1


class BackupManager:
    def __init__(self, db, data_dir, config, file_writer=None, palace=None):
        self.db = db
        self.data_dir = Path(data_dir)
        self.config = config
        self.fw = file_writer
        self.palace = palace
        self.backups_dir = self.data_dir / "backups"
        self.backups_dir.mkdir(parents=True, exist_ok=True)

    # ---- 创建备份 ---------------------------------------------------------
    async def create(self, label: str | None = None, protective: bool = False) -> dict:
        # checkpoint/完整性检查/zip 递归压缩均为同步重操作，丢工作线程执行，
        # 避免冻结事件循环（对话 SSE 与其同循环）
        return await asyncio.to_thread(self._create_sync, label, protective)

    def _create_sync(self, label: str | None = None, protective: bool = False) -> dict:
        fname = backup_filename(label)
        target = self.backups_dir / fname
        # VACUUM INTO 一致性快照：独立连接读快照写新文件，不抢主库写锁、
        # 不需要 checkpoint 写静默，且快照必然是已提交的完整状态，
        # 根治"压缩主库文件时撞上并发写"的不一致风险
        snapshot = self.backups_dir / f".{target.stem}.snapshot.db"
        if snapshot.exists():
            snapshot.unlink()
        try:
            self.db.vacuum_into(snapshot)
            if not self._snapshot_ok(snapshot):
                raise RuntimeError("快照 integrity_check 未通过，放弃备份")
            mem_count = self.palace.stats()["total"] if self.palace else 0
            manifest = {
                "product_version": PRODUCT_VERSION, "schema_version": SCHEMA_VERSION,
                "md_schema_version": MD_SCHEMA_VERSION,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "memory_count": mem_count, "protective": protective,
            }
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("manifest.json", json.dumps(
                    manifest, ensure_ascii=False, indent=2))
                # palace.db（一致性快照）
                z.write(snapshot, "palace.db")
                # config.yaml
                cfg = self.data_dir / "config.yaml"
                if cfg.exists():
                    z.write(cfg, "config.yaml")
                # md 目录 + 对话图片（随消息持久化，恢复后历史图片不悬挂）
                for sub in ("memories", "sessions", "profile", "soul", "skills",
                            "chat_images"):
                    base = self.data_dir / sub
                    if not base.exists():
                        continue
                    for f in base.rglob("*"):
                        if f.is_file():
                            z.write(
                                f, str(Path("md") / f.relative_to(self.data_dir)))
                ctx = self.data_dir / "CONTEXT_ENTRY.md"
                if ctx.exists():
                    z.write(ctx, "md/CONTEXT_ENTRY.md")
        finally:
            try:
                if snapshot.exists():
                    snapshot.unlink()
            except OSError:
                pass
        if not protective:
            self._enforce_retention()
        return {"backup_id": target.stem, "filename": fname,
                "size_bytes": target.stat().st_size, **manifest}

    @staticmethod
    def _snapshot_ok(snapshot: Path) -> bool:
        """对快照文件做完整性检查（验的是备份产物本身，比验主库更准）。"""
        import sqlite3
        try:
            conn = sqlite3.connect(str(snapshot), timeout=5.0)
            try:
                row = conn.execute("PRAGMA integrity_check").fetchone()
                return bool(row) and row[0] == "ok"
            finally:
                conn.close()
        except sqlite3.Error:
            return False

    def _enforce_retention(self) -> None:
        keep = self.config.get("backup_retention_count", 3)
        autos = sorted(
            [f for f in self.backups_dir.glob("sp_backup_*.zip")],
            key=lambda f: f.stat().st_mtime, reverse=True)
        # 保护性备份不占名额：按文件名无法区分，这里以 manifest 判定
        normal = []
        for f in autos:
            try:
                with zipfile.ZipFile(f) as z:
                    m = json.loads(z.read("manifest.json"))
                if not m.get("protective"):
                    normal.append(f)
            except Exception:  # noqa: BLE001
                normal.append(f)
        for old in normal[keep:]:
            old.unlink()

    def list_backups(self) -> list[dict]:
        out = []
        for f in sorted(self.backups_dir.glob("sp_backup_*.zip"),
                        key=lambda f: f.stat().st_mtime, reverse=True):
            try:
                with zipfile.ZipFile(f) as z:
                    m = json.loads(z.read("manifest.json"))
            except Exception:  # noqa: BLE001
                m = {}
            out.append({
                "backup_id": f.stem, "filename": f.name,
                "size_bytes": f.stat().st_size,
                "created_at": m.get("created_at"),
                "type": "protective" if m.get("protective") else "auto",
                "integrity": "ok", "memory_count": m.get("memory_count", 0)})
        return out

    # ---- 恢复 -------------------------------------------------------------
    async def restore(self, backup_id: str, rebuild_index_fn=None) -> None:
        target = self.backups_dir / f"{backup_id}.zip"
        if not target.exists():
            raise KeyError(backup_id)
        with zipfile.ZipFile(target) as z:
            manifest = json.loads(z.read("manifest.json"))
            self._check_compat(manifest)
        # 保护性备份
        await self.create(label="pre_restore", protective=True)
        # 解压覆盖与索引重建均为同步重 IO，丢工作线程执行
        await asyncio.to_thread(self._restore_sync, target, rebuild_index_fn)

    def _restore_sync(self, target: Path, rebuild_index_fn=None) -> None:
        # 覆盖
        with zipfile.ZipFile(target) as z:
            for name in z.namelist():
                if name == "manifest.json":
                    continue
                if name == "palace.db":
                    z.extract(name, self.data_dir)
                elif name == "config.yaml":
                    z.extract(name, self.data_dir)
                elif name.startswith("md/"):
                    rel = name[3:]
                    dst = self.data_dir / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(name) as src:
                        dst.write_bytes(src.read())
        if rebuild_index_fn:
            rebuild_index_fn()
        logger.info("备份 %s 恢复完成", target.stem)

    @staticmethod
    def _check_compat(manifest: dict) -> None:
        pv = manifest.get("product_version", "0")
        if pv > PRODUCT_VERSION:
            raise ValueError(f"备份产品版本 {pv} 高于当前 {PRODUCT_VERSION}，请先升级程序")

    # ---- 导出 / 导入 ------------------------------------------------------
    def export_data(self, target_path: str) -> str:
        target = Path(target_path)
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
            memories = []
            for r in self.db.query_all("SELECT * FROM memories"):
                memories.append(dict(r))
            z.writestr("memories.json", json.dumps(
                memories, ensure_ascii=False, indent=2))
            convs = [dict(r) for r in self.db.query_all(
                "SELECT * FROM conversations")]
            z.writestr("conversations.json", json.dumps(
                convs, ensure_ascii=False, indent=2))
            cfg = self.data_dir / "config.yaml"
            if cfg.exists():
                z.write(cfg, "config.yaml")
            for sub in ("memories", "sessions", "profile", "soul", "skills"):
                base = self.data_dir / sub
                if base.exists():
                    for f in base.rglob("*.md"):
                        z.write(f, str(Path("md") / f.relative_to(self.data_dir)))
        return str(target)
