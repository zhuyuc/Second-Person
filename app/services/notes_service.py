"""记事本：md 主副本 + 逻辑目录树 + AI 修订备份 + 图片资源。"""
from __future__ import annotations

import json
import re
import secrets
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from infrastructure.timeutil import now_cst

_TITLE_MAX = 120
_FOLDER_NAME_MAX = 80
_BODY_HARD_MAX = 2 * 1024 * 1024
_BODY_WARN = 512 * 1024
_FOLDER_DEPTH_MAX = 10
_REV_KEEP = 10
_TREE_NAME = "_tree.json"
_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)
_NOTE_ID_RE = re.compile(r"^note_[0-9a-f]{8}$")
_FOLD_ID_RE = re.compile(r"^fold_[0-9a-f]{8}$")
_REV_ID_RE = re.compile(r"^rev_[0-9a-f]{8}$")
_ASSET_ID_RE = re.compile(r"^img_[0-9a-f]{12}\.(png|jpg|jpeg|gif|webp)$", re.I)


def _iso(dt: datetime | None = None) -> str:
    return (dt or now_cst()).isoformat(timespec="seconds")


def _new_note_id() -> str:
    return "note_" + secrets.token_hex(4)


def _new_fold_id() -> str:
    return "fold_" + secrets.token_hex(4)


def _new_rev_id() -> str:
    return "rev_" + secrets.token_hex(4)


def _new_asset_name(ext: str) -> str:
    ext = (ext or "png").lower().lstrip(".")
    if ext == "jpeg":
        ext = "jpg"
    if ext not in ("png", "jpg", "gif", "webp"):
        ext = "png"
    return f"img_{secrets.token_hex(6)}.{ext}"


def _parse_frontmatter(raw: str) -> tuple[dict, str]:
    m = _FM_RE.match(raw)
    if not m:
        return {}, raw
    meta: dict = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, _, v = line.partition(":")
        meta[k.strip()] = v.strip().strip("'").strip('"')
    return meta, m.group(2)


def _dump_file(meta: dict, body: str) -> str:
    lines = ["---"]
    for k in ("id", "title", "folder_id", "created_at", "updated_at"):
        if k not in meta:
            continue
        val = meta[k]
        if val is None or val == "":
            if k == "folder_id":
                continue
            continue
        lines.append(f"{k}: {str(val).replace(chr(10), ' ').strip()}")
    lines.append("---")
    lines.append("")
    body = body if body is not None else ""
    if body and not body.endswith("\n"):
        body += "\n"
    return "\n".join(lines) + body


def _norm_title(title: str | None, fallback: str = "未命名文档") -> str:
    t = (title or "").strip() or fallback
    return t[:_TITLE_MAX]


def _norm_folder_name(name: str | None, fallback: str = "新建文件夹") -> str:
    t = (name or "").strip() or fallback
    return t[:_FOLDER_NAME_MAX]


class ConflictError(Exception):
    def __init__(self, current: dict):
        super().__init__("文档已被更新")
        self.current = current


