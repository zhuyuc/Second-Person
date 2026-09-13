"""视频工坊资产存储（单视频为核心）。"""
from __future__ import annotations

import base64
import json
import logging
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from infrastructure.timeutil import now_cst

logger = logging.getLogger("second_person.video_workshop")

WORKSHOP_TYPES = (
    "广告片",
    "AI 短剧",
    "品牌宣传片",
    "故事短片",
    "概念氛围片",
    "产品演示",
)
WORKSHOP_TYPE_DESCS = {
    "广告片": "3 秒抓眼 → 痛点 → 卖点 → 行动引导",
    "AI 短剧": "连续剧情 · 单集强钩子 → 结尾留悬念",
    "品牌宣传片": "宏观 → 细节 → 价值 → 升华",
    "故事短片": "起 → 承 → 转 → 合",
    "概念氛围片": "氛围优先，弱叙事，重画面与音乐",
    "产品演示": "功能点逐一亮相 + 使用场景",
}
VALID_STATUS = ("draft", "doing", "done", "failed", "cancelled")
MAX_REFS = 6
# doing 超时回收（分钟）：仅用于进程崩溃后无 remote 续跟的僵尸；有 remote_id 应续 poll
STALE_DOING_MINUTES = 30

_REF_EXT_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}
_REF_MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
}


class WorkshopError(ValueError):
    """工坊业务错误。"""


class WorkshopConflict(WorkshopError):
    """状态冲突（如正在生成）。"""


def _new_id() -> str:
    return "vp_" + secrets.token_hex(6)


def _safe_unlink(dir_path: Path, filename: str | None) -> None:
    if not filename:
        return
    fname = Path(filename).name
    if not fname or ".." in fname or "/" in fname or "\\" in fname:
        return
    path = (dir_path / fname).resolve()
    try:
        path.relative_to(dir_path.resolve())
        if path.is_file():
            path.unlink()
    except (ValueError, OSError) as exc:
        logger.warning("删除文件失败 %s: %s", fname, exc)


def _ref_public_url(item: str) -> str:
    """DB 内文件名 / 旧 dataURL → 前端可展示的 URL。"""
    s = (item or "").strip()
    if not s:
        return s
    if s.startswith("data:") or s.startswith("/"):
        return s
    return f"/chat-images/{Path(s).name}"


def _persist_data_uri(image_dir: Path, data_uri: str) -> str | None:
    """与主对话 _persist_images 一致：dataURL → chat_images 文件名。"""
    try:
        header, _, encoded = data_uri.partition(",")
        if not encoded:
            return None
        mime = header.split(";")[0].removeprefix("data:").strip().lower()
        extension = _REF_MIME_EXT.get(mime, ".png")
        filename = f"wsref_{uuid.uuid4().hex[:12]}{extension}"
        (image_dir / filename).write_bytes(base64.b64decode(encoded))
        return filename
    except Exception:  # noqa: BLE001
        logger.warning("工坊参考图落盘失败", exc_info=True)
        return None


def normalize_refs_for_storage(refs: list, data_dir: Path) -> list[str]:
    """把 dataURL 落盘为文件名；已是文件名或 /chat-images/ 则规范化为纯文件名。"""
    image_dir = Path(data_dir) / "chat_images"
    image_dir.mkdir(parents=True, exist_ok=True)
    out: list[str] = []
    for item in refs or []:
        if not isinstance(item, str):
            continue
        s = item.strip()
        if not s:
            continue
        if s.startswith("data:"):
            fname = _persist_data_uri(image_dir, s)
            if fname:
                out.append(fname)
            continue
        if s.startswith("/chat-images/"):
            fname = Path(s).name
        else:
            fname = Path(s).name if ("/" in s or "\\" in s) else s
        if not fname or ".." in fname or "/" in fname or "\\" in fname:
            continue
        out.append(fname)
    if len(out) > MAX_REFS:
        raise WorkshopError(f"参考图最多 {MAX_REFS} 张")
    return out


