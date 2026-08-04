"""
本地目录全域接入（FolderScanner）—— 个人知识接入核心。

- 用户配置一个或多个本地目录，系统按 local_dir_scan_interval_hours 间隔扫描
- 指纹（size + mtime_ns）增量检测：仅新增/变更文件进入导入管线，未变文件零开销
- 文件复制进 raw_docs（保持不可变素材语义），source=local_dir，可参与 --recompile 重建
- 提炼复用 IngestManager.ingest_file（本地目录导入恒为静默模式，不预览不弹确认）
- 单轮处理数量受 local_dir_max_files_per_scan 上限约束，超出留待下轮
- 扫描协程在调度器/手动 API 中执行（await 释放事件循环），asyncio 锁防并发重扫
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from infrastructure.timeutil import now_cst
from scheduler.ingest import IMAGE_EXTS, MAX_FILE_MB

logger = logging.getLogger("second_person.folder_scan")

# 跳过目录：隐藏目录 + 常见依赖/构建/虚拟环境（防扫描进 node_modules 等海量噪音）
SKIP_DIR_NAMES = {
    ".git", ".hg", ".svn", ".idea", ".vscode", "__pycache__",
    "node_modules", "venv", ".venv", "env", ".env",
    "dist", "build", "site-packages", "target", ".gradle", ".next",
}

# 支持的扩展名：文本类 + PDF/DOCX（与 ingest.extract_text 支持集对齐）
SUPPORTED_EXTS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log",
    ".yaml", ".yml", ".xml", ".html", ".htm", ".ini", ".conf", ".toml",
    ".py", ".js", ".ts", ".jsx", ".tsx", ".vue", ".java", ".go", ".rs",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".sh", ".sql",
    ".css", ".scss", ".pdf", ".docx",
}

# 单轮候选收集上限：防止超大目录（数十万文件）把扫描线程拖死
CANDIDATE_LIMIT = 20000


def _fingerprint(p: Path) -> str:
    """文件指纹：size + mtime_ns。mtime 精度覆盖同秒内修改场景。"""
    try:
        st = p.stat()
        return f"{st.st_size}_{st.st_mtime_ns}"
    except OSError:
        return ""


class FolderScanner:
    def __init__(self, db, data_dir, ingest, config, notifier=None):
        self.db = db
        self.data_dir = Path(data_dir)
        self.ingest = ingest
        self.config = config
        self.notify = notifier or (lambda t, m: None)
        self._lock = asyncio.Lock()

    # ---- 目录管理 ----------------------------------------------------------
    def add_dir(self, path: str, recursive: bool = True) -> dict:
        """接入一个本地目录。校验：存在、不在系统数据目录内、不重复。"""
        p = Path(path).expanduser().resolve()
        if not p.is_dir():
            raise ValueError(f"目录不存在：{path}")
        data_resolved = self.data_dir.resolve()
        # 防止把系统数据目录自身（含 memories/raw_docs 等）接入，导致自我扫描膨胀
        if p == data_resolved or data_resolved in p.parents:
            raise ValueError("不能接入系统数据目录（data/）")
        if self.db.query_one("SELECT 1 FROM local_dirs WHERE path=?",
                             (str(p),)):
            raise ValueError("该目录已接入")
        now = now_cst().isoformat(timespec="seconds")
        cur = self.db.execute(
            "INSERT INTO local_dirs(path,enabled,recursive,created_at) "
            "VALUES(?,1,?,?)", (str(p), 1 if recursive else 0, now))
        return {"id": cur.lastrowid, "path": str(p), "enabled": True,
                "recursive": bool(recursive), "created_at": now}

    def list_dirs(self) -> list[dict]:
        rows = self.db.query_all(
            "SELECT d.*, "
            "(SELECT count(*) FROM local_dir_files f WHERE f.dir_id=d.id) AS file_count, "
            "(SELECT count(*) FROM local_dir_files f WHERE f.dir_id=d.id "
            "AND f.status='imported') AS imported_count "
            "FROM local_dirs d ORDER BY d.id")
        out = []
        for r in rows:
            try:
                summary = json.loads(r["last_scan_summary"] or "{}")
            except (TypeError, ValueError):
                summary = {}
            out.append({
                "id": r["id"], "path": r["path"],
                "enabled": bool(r["enabled"]),
                "recursive": bool(r["recursive"]),
                "last_scan_at": r["last_scan_at"],
                "summary": summary,
                "file_count": r["file_count"] or 0,
                "imported_count": r["imported_count"] or 0,
            })
        return out

    def set_enabled(self, dir_id: int, enabled: bool) -> None:
        self.db.execute("UPDATE local_dirs SET enabled=? WHERE id=?",
                        (1 if enabled else 0, dir_id))

    def remove_dir(self, dir_id: int) -> None:
        """解除跟踪：仅清理跟踪记录，raw_docs 副本与已提炼记忆保留。"""
        self.db.execute(
            "DELETE FROM local_dir_files WHERE dir_id=?", (dir_id,))
        self.db.execute("DELETE FROM local_dirs WHERE id=?", (dir_id,))

    # ---- 扫描 --------------------------------------------------------------
    async def scan_all(self, trigger: str = "manual") -> dict:
        """扫描全部已启用目录（手动触发 / 调度器调用）。锁防并发重扫。"""
        async with self._lock:
            dirs = self.db.query_all(
                "SELECT * FROM local_dirs WHERE enabled=1 ORDER BY id")
            results = []
            for d in dirs:
                try:
                    results.append(await self._scan_dir(d, trigger))
                except Exception as e:  # noqa: BLE001
                    logger.exception("目录扫描失败：%s", d["path"])
                    results.append({"dir_id": d["id"], "path": d["path"],
                                    "skipped": True, "reason": str(e)})
            return {"dirs": results}

    async def _scan_dir(self, d: dict, trigger: str) -> dict:
        root = Path(d["path"])
        if not root.is_dir():
            return {"dir_id": d["id"], "path": d["path"], "skipped": True,
                    "reason": "目录不存在或不可访问"}
        max_files = self.config.get("local_dir_max_files_per_scan", 50)
        include_images = self.config.get("local_dir_include_images", False)
        now = now_cst().isoformat(timespec="seconds")

        # 1) 枚举候选文件（有界收集 + 过滤依赖目录/超大文件/不支持格式）
        candidates: list[Path] = []
        try:
            it = root.rglob("*") if d["recursive"] else root.glob("*")
            for f in it:
                try:
                    if not f.is_file():
                        continue
                    # 隐藏文件（.env/.gitignore 等）与隐藏/依赖目录一律跳过
                    if f.name.startswith("."):
                        continue
                    rel = f.relative_to(root)
                    if any(part.startswith(".") or part in SKIP_DIR_NAMES
                           for part in rel.parts[:-1]):
                        continue
                    ext = f.suffix.lower()
                    if ext not in SUPPORTED_EXTS:
                        if not (include_images and ext in IMAGE_EXTS):
                            continue
                    if f.stat().st_size > MAX_FILE_MB * 1024 * 1024:
                        continue
                except OSError:
                    continue
                candidates.append(f)
                if len(candidates) >= CANDIDATE_LIMIT:
                    break
        except OSError as e:
            return {"dir_id": d["id"], "path": d["path"], "skipped": True,
                    "reason": f"目录不可读：{e}"}

        # 2) 指纹比对：取新增/变更；源文件消失标记 deleted（记忆与副本保留）
        existing = {r["path"]: r for r in self.db.query_all(
            "SELECT id,path,fingerprint,status,doc_id FROM local_dir_files "
            "WHERE dir_id=?", (d["id"],))}
        seen: set[str] = set()
        pending: list[tuple[Path, str, int | None]] = []  # (文件, 指纹, 已有行 id)
        for f in candidates:
            fp = _fingerprint(f)
            if not fp:
                continue
            key = str(f)
            seen.add(key)
            old = existing.get(key)
            if old is None or old["fingerprint"] != fp:
                pending.append((f, fp, old["id"] if old else None))
            elif old["status"] == "deleted":
                # 文件恢复：内容未变，复用原 doc 关联，仅刷新状态
                self.db.execute(
                    "UPDATE local_dir_files SET status='imported', "
                    "last_seen_at=? WHERE id=?", (now, old["id"]))
        gone = [r["id"] for k, r in existing.items() if k not in seen]
        for rid in gone:
            self.db.execute(
                "UPDATE local_dir_files SET status='deleted', "
                "last_seen_at=? WHERE id=?", (now, rid))

        # 3) 逐文件导入（单轮上限约束；单文件失败隔离不中断整轮）
        imported = failed = memories = 0
        errors: list[str] = []
        for f, fp, row_id in pending[:max_files]:
            try:
                content = f.read_bytes()
                result = await self.ingest.ingest_file(
                    f.name, content, source="local_dir")
                doc_id = result.get("doc_id", "")
                imported += 1
                memories += result.get("extracted", 0)
                if row_id is None:
                    self.db.execute(
                        "INSERT INTO local_dir_files(dir_id,path,fingerprint,"
                        "doc_id,status,last_seen_at,imported_at) "
                        "VALUES(?,?,?,?,'imported',?,?)",
                        (d["id"], str(f), fp, doc_id, now, now))
                else:
                    self.db.execute(
                        "UPDATE local_dir_files SET fingerprint=?, doc_id=?, "
                        "status='imported', fail_reason=NULL, last_seen_at=?, "
                        "imported_at=? WHERE id=?", (fp, doc_id, now, now, row_id))
            except Exception as e:  # noqa: BLE001
                logger.warning("本地目录文件导入失败：%s：%s", f, e)
                failed += 1
                errors.append(f"{f.name}: {e}")
                if row_id is None:
                    self.db.execute(
                        "INSERT INTO local_dir_files(dir_id,path,fingerprint,"
                        "status,fail_reason,last_seen_at) "
                        "VALUES(?,?,?,'failed',?,?)",
                        (d["id"], str(f), fp, str(e)[:300], now))
                else:
                    self.db.execute(
                        "UPDATE local_dir_files SET status='failed', "
                        "fail_reason=?, last_seen_at=? WHERE id=?",
                        (str(e)[:300], now, row_id))

        summary = {
            "candidates": len(candidates),
            "changed": len(pending),
            "processed": min(len(pending), max_files),
            "imported": imported, "failed": failed, "memories": memories,
            "deleted": len(gone),
        }
        self.db.execute(
            "UPDATE local_dirs SET last_scan_at=?, last_scan_summary=? WHERE id=?",
            (now, json.dumps(summary, ensure_ascii=False), d["id"]))
        if imported or failed:
            msg = (f"本地目录「{root.name}」扫描完成：新增/变更 {summary['changed']} 个，"
                   f"导入 {imported} 个（提炼 {memories} 条记忆）"
                   + (f"，失败 {failed} 个" if failed else ""))
            self.notify("local_dir_scan_done", msg)
        return {"dir_id": d["id"], "path": d["path"], "summary": summary,
                "errors": errors[:10]}

    # ---- 供调度器使用的到期判断 -------------------------------------------
    def last_scan_at(self) -> datetime | None:
        row = self.db.query_one(
            "SELECT MAX(last_scan_at) m FROM local_dirs WHERE enabled=1")
        if not row or not row["m"]:
            return None
        try:
            return datetime.fromisoformat(row["m"])
        except (TypeError, ValueError):
            return None

    def has_enabled_dirs(self) -> bool:
        return bool(self.db.query_one(
            "SELECT 1 FROM local_dirs WHERE enabled=1"))
