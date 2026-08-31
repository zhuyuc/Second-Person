"""
项目工作区（Project Workspace）管理。

设计原则见 docs/PROJECTS.md：
- realpath 归一化 → path 存原始（正斜杠 + 用户输入大小写），path_key 存归一化比较键
- 同一物理目录只能存在一条 active 记录（path_key 唯一）
- 归档非破坏：项目 archived 联动会话 archived_source='project'
- 永久删除仅允许 status='archived'；purge 会真删该项目所有相关数据
- 目录本身永不删除

依赖：infrastructure.db.Database（写入走单写线程 + transaction()）
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from memory.naming import project_id as make_project_id
from infrastructure.timeutil import now_cst

logger = logging.getLogger("second_person.projects")

# 系统关键目录黑名单：**浏览和创建都禁止**（真正的系统文件区）
SYSTEM_DIR_BLACKLIST_LOWER = {
    "/etc", "/usr", "/bin", "/sbin", "/root", "/dev", "/proc", "/sys",
    "/boot", "/lib", "/lib64",
    "c:/windows", "c:/program files", "c:/program files (x86)",
    "c:/programdata", "c:/system volume information",
    "$recycle.bin",
}
# 前缀黑名单：起始于以下路径都拒（浏览 + 创建）
SYSTEM_DIR_PREFIX_BLACKLIST_LOWER = (
    "c:/windows/", "c:/program files/", "c:/program files (x86)/",
    "c:/programdata/", "/etc/", "/usr/", "/bin/", "/sbin/", "/sys/",
    "/proc/", "/dev/", "/boot/",
)
# 仅**创建**项目时禁止（浏览允许，用户需要点进去看子目录）：
# 磁盘根、根目录、用户目录本身
CREATE_ONLY_BLACKLIST_PATTERNS = (
    # 匹配 Windows 磁盘根：c:/、c:、c:/  或 POSIX 根
    re.compile(r"^[a-z]:/?$"),
    re.compile(r"^/$"),
    re.compile(r"^c:/users$"),
    re.compile(r"^c:/users/public$"),
)

VALID_SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
VALID_STATUS = ("active", "archived")
VALID_ARCHIVED_SOURCE = ("project", "manual")


class ProjectError(ValueError):
    """项目操作错误（路径非法 / 状态不允许 / 冲突等）。"""


@dataclass
class Project:
    id: str
    path: str
    path_key: str
    title: str
    display_order: int
    sandbox_mode: str
    ignore_extra: list
    status: str
    archived_at: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: dict) -> "Project":
        return cls(
            id=row["id"], path=row["path"], path_key=row["path_key"],
            title=row["title"], display_order=row["display_order"],
            sandbox_mode=row["sandbox_mode"],
            ignore_extra=json.loads(row["ignore_extra"]) if row.get("ignore_extra") else [],
            status=row["status"], archived_at=row.get("archived_at"),
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id, "path": self.path, "title": self.title,
            "display_order": self.display_order,
            "sandbox_mode": self.sandbox_mode,
            "ignore_extra": self.ignore_extra,
            "status": self.status, "archived_at": self.archived_at,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }


def normalize_path(raw: str) -> tuple[str, str]:
    """返回 (display_path, path_key)。

    display_path：realpath 后转正斜杠，保留用户输入的大小写。
    path_key：display_path 全小写（Windows FS 大小写不敏感，避免同目录重复加载）。
    """
    if not raw or not isinstance(raw, str):
        raise ProjectError("路径为空")
    # 展开 ~
    expanded = os.path.expanduser(raw.strip())
    try:
        resolved = os.path.realpath(expanded)
    except OSError as exc:
        raise ProjectError(f"路径无法解析：{exc}") from exc
    display = resolved.replace("\\", "/")
    # 去尾斜杠（除非是根 /）
    if len(display) > 1 and display.endswith("/"):
        display = display.rstrip("/")
    key = display.lower()
    return display, key


def is_system_dir(key: str) -> bool:
    """浏览级黑名单：真正的系统文件区。适用于 browse 与 create 两个入口。"""
    if key in SYSTEM_DIR_BLACKLIST_LOWER:
        return True
    for prefix in SYSTEM_DIR_PREFIX_BLACKLIST_LOWER:
        if key.startswith(prefix):
            return True
    return False


def is_create_forbidden_root(key: str) -> bool:
    """创建项目禁止的根：磁盘根、根目录、C:/Users 本身。仅在 create 校验。"""
    for pat in CREATE_ONLY_BLACKLIST_PATTERNS:
        if pat.match(key):
            return True
    return False


def validate_project_path(raw: str) -> str:
    """校验**创建项目时**路径合法性并返回归一化后的 (display_path, path_key)。

    抛 ProjectError（400 语义）：路径不存在 / 非目录 / 系统目录 / 磁盘根 / 用户根
    """
    display, key = normalize_path(raw)
    if is_system_dir(key):
        raise ProjectError(f"拒绝加载系统目录：{display}")
    if is_create_forbidden_root(key):
        raise ProjectError(f"拒绝加载磁盘根或用户目录：{display}"
                           "，请点进去选具体子目录")
    p = Path(display)
    if not p.exists():
        raise ProjectError(f"路径不存在：{display}")
    if not p.is_dir():
        raise ProjectError(f"不是目录：{display}")
    return display, key


class ProjectStore:
    """项目 CRUD + 归档 + 永久删除。所有写走 db.transaction()。"""

    def __init__(self, db, data_dir):
        self.db = db
        self.data_dir = Path(data_dir)

    # ---- 查询 ------------------------------------------------------------
    def get(self, project_id: str) -> Project | None:
        row = self.db.query_one(
            "SELECT * FROM projects WHERE id=?", (project_id,))
        return Project.from_row(row) if row else None

    def get_by_path(self, raw_path: str) -> Project | None:
        try:
            _, key = normalize_path(raw_path)
        except ProjectError:
            return None
        row = self.db.query_one(
            "SELECT * FROM projects WHERE path_key=?", (key,))
        return Project.from_row(row) if row else None

    def list(self, status: str = "active") -> list[Project]:
        """status: active / archived / all"""
        if status == "all":
            rows = self.db.query_all(
                "SELECT * FROM projects ORDER BY display_order ASC, created_at DESC")
        else:
            rows = self.db.query_all(
                "SELECT * FROM projects WHERE status=? "
                "ORDER BY display_order ASC, created_at DESC", (status,))
        return [Project.from_row(r) for r in rows]

    def list_active(self) -> list[Project]:
        return self.list("active")

    def session_count(self, project_id: str, include_archived: bool = False) -> int:
        if include_archived:
            row = self.db.query_one(
                "SELECT COUNT(*) c FROM sessions WHERE project_id=?", (project_id,))
        else:
            row = self.db.query_one(
                "SELECT COUNT(*) c FROM sessions WHERE project_id=? AND archived=0",
                (project_id,))
        return int(row["c"]) if row else 0

    def path_missing(self, project: Project) -> bool:
        try:
            return not Path(project.path).is_dir()
        except OSError:
            return True

    def scan_missing_dirs(self, notifier=None) -> list[str]:
        """遍历所有 active 项目，返回目录已丢失的 id 列表；命中时可选推系统通知。
        本次改造：不修改 status；只通过 path_missing 视图字段暴露到前端，
        避免误把 U 盘临时挂载丢失当作项目删除。用户通过 UI 主动重定位或删除。"""
        missing: list[str] = []
        for proj in self.list_active():
            if self.path_missing(proj):
                missing.append(proj.id)
                if notifier is not None:
                    try:
                        notifier("project_dir_missing",
                                 f"项目「{proj.title}」的目录已丢失："
                                 f"{proj.path}，请重新定位或永久删除")
                    except Exception:  # noqa: BLE001
                        logger.debug("推送目录丢失通知失败", exc_info=True)
        return missing

    # ---- 创建 ------------------------------------------------------------
    def create(self, raw_path: str, title: str | None = None,
               sandbox_mode: str = "workspace-write") -> Project:
        """创建项目；同 path_key 已存在则返回既有（幂等）。"""
        if sandbox_mode not in VALID_SANDBOX_MODES:
            raise ProjectError(f"非法沙箱档位：{sandbox_mode}")
        display, key = validate_project_path(raw_path)
        # 幂等：已加载过 → 直接返回（active 或 archived 都返回）
        existing = self.db.query_one(
            "SELECT * FROM projects WHERE path_key=?", (key,))
        if existing:
            return Project.from_row(existing)
        title = (title or Path(display).name or "未命名项目")[:60]
        pid = make_project_id()
        # 冲突极小概率：重试一次
        for _ in range(3):
            if not self.db.query_one("SELECT 1 FROM projects WHERE id=?", (pid,)):
                break
            pid = make_project_id()
        now = now_cst().isoformat(timespec="seconds")
        # display_order 取当前最小 - 1（新建置顶）
        row = self.db.query_one("SELECT MIN(display_order) m FROM projects")
        min_order = row["m"] if row and row["m"] is not None else 0
        order = min_order - 1
        self.db.execute(
            "INSERT INTO projects(id, path, path_key, title, display_order, "
            "sandbox_mode, ignore_extra, status, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,'active',?,?)",
            (pid, display, key, title, order, sandbox_mode, "[]", now, now))
        # 写 md 主副本
        self._write_md(pid)
        logger.info("项目已创建 id=%s path=%s", pid, display)
        return self.get(pid)

    # ---- 修改 ------------------------------------------------------------
    def patch(self, project_id: str, *, title: str | None = None,
              display_order: int | None = None,
              sandbox_mode: str | None = None,
              ignore_extra: list | None = None) -> Project:
        proj = self.get(project_id)
        if not proj:
            raise ProjectError(f"项目不存在：{project_id}")
        updates, params = [], []
        if title is not None:
            updates.append("title=?"); params.append(title[:60])
        if display_order is not None:
            updates.append("display_order=?"); params.append(int(display_order))
        if sandbox_mode is not None:
            if sandbox_mode not in VALID_SANDBOX_MODES:
                raise ProjectError(f"非法沙箱档位：{sandbox_mode}")
            updates.append("sandbox_mode=?"); params.append(sandbox_mode)
        if ignore_extra is not None:
            if not isinstance(ignore_extra, list):
                raise ProjectError("ignore_extra 必须是数组")
            updates.append("ignore_extra=?")
            params.append(json.dumps(ignore_extra, ensure_ascii=False))
        if not updates:
            return proj
        updates.append("updated_at=?")
        params.append(now_cst().isoformat(timespec="seconds"))
        params.append(project_id)
        self.db.execute(
            f"UPDATE projects SET {', '.join(updates)} WHERE id=?", params)
        self._write_md(project_id)
        return self.get(project_id)

    def relocate(self, project_id: str, new_path: str) -> Project:
        proj = self.get(project_id)
        if not proj:
            raise ProjectError(f"项目不存在：{project_id}")
        display, key = validate_project_path(new_path)
        if key != proj.path_key:
            # 冲突：另一个项目已占该路径
            conflict = self.db.query_one(
                "SELECT id, title FROM projects WHERE path_key=? AND id!=?",
                (key, project_id))
            if conflict:
                raise ProjectError(
                    f"该目录已加载为项目：{conflict['title']}（{conflict['id']}）")
        now = now_cst().isoformat(timespec="seconds")
        self.db.execute(
            "UPDATE projects SET path=?, path_key=?, updated_at=? WHERE id=?",
            (display, key, now, project_id))
        self._write_md(project_id)
        return self.get(project_id)

    # ---- 归档 / 恢复 -----------------------------------------------------
    def archive(self, project_id: str) -> dict:
        proj = self.get(project_id)
        if not proj:
            raise ProjectError(f"项目不存在：{project_id}")
        if proj.status == "archived":
            return {"archived_sessions": 0, "already_archived": True}
        now = now_cst().isoformat(timespec="seconds")
        # 联动归档会话
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE projects SET status='archived', archived_at=?, updated_at=? "
                "WHERE id=?", (now, now, project_id))
            cur = conn.execute(
                "UPDATE sessions SET archived=1, archived_source='project', "
                "archived_at=? WHERE project_id=? AND archived=0",
                (now, project_id))
            n = cur.rowcount
        self._write_md(project_id)
        logger.info("项目归档 id=%s sessions=%d", project_id, n)
        return {"archived_sessions": n}

    def unarchive(self, project_id: str) -> dict:
        proj = self.get(project_id)
        if not proj:
            raise ProjectError(f"项目不存在：{project_id}")
        if proj.status == "active":
            return {"restored_sessions": 0, "already_active": True}
        now = now_cst().isoformat(timespec="seconds")
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE projects SET status='active', archived_at=NULL, updated_at=? "
                "WHERE id=?", (now, project_id))
            cur = conn.execute(
                "UPDATE sessions SET archived=0, archived_source=NULL, "
                "archived_at=NULL WHERE project_id=? AND archived_source='project'",
                (project_id,))
            n = cur.rowcount
        self._write_md(project_id)
        logger.info("项目恢复 id=%s sessions=%d", project_id, n)
        return {"restored_sessions": n}

    # ---- 永久删除（全量） ------------------------------------------------
    def purge(self, project_id: str) -> dict:
        """永久删除项目及所有相关数据。仅允许 status='archived'。

        删除范围（按 docs/PROJECTS.md v5 §八 8.5）：
        - sessions 及其 conversations / citation_events / review_candidates /
          memory_write_candidates / token_usage / agent_turns /
          agent_turn_events / session_policy_events / fs_observations /
          platform_sessions
        - 图谱：memory_entity_links / memory_entities / memory_links / graph_layout
        - 记忆：vectors / memories_fts / memory_timeline / lint_suggestions / memories
        - 知识：local_dir_files / local_dirs / raw_docs
        - 项目本体 + md 主副本

        项目本地目录本身**永不删除**。
        """
        proj = self.get(project_id)
        if not proj:
            raise ProjectError(f"项目不存在：{project_id}")
        if proj.status != "archived":
            raise ProjectError("请先归档项目再永久删除")

        # 先读全部 id 便于事务后清理磁盘文件
        session_ids = [r["session_id"] for r in self.db.query_all(
            "SELECT session_id FROM sessions WHERE project_id=?", (project_id,))]
        memory_ids = [r["id"] for r in self.db.query_all(
            "SELECT id FROM memories WHERE project_id=?", (project_id,))]
        memory_md_paths = [r["md_path"] for r in self.db.query_all(
            "SELECT md_path FROM memories WHERE project_id=?", (project_id,))]
        # raw_docs：读文件路径便于后续磁盘清理
        raw_rows = self.db.query_all(
            "SELECT id, file_path FROM raw_docs WHERE project_id=?", (project_id,))

        deleted_msgs = 0
        # 大事务
        with self.db.transaction() as conn:
            if session_ids:
                ph = ",".join("?" * len(session_ids))
                # 1. 会话相关（顺序：外键式引用先删）
                cur = conn.execute(
                    f"DELETE FROM conversations WHERE session_id IN ({ph})",
                    session_ids)
                deleted_msgs = cur.rowcount
                for tbl in ("citation_events", "review_candidates",
                            "token_usage", "session_policy_events",
                            "fs_observations", "platform_sessions"):
                    conn.execute(
                        f"DELETE FROM {tbl} WHERE session_id IN ({ph})",
                        session_ids)
                # memory_write_candidates 部分历史行 status 已 done → 保留（不阻塞删除）
                conn.execute(
                    f"DELETE FROM memory_write_candidates WHERE session_id IN ({ph})",
                    session_ids)
                # agent_turns / agent_turn_events / delivery 兼容缺表环境
                for tbl in ("agent_turns", "agent_turn_events",
                            "agent_step_metrics", "delivery_jobs",
                            "delivery_sections"):
                    try:
                        conn.execute(
                            f"DELETE FROM {tbl} WHERE session_id IN ({ph})",
                            session_ids)
                    except Exception:  # noqa: BLE001
                        # 该表可能不存在或字段命名不同；不阻塞删除
                        pass
                conn.execute(
                    f"DELETE FROM sessions WHERE session_id IN ({ph})",
                    session_ids)

            # 2. 图谱（按 project_id）
            for tbl in ("memory_entity_links", "memory_entities",
                        "memory_links", "graph_layout"):
                conn.execute(f"DELETE FROM {tbl} WHERE project_id=?", (project_id,))

            # 3. 记忆及其向量 / FTS / 时间线 / lint
            if memory_ids:
                mh = ",".join("?" * len(memory_ids))
                for tbl in ("vectors", "memories_fts", "memory_timeline"):
                    conn.execute(
                        f"DELETE FROM {tbl} WHERE memory_id IN ({mh})",
                        memory_ids)
                try:
                    conn.execute(
                        f"DELETE FROM lint_suggestions "
                        f"WHERE primary_memory_id IN ({mh})", memory_ids)
                except Exception:  # noqa: BLE001
                    pass
            conn.execute(
                "DELETE FROM memories WHERE project_id=?", (project_id,))

            # 4. 知识素材
            try:
                conn.execute(
                    "DELETE FROM local_dir_files WHERE dir_id IN "
                    "(SELECT id FROM local_dirs WHERE project_id=?)",
                    (project_id,))
            except Exception:  # noqa: BLE001
                pass
            conn.execute(
                "DELETE FROM local_dirs WHERE project_id=?", (project_id,))
            conn.execute(
                "DELETE FROM raw_docs WHERE project_id=?", (project_id,))

            # 5. 项目本身
            conn.execute("DELETE FROM projects WHERE id=?", (project_id,))

        # 6. 磁盘清理（事务后）
        self._unlink_many([self.data_dir / p for p in memory_md_paths])
        for r in raw_rows:
            self._unlink_safe(Path(r["file_path"]))
        self._unlink_safe(self.data_dir / "projects" / f"{project_id}.md")
        # 会话摘要 md（可能不存在）
        for sid in session_ids:
            self._unlink_safe(self.data_dir / "sessions" / f"{sid}.md")

        logger.info("项目已永久删除 id=%s sessions=%d messages=%d "
                    "memories=%d raw_docs=%d",
                    project_id, len(session_ids), deleted_msgs,
                    len(memory_ids), len(raw_rows))
        return {
            "deleted_sessions": len(session_ids),
            "deleted_messages": deleted_msgs,
            "deleted_memories": len(memory_ids),
            "deleted_docs": len(raw_rows),
        }

    # ---- md 主副本 -------------------------------------------------------
    def _write_md(self, project_id: str) -> None:
        proj = self.get(project_id)
        if not proj:
            return
        pdir = self.data_dir / "projects"
        pdir.mkdir(parents=True, exist_ok=True)
        fm_lines = [
            "---",
            f"id: {proj.id}",
            f"path: {proj.path}",
            f"title: {proj.title}",
            f"display_order: {proj.display_order}",
            f"sandbox_mode: {proj.sandbox_mode}",
            f"status: {proj.status}",
            f"archived_at: {proj.archived_at or 'null'}",
            f"ignore_extra: {json.dumps(proj.ignore_extra, ensure_ascii=False)}",
            f"created_at: {proj.created_at}",
            f"updated_at: {proj.updated_at}",
            "---",
            "",
            f"# {proj.title}",
            "",
            f"项目根：`{proj.path}`",
            "",
        ]
        (pdir / f"{proj.id}.md").write_text(
            "\n".join(fm_lines), encoding="utf-8")

    def _unlink_safe(self, p: Path) -> None:
        try:
            if p.exists():
                p.unlink()
        except OSError as exc:
            logger.warning("磁盘清理失败：%s (%s)", p, exc)

    def _unlink_many(self, paths) -> None:
        for p in paths:
            self._unlink_safe(p)