class NotesService:
    def __init__(self, data_dir: str | Path):
        self.root = Path(data_dir) / "notes"
        self.root.mkdir(parents=True, exist_ok=True)
        self.rev_root = self.root / "_revisions"
        self.rev_root.mkdir(parents=True, exist_ok=True)
        self._ensure_tree()
        # 进程内光标锚点：{note_id: {"before": str, "after": str, "updated_at": str}}
        # 由前端划词/光标变化上报；insert_at_cursor 据此定位，不落盘。
        self._cursors: dict[str, dict[str, str]] = {}

    # ---- tree ------------------------------------------------------------
    def _tree_path(self) -> Path:
        return self.root / _TREE_NAME

    def _ensure_tree(self) -> dict:
        p = self._tree_path()
        if not p.is_file():
            data = {"folders": [], "updated_at": _iso()}
            self._write_tree(data)
            return data
        return self._read_tree()

    def _read_tree(self) -> dict:
        p = self._tree_path()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {"folders": [], "updated_at": _iso()}
        if not isinstance(data.get("folders"), list):
            data["folders"] = []
        return data

    def _write_tree(self, data: dict) -> None:
        data = dict(data)
        data["updated_at"] = _iso()
        tmp = self._tree_path().with_suffix(".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(self._tree_path())

    def _folder_map(self) -> dict[str, dict]:
        return {f["id"]: f for f in self._read_tree()["folders"] if f.get("id")}

    def _folder_depth(self, folder_id: str | None, fmap: dict[str, dict] | None = None) -> int:
        if not folder_id:
            return 0
        fmap = fmap or self._folder_map()
        depth = 0
        seen: set[str] = set()
        cur = folder_id
        while cur:
            if cur in seen:
                break
            seen.add(cur)
            depth += 1
            parent = (fmap.get(cur) or {}).get("parent_id")
            cur = parent
        return depth

    def _assert_folder(self, folder_id: str | None) -> None:
        if folder_id is None or folder_id == "":
            return
        if not _FOLD_ID_RE.fullmatch(folder_id):
            raise ValueError("无效的文件夹 id")
        if folder_id not in self._folder_map():
            raise ValueError("文件夹不存在")

    def _is_descendant(self, ancestor_id: str, maybe_child_id: str, fmap: dict) -> bool:
        cur = maybe_child_id
        seen: set[str] = set()
        while cur:
            if cur == ancestor_id:
                return True
            if cur in seen:
                break
            seen.add(cur)
            cur = (fmap.get(cur) or {}).get("parent_id")
        return False

    # ---- note files ------------------------------------------------------
    def _note_path(self, note_id: str) -> Path:
        if not _NOTE_ID_RE.fullmatch(note_id or ""):
            raise ValueError("无效的文档 id")
        return self.root / f"{note_id}.md"

    def _assets_dir(self, note_id: str) -> Path:
        return self.root / note_id / "assets"

    def _read_note_file(self, note_id: str) -> dict:
        path = self._note_path(note_id)
        if not path.is_file():
            raise FileNotFoundError("文档不存在")
        raw = path.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(raw)
        folder_id = meta.get("folder_id") or None
        if folder_id == "" or folder_id == "null":
            folder_id = None
        return {
            "id": meta.get("id") or note_id,
            "title": meta.get("title") or "未命名文档",
            "folder_id": folder_id,
            "body_md": body,
            "created_at": meta.get("created_at") or "",
            "updated_at": meta.get("updated_at") or "",
            "size_bytes": len(raw.encode("utf-8")),
        }

    def _write_note_file(self, note_id: str, meta: dict, body: str) -> None:
        path = self._note_path(note_id)
        path.write_text(_dump_file(meta, body), encoding="utf-8")

    def list_notes(self) -> list[dict]:
        items: list[dict] = []
        for f in self.root.glob("note_*.md"):
            try:
                n = self._read_note_file(f.stem)
                items.append({
                    "id": n["id"],
                    "title": n["title"],
                    "folder_id": n["folder_id"],
                    "created_at": n["created_at"],
                    "updated_at": n["updated_at"],
                })
            except (OSError, ValueError):
                continue
        items.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
        return items

    def get_tree(self) -> dict:
        folders = list(self._read_tree()["folders"])
        notes = self.list_notes()
        return {"folders": folders, "notes": notes}

    @staticmethod
    def _search_snippet(body: str, query: str, *, radius: int = 36) -> str:
        """截取正文命中附近片段，换行压成空格便于列表展示。"""
        text = (body or "").replace("\r\n", "\n")
        flat = text.replace("\n", " ")
        q = query or ""
        if not q:
            return (flat[:80] + "…") if len(flat) > 80 else flat
        low = flat.lower()
        at = low.find(q.lower())
        if at < 0:
            return (flat[:80] + "…") if len(flat) > 80 else flat
        start = max(0, at - radius)
        end = min(len(flat), at + len(q) + radius)
        snip = flat[start:end].strip()
        if start > 0:
            snip = "…" + snip
        if end < len(flat):
            snip = snip + "…"
        return snip

    def search(self, query: str, *, limit: int = 50) -> dict:
        """跨文档搜索标题与正文（子串、不区分大小写）。"""
        q = (query or "").strip()
        limit = max(1, min(int(limit or 50), 100))
        if not q:
            return {"query": "", "total": 0, "hits": []}
        needle = q.lower()
        hits: list[dict] = []
        for f in self.root.glob("note_*.md"):
            try:
                n = self._read_note_file(f.stem)
            except (OSError, ValueError, FileNotFoundError):
                continue
            title = n.get("title") or ""
            body = n.get("body_md") or ""
            in_title = needle in title.lower()
            in_body = needle in body.lower()
            if not in_title and not in_body:
                continue
            match_in: list[str] = []
            if in_title:
                match_in.append("title")
            if in_body:
                match_in.append("body")
            hits.append({
                "id": n["id"],
                "title": title,
                "folder_id": n.get("folder_id"),
                "updated_at": n.get("updated_at") or "",
                "match_in": match_in,
                "snippet": self._search_snippet(body, q) if in_body else (title or "未命名文档"),
            })
        # 稳定排序：先按更新时间降序，再标题命中优先（同组内时间序保留）
        hits.sort(key=lambda h: h.get("updated_at") or "", reverse=True)
        hits.sort(key=lambda h: 0 if "title" in h["match_in"] else 1)
        total = len(hits)
        return {"query": q, "total": total, "hits": hits[:limit]}

    def get(self, note_id: str) -> dict:
        n = self._read_note_file(note_id)
        n["size_warn"] = n["size_bytes"] >= _BODY_WARN
        return n

    def create(self, title: str | None = None, body_md: str = "",
               folder_id: str | None = None) -> dict:
        if folder_id is None or folder_id == "":
            folder_id = None
        elif not _FOLD_ID_RE.fullmatch(folder_id) or folder_id not in self._folder_map():
            folder_id = None
        nid = _new_note_id()
        for _ in range(5):
            if not self._note_path(nid).exists():
                break
            nid = _new_note_id()
        now = _iso()
        meta = {
            "id": nid,
            "title": _norm_title(title),
            "folder_id": folder_id,
            "created_at": now,
            "updated_at": now,
        }
        self._write_note_file(nid, meta, body_md or "")
        return self.get(nid)

    def update(
        self,
        note_id: str,
        *,
        title: str | None = None,
        body_md: str | None = None,
        folder_id: Any = ...,
        if_updated_at: str | None = None,
    ) -> dict:
        cur = self.get(note_id)
        if if_updated_at is not None and if_updated_at and cur["updated_at"] != if_updated_at:
            raise ConflictError(cur)
        new_title = cur["title"] if title is None else _norm_title(title)
        new_body = cur["body_md"] if body_md is None else body_md
        if folder_id is ...:
            new_folder = cur["folder_id"]
        else:
            new_folder = folder_id or None
            self._assert_folder(new_folder)
        if isinstance(new_body, str) and len(new_body.encode("utf-8")) > _BODY_HARD_MAX:
            raise ValueError(f"文档超过 {_BODY_HARD_MAX // (1024 * 1024)}MB 上限，无法保存")
        meta = {
            "id": cur["id"],
            "title": new_title,
            "folder_id": new_folder,
            "created_at": cur["created_at"] or _iso(),
            "updated_at": _iso(),
        }
        self._write_note_file(note_id, meta, new_body or "")
        return self.get(note_id)

    def delete(self, note_id: str) -> None:
        path = self._note_path(note_id)
        if not path.is_file():
            raise FileNotFoundError("文档不存在")
        path.unlink()
        asset_root = self.root / note_id
        if asset_root.is_dir():
            shutil.rmtree(asset_root, ignore_errors=True)
        rev_dir = self.rev_root / note_id
        if rev_dir.is_dir():
            shutil.rmtree(rev_dir, ignore_errors=True)

    # ---- folders ---------------------------------------------------------
    def create_folder(self, name: str | None = None,
                      parent_id: str | None = None) -> dict:
        # 空 / 已删除的 parent 一律落到根，避免前端残留选中 id 导致「文件夹不存在」
        if parent_id is None or parent_id == "":
            parent_id = None
        elif not _FOLD_ID_RE.fullmatch(parent_id) or parent_id not in self._folder_map():
            parent_id = None
        fmap = self._folder_map()
        depth = self._folder_depth(parent_id, fmap) + 1
        if depth > _FOLDER_DEPTH_MAX:
            raise ValueError(f"文件夹最多嵌套 {_FOLDER_DEPTH_MAX} 级")
        fid = _new_fold_id()
        for _ in range(5):
            if fid not in fmap:
                break
            fid = _new_fold_id()
        siblings = [f for f in fmap.values() if (f.get("parent_id") or None) == parent_id]
        sort_order = (max((s.get("sort_order") or 0) for s in siblings) + 1) if siblings else 0
        folder = {
            "id": fid,
            "name": _norm_folder_name(name),
            "parent_id": parent_id,
            "sort_order": sort_order,
            "created_at": _iso(),
            "updated_at": _iso(),
        }
        data = self._read_tree()
        data["folders"].append(folder)
        self._write_tree(data)
        return folder

    def update_folder(
        self,
        folder_id: str,
        *,
        name: str | None = None,
        parent_id: Any = ...,
        sort_order: int | None = None,
    ) -> dict:
        if not _FOLD_ID_RE.fullmatch(folder_id or ""):
            raise ValueError("无效的文件夹 id")
        data = self._read_tree()
        folders = data["folders"]
        idx = next((i for i, f in enumerate(folders) if f.get("id") == folder_id), -1)
        if idx < 0:
            raise FileNotFoundError("文件夹不存在")
        folder = dict(folders[idx])
        fmap = {f["id"]: f for f in folders}
        if name is not None:
            folder["name"] = _norm_folder_name(name)
        if parent_id is not ...:
            new_parent = parent_id or None
            if new_parent == folder_id:
                raise ValueError("不能将文件夹移动到自身下")
            if new_parent:
                if new_parent not in fmap:
                    raise ValueError("目标父文件夹不存在")
                if self._is_descendant(folder_id, new_parent, fmap):
                    raise ValueError("不能将文件夹移动到其子文件夹下")
                # depth check after move
                # temporarily pretend parent changed
                trial = dict(fmap)
                trial[folder_id] = {**folder, "parent_id": new_parent}
                if self._folder_depth(folder_id, trial) > _FOLDER_DEPTH_MAX:
                    raise ValueError(f"文件夹最多嵌套 {_FOLDER_DEPTH_MAX} 级")
            folder["parent_id"] = new_parent
        if sort_order is not None:
            folder["sort_order"] = int(sort_order)
        folder["updated_at"] = _iso()
        folders[idx] = folder
        data["folders"] = folders
        self._write_tree(data)
        return folder

    def delete_folder(self, folder_id: str) -> dict:
        """策略 A：删除文件夹及其全部子孙文件夹与文档。"""
        if not _FOLD_ID_RE.fullmatch(folder_id or ""):
            raise ValueError("无效的文件夹 id")
        data = self._read_tree()
        fmap = {f["id"]: f for f in data["folders"]}
        if folder_id not in fmap:
            raise FileNotFoundError("文件夹不存在")
        to_delete_folds = {
            fid for fid in fmap
            if fid == folder_id or self._is_descendant(folder_id, fid, fmap)
        }
        notes = self.list_notes()
        deleted_notes = [n["id"] for n in notes if (n.get("folder_id") in to_delete_folds)]
        for nid in deleted_notes:
            try:
                self.delete(nid)
            except FileNotFoundError:
                pass
        data["folders"] = [f for f in data["folders"] if f.get("id") not in to_delete_folds]
        self._write_tree(data)
        return {
            "deleted_folders": sorted(to_delete_folds),
            "deleted_notes": deleted_notes,
        }

    # ---- revisions -------------------------------------------------------
    def create_revision(
        self,
        note_id: str,
        *,
        source: str = "ai",
        skip_empty: bool = True,
        skip_duplicate: bool = True,
    ) -> dict | None:
        """备份当前正文。空文档默认不建备份；与最近一份正文相同则复用，避免堆重复。"""
        cur = self.get(note_id)
        body = cur.get("body_md") or ""
        if skip_empty and not body.strip():
            return None
        if skip_duplicate:
            latest = self.list_revisions(note_id, limit=1, include_body=True)
            if latest and (latest[0].get("body_md") or "") == body:
                return latest[0]
        rev_id = _new_rev_id()
        rev_dir = self.rev_root / note_id
        rev_dir.mkdir(parents=True, exist_ok=True)
        md_path = rev_dir / f"{rev_id}.md"
        meta_path = rev_dir / f"{rev_id}.meta.json"
        md_path.write_text(body, encoding="utf-8")
        meta = {
            "id": rev_id,
            "note_id": note_id,
            "title": cur["title"],
            "created_at": _iso(),
            "source": source,
            "char_count": len(body),
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        self._prune_revisions(note_id)
        return meta

    def _prune_revisions(self, note_id: str) -> None:
        rev_dir = self.rev_root / note_id
        if not rev_dir.is_dir():
            return
        metas = sorted(rev_dir.glob("rev_*.meta.json"), key=lambda p: p.stat().st_mtime,
                       reverse=True)
        for old in metas[_REV_KEEP:]:
            rid = old.name.replace(".meta.json", "")
            old.unlink(missing_ok=True)
            (rev_dir / f"{rid}.md").unlink(missing_ok=True)

    def list_revisions(
        self,
        note_id: str,
        *,
        limit: int | None = None,
        include_body: bool = False,
    ) -> list[dict]:
        self.get(note_id)  # ensure exists
        rev_dir = self.rev_root / note_id
        if not rev_dir.is_dir():
            return []
        out: list[dict] = []
        for meta_path in rev_dir.glob("rev_*.meta.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            rid = meta.get("id") or ""
            body_path = rev_dir / f"{rid}.md"
            body = ""
            if body_path.is_file():
                try:
                    body = body_path.read_text(encoding="utf-8")
                except OSError:
                    body = ""
            # 历史误写入的空备份不进入列表，避免「0 字」干扰选择
            if not body.strip():
                continue
            item = {
                **meta,
                "char_count": len(body),
                "preview": (body[:280] + ("…" if len(body) > 280 else "")),
            }
            if include_body:
                item["body_md"] = body
            out.append(item)
        out.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        if limit is not None:
            lim = max(1, min(int(limit), _REV_KEEP))
            out = out[:lim]
        return out

    def get_revision(self, note_id: str, rev_id: str) -> dict:
        if not _REV_ID_RE.fullmatch(rev_id or ""):
            raise ValueError("无效的备份 id")
        self.get(note_id)
        rev_dir = self.rev_root / note_id
        meta_path = rev_dir / f"{rev_id}.meta.json"
        body_path = rev_dir / f"{rev_id}.md"
        if not meta_path.is_file() or not body_path.is_file():
            raise FileNotFoundError("备份不存在")
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise FileNotFoundError("备份损坏") from e
        body = body_path.read_text(encoding="utf-8")
        return {
            **meta,
            "body_md": body,
            "char_count": len(body),
            "preview": body[:280] + ("…" if len(body) > 280 else ""),
        }

    def restore_revision(self, note_id: str, rev_id: str | None = None) -> dict:
        """恢复指定修订；rev_id 为空则恢复最近一次。"""
        revs = self.list_revisions(note_id)
        if not revs:
            raise ValueError("没有可恢复的备份")
        target = None
        if rev_id:
            target = next((r for r in revs if r.get("id") == rev_id), None)
            if not target:
                raise FileNotFoundError("备份不存在")
        else:
            target = revs[0]
        path = self.rev_root / note_id / f"{target['id']}.md"
        if not path.is_file():
            raise FileNotFoundError("备份正文不存在")
        body = path.read_text(encoding="utf-8")
        # 恢复前再备份当前版，便于反悔
        self.create_revision(note_id, source="pre_restore")
        return self.update(note_id, body_md=body)

    # ---- assets ----------------------------------------------------------
    def save_asset(self, note_id: str, data: bytes, filename: str | None = None) -> dict:
        self.get(note_id)
        if len(data) > 8 * 1024 * 1024:
            raise ValueError("单张图片不能超过 8MB")
        ext = "png"
        if filename and "." in filename:
            ext = filename.rsplit(".", 1)[-1]
        name = _new_asset_name(ext)
        if not _ASSET_ID_RE.fullmatch(name):
            name = _new_asset_name("png")
        d = self._assets_dir(note_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(data)
        rel = f"./assets/{name}"
        return {"filename": name, "rel_path": rel, "url": f"/api/notes/{note_id}/assets/{name}"}

    def get_asset_path(self, note_id: str, filename: str) -> Path:
        if not _NOTE_ID_RE.fullmatch(note_id or ""):
            raise ValueError("无效的文档 id")
        if not _ASSET_ID_RE.fullmatch(filename or ""):
            raise ValueError("无效的资源名")
        path = self._assets_dir(note_id) / filename
        if not path.is_file():
            raise FileNotFoundError("图片不存在")
        return path

    # ---- cursor anchor (insert_at_cursor) --------------------------------
    def set_cursor(
        self,
        note_id: str,
        *,
        before: str | None = None,
        after: str | None = None,
    ) -> dict:
        """记录插入点两侧文本锚点（前端从编辑器光标/选区上报）。"""
        self.get(note_id)  # ensure exists
        before_s = (before or "")[-240:]
        after_s = (after or "")[:160]
        meta = {
            "note_id": note_id,
            "before": before_s,
            "after": after_s,
            "updated_at": _iso(),
        }
        self._cursors[note_id] = meta
        return meta

    def get_cursor(self, note_id: str) -> dict | None:
        return self._cursors.get(note_id)

    def clear_cursor(self, note_id: str) -> None:
        self._cursors.pop(note_id, None)

    @staticmethod
    def _find_insert_pos(body: str, before: str, after: str) -> int | None:
        """在 body 中根据 before/after 锚点定位插入下标；找不到返回 None。"""
        before = before or ""
        after = after or ""
        if not before and not after:
            return None

        def _match_after(rest: str, needle: str) -> bool:
            if not needle:
                return True
            if rest.startswith(needle):
                return True
            # 允许中间夹少量空白/换行（md 常见）
            stripped = rest.lstrip(" \t")
            if stripped.startswith(needle):
                return True
            if stripped.startswith("\n"):
                return stripped[1:].lstrip(" \t").startswith(needle)
            return False

        if before:
            start = 0
            candidates: list[int] = []
            while True:
                i = body.find(before, start)
                if i < 0:
                    break
                j = i + len(before)
                if _match_after(body[j:], after):
                    candidates.append(j)
                start = j if j > i else i + 1
            if len(candidates) == 1:
                return candidates[0]
            if len(candidates) > 1:
                # 多处命中：取最后一处（更接近用户刚编辑的位置）
                return candidates[-1]
            # before 能找到但 after 对不上：仍插在最后一个 before 之后
            last = body.rfind(before)
            if last >= 0:
                return last + len(before)

        if after:
            i = body.find(after)
            if i >= 0:
                return i
        return None

    # ---- AI apply --------------------------------------------------------
    def apply_edit(
        self,
        note_id: str,
        *,
        op: str,
        content_md: str,
        selected_text: str | None = None,
        insert_after: str | None = None,
        create_backup: bool = True,
    ) -> dict:
        op = (op or "").strip()
        content_md = content_md if content_md is not None else ""
        rev_meta = None
        if create_backup:
            rev_meta = self.create_revision(note_id, source="ai")
        cur = self.get(note_id)
        body = cur["body_md"] or ""

        if op == "append":
            sep = "" if (not body or body.endswith("\n")) else "\n"
            new_body = body + sep + content_md
        elif op == "replace_document":
            new_body = content_md
        elif op == "insert_at_cursor":
            # 定位优先级：显式 insert_after → 前端上报光标 → selected_text 作 before → 文末
            before = ""
            after = ""
            if insert_after is not None and str(insert_after) != "":
                before = str(insert_after)
            else:
                anchor = self._cursors.get(note_id) or {}
                before = str(anchor.get("before") or "")
                after = str(anchor.get("after") or "")
                if not before and selected_text:
                    before = str(selected_text)
            pos = self._find_insert_pos(body, before, after)
            # 富文本纯文本锚点与 md 不完全一致时，缩短 before 再试一次
            if pos is None and len(before) > 48:
                pos = self._find_insert_pos(body, before[-48:], after)
            if pos is None and len(before) > 24:
                pos = self._find_insert_pos(body, before[-24:], after[:40] if after else "")
            if pos is None:
                sep = "" if (not body or body.endswith("\n")) else "\n"
                new_body = body + sep + content_md
                inserted_at = "append_fallback"
            else:
                new_body = body[:pos] + content_md + body[pos:]
                inserted_at = "cursor"
        elif op == "replace_selection":
            sel = selected_text if selected_text is not None else ""
            if sel == "":
                raise ValueError("replace_selection 需要 selected_text")
            if sel not in body:
                raise ValueError("选区文本在文档中未找到，请重新划词后再试")
            new_body = body.replace(sel, content_md, 1)
            inserted_at = None
        else:
            raise ValueError(
                "op 必须是 replace_selection | insert_at_cursor | append | replace_document")

        note = self.update(note_id, body_md=new_body)
        note["op"] = op
        note["revision_created"] = bool(rev_meta)
        if op == "insert_at_cursor":
            note["inserted_at"] = inserted_at
        return note