@dataclass
class VideoProject:
    id: str
    title: str
    type: str
    status: str
    script: str
    aspect: str
    duration_sec: float
    params_json: dict
    refs_json: list
    filename: str | None
    public_url: str | None
    poster_url: str | None
    progress: int
    error_message: str | None
    session_id: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Any) -> "VideoProject":
        params = row["params_json"] if isinstance(row, dict) else row["params_json"]
        refs = row["refs_json"] if isinstance(row, dict) else row["refs_json"]
        if isinstance(params, str):
            try:
                params = json.loads(params or "{}")
            except json.JSONDecodeError:
                params = {}
        if isinstance(refs, str):
            try:
                refs = json.loads(refs or "[]")
            except json.JSONDecodeError:
                refs = []
        return cls(
            id=row["id"],
            title=row["title"],
            type=row["type"],
            status=row["status"],
            script=row["script"] or "",
            aspect=row["aspect"] or "16:9",
            duration_sec=float(row["duration_sec"] or 0),
            params_json=params if isinstance(params, dict) else {},
            refs_json=refs if isinstance(refs, list) else [],
            filename=row["filename"],
            public_url=row["public_url"],
            poster_url=row["poster_url"],
            progress=int(row["progress"] or 0),
            error_message=row["error_message"],
            session_id=row["session_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "type": self.type,
            "status": self.status,
            "script": self.script,
            "aspect": self.aspect,
            "duration_sec": self.duration_sec,
            "params": self.params_json,
            "refs": [_ref_public_url(r) for r in (self.refs_json or [])
                     if isinstance(r, str) and r],
            "filename": self.filename,
            "public_url": self.public_url,
            "poster_url": self.poster_url,
            "progress": self.progress,
            "error_message": self.error_message,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "type_desc": WORKSHOP_TYPE_DESCS.get(self.type, ""),
        }


class VideoWorkshopStore:
    def __init__(self, db, data_dir: str | Path):
        self.db = db
        self.data_dir = Path(data_dir)
        self.videos_dir = self.data_dir / "chat_videos"
        self.images_dir = self.data_dir / "chat_images"

    def create(self, title: str, type_name: str) -> VideoProject:
        title = (title or "").strip()
        if not title:
            raise WorkshopError("视频名称必填")
        if type_name not in WORKSHOP_TYPES:
            raise WorkshopError(f"不支持的类型：{type_name}")
        now = now_cst()
        pid = _new_id()
        self.db.execute(
            "INSERT INTO video_projects("
            "id,title,type,status,script,aspect,duration_sec,"
            "params_json,refs_json,progress,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, title, type_name, "draft", "", "16:9", 0,
             "{}", "[]", 0, now, now))
        return self.get(pid)

    def get(self, project_id: str) -> VideoProject:
        row = self.db.query_one(
            "SELECT * FROM video_projects WHERE id=?", (project_id,))
        if row is None:
            raise WorkshopError("视频不存在")
        return VideoProject.from_row(row)

    def list(self, *, type_name: str | None = None, q: str | None = None,
             status: str | None = None, limit: int = 100,
             offset: int = 0) -> list[VideoProject]:
        where = ["1=1"]
        args: list[Any] = []
        if type_name and type_name != "all":
            where.append("type=?")
            args.append(type_name)
        if status:
            where.append("status=?")
            args.append(status)
        if q and q.strip():
            where.append("title LIKE ? ESCAPE '\\'")
            args.append("%" + q.strip().replace("\\", "\\\\")
                        .replace("%", "\\%").replace("_", "\\_") + "%")
        limit = max(1, min(int(limit or 100), 200))
        offset = max(0, int(offset or 0))
        sql = (
            "SELECT * FROM video_projects WHERE "
            + " AND ".join(where)
            + " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        )
        args.extend([limit, offset])
        rows = self.db.query_all(sql, tuple(args))
        return [VideoProject.from_row(r) for r in rows]

    def add_ref_uploads(
        self,
        project_id: str,
        uploads: list[tuple[bytes, str]],
    ) -> VideoProject:
        """保存上传的参考图到 chat_images，并追加到 refs（与主对话落盘一致）。

        uploads: [(raw_bytes, content_type_or_filename), ...]
        """
        proj = self.get(project_id)
        if proj.status == "doing":
            raise WorkshopConflict("视频正在生成中，暂不可编辑")
        current: list[str] = []
        for item in proj.refs_json or []:
            if isinstance(item, str) and item and not item.startswith("data:"):
                current.append(Path(item).name)
            elif isinstance(item, str) and item.startswith("data:"):
                # 旧数据偶发仍是 dataURL：落盘后纳入
                fname = _persist_data_uri(self.images_dir, item)
                if fname:
                    current.append(fname)
        if len(current) >= MAX_REFS:
            raise WorkshopError(f"参考图最多 {MAX_REFS} 张")

        self.images_dir.mkdir(parents=True, exist_ok=True)
        added = 0
        for raw, hint in uploads:
            if len(current) >= MAX_REFS:
                break
            if not raw:
                continue
            # 与主对话附件同级：单文件不超过 50MB
            if len(raw) > 50 * 1024 * 1024:
                raise WorkshopError("单张参考图超过 50MB 上限")
            mime = (hint or "").split(";")[0].strip().lower()
            if "/" not in mime:
                # hint 可能是文件名
                ext = Path(hint or "").suffix.lower()
                mime = _REF_EXT_MIME.get(ext, "image/png")
            if not mime.startswith("image/"):
                raise WorkshopError("参考图仅支持图片文件")
            extension = _REF_MIME_EXT.get(mime, ".png")
            filename = f"wsref_{uuid.uuid4().hex[:12]}{extension}"
            (self.images_dir / filename).write_bytes(raw)
            current.append(filename)
            added += 1
        if added == 0:
            raise WorkshopError("未收到有效图片")
        return self.update(project_id, refs_json=current)

    def update(self, project_id: str, **fields) -> VideoProject:
        proj = self.get(project_id)
        allowed = {
            "title", "script", "aspect", "duration_sec", "params_json",
            "refs_json", "status", "filename", "public_url", "poster_url",
            "progress", "error_message", "session_id", "type",
        }
        sets = []
        args: list[Any] = []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "title" and v is not None:
                v = str(v).strip()
                if not v:
                    raise WorkshopError("视频名称不能为空")
            if k == "type" and v is not None and v not in WORKSHOP_TYPES:
                raise WorkshopError(f"不支持的类型：{v}")
            if k == "status" and v is not None and v not in VALID_STATUS:
                raise WorkshopError(f"非法状态：{v}")
            if k == "refs_json" and isinstance(v, list):
                # 与主对话一致：dataURL 落盘 chat_images，库内只存文件名
                old_refs = [
                    Path(str(x)).name for x in (proj.refs_json or [])
                    if isinstance(x, str) and x and not str(x).startswith("data:")
                ]
                stored = normalize_refs_for_storage(v, self.data_dir)
                # 删除本次不再引用的工坊参考图（仅 wsref_ 前缀，避免误删对话图）
                kept = set(stored)
                for fname in old_refs:
                    if fname.startswith("wsref_") and fname not in kept:
                        _safe_unlink(self.images_dir, fname)
                v = json.dumps(stored, ensure_ascii=False)
            if k == "params_json" and isinstance(v, dict):
                v = json.dumps(v, ensure_ascii=False)
            sets.append(f"{k}=?")
            args.append(v)
        if not sets:
            return proj
        sets.append("updated_at=?")
        args.append(now_cst())
        args.append(project_id)
        self.db.execute(
            f"UPDATE video_projects SET {', '.join(sets)} WHERE id=?",
            tuple(args))
        return self.get(project_id)

    def try_claim_doing(self, project_id: str) -> VideoProject:
        """原子抢锁：仅当 status!='doing' 时切入 doing，并清输出字段。

        成功返回抢锁后的项目；冲突抛 WorkshopConflict。
        旧成片文件会在抢锁成功后删除。
        """
        proj = self.get(project_id)
        if proj.status == "doing":
            raise WorkshopConflict("视频正在生成中，请稍候")
        old_filename = proj.filename
        now = now_cst()
        result = self.db.execute(
            "UPDATE video_projects SET status=?, progress=?, error_message=NULL, "
            "filename=NULL, public_url=NULL, poster_url=NULL, updated_at=? "
            "WHERE id=? AND status!=?",
            ("doing", 5, now, project_id, "doing"),
        )
        if int(result.rowcount or 0) != 1:
            raise WorkshopConflict("视频正在生成中，请稍候")
        _safe_unlink(self.videos_dir, old_filename)
        return self.get(project_id)

    def reclaim_stale_doing(self, *, older_than_minutes: int = STALE_DOING_MINUTES) -> int:
        """进程崩溃后的僵尸 doing：仅当无 remote_jobs 句柄时才标 failed。

        有 running remote_job 的项目留给 resume 续跟，禁止盲杀逼用户重提。
        """
        minutes = max(5, int(older_than_minutes or STALE_DOING_MINUTES))
        from datetime import datetime, timedelta

        def _parse(ts):
            if isinstance(ts, datetime):
                return ts
            s = str(ts or "")
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                try:
                    return datetime.strptime(s, fmt)
                except ValueError:
                    continue
            return None

        now_dt = now_cst()
        threshold = now_dt - timedelta(minutes=minutes)
        rows = self.db.query_all(
            "SELECT id, updated_at, session_id FROM video_projects WHERE status=?",
            ("doing",))
        n = 0
        kept = 0
        msg = f"生成中断（超过 {minutes} 分钟且无远端任务句柄），请重新生成"
        from infrastructure.remote_jobs import get_store
        store = get_store()
        for row in rows:
            ts = _parse(row["updated_at"])
            if ts is None or ts > threshold:
                continue
            pid = row["id"]
            sid = row["session_id"] or pid
            has_handle = False
            if store is not None:
                try:
                    jobs = store.find_running_by_session(sid)
                    if not jobs and sid != pid:
                        jobs = store.find_running_by_session(pid)
                    has_handle = bool(jobs)
                except Exception:  # noqa: BLE001
                    has_handle = False
            if has_handle:
                kept += 1
                continue
            result = self.db.execute(
                "UPDATE video_projects SET status=?, progress=0, "
                "error_message=?, updated_at=? WHERE id=? AND status=?",
                ("failed", msg, now_cst(), pid, "doing"),
            )
            if int(result.rowcount or 0) == 1:
                n += 1
        if n:
            logger.warning("reclaimed %s stale workshop doing projects", n)
        if kept:
            logger.info("kept %s workshop doing with remote_jobs handle", kept)
        return n

    def delete(self, project_id: str, *, sessions=None, force: bool = False) -> None:
        proj = self.get(project_id)
        if proj.status == "doing" and not force:
            raise WorkshopConflict("视频正在生成中，请稍后再删除")
        _safe_unlink(self.videos_dir, proj.filename)
        for item in proj.refs_json or []:
            if not isinstance(item, str) or item.startswith("data:"):
                continue
            fname = Path(item).name
            if fname.startswith("wsref_"):
                _safe_unlink(self.images_dir, fname)
        # 删绑定 workshop 会话
        if proj.session_id and sessions is not None:
            try:
                sessions.delete_session(proj.session_id)
            except Exception:  # noqa: BLE001
                logger.debug("删除工坊会话失败", exc_info=True)
        self.db.execute("DELETE FROM video_projects WHERE id=?", (project_id,))
