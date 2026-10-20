"""记事本 HTTP 路由。"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Path, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

router = APIRouter(prefix="/notes", tags=["notes"])

# 约束文档 id，避免 /search 等静态段在路由误匹配时被当成 note_id
NoteIdPath = Annotated[str, Path(pattern=r"^note_[0-9a-f]{8}$")]
RevIdPath = Annotated[str, Path(pattern=r"^rev_[0-9a-f]{8}$")]


def _c():
    from app.main import get_container
    return get_container()


def _svc():
    return _c().notes


class CreateNoteBody(BaseModel):
    title: str | None = None
    body_md: str = ""
    folder_id: str | None = None


class PatchNoteBody(BaseModel):
    title: str | None = None
    body_md: str | None = None
    folder_id: str | None = None
    clear_folder: bool = False
    if_updated_at: str | None = None


class CreateFolderBody(BaseModel):
    name: str | None = None
    parent_id: str | None = None


class PatchFolderBody(BaseModel):
    name: str | None = None
    parent_id: str | None = None
    clear_parent: bool = False
    sort_order: int | None = None


class ApplyEditBody(BaseModel):
    op: str = Field(..., description="replace_selection|insert_at_cursor|append|replace_document")
    content_md: str = ""
    selected_text: str | None = None
    insert_after: str | None = None


class CursorBody(BaseModel):
    before: str = ""
    after: str = ""


class RestoreBody(BaseModel):
    rev_id: str | None = None


@router.get("/tree")
async def get_tree():
    return {"code": 200, "data": _svc().get_tree()}


@router.get("")
async def list_notes():
    return {"code": 200, "data": {"list": _svc().list_notes()}}


@router.get("/search")
async def search_notes(
    q: str = Query("", description="搜索关键字（标题+正文）"),
    limit: int = Query(50, ge=1, le=100),
):
    """笔记库搜索：匹配文档标题与正文，返回摘要片段。须注册在 /{note_id} 之前。"""
    return {"code": 200, "data": _svc().search(q, limit=limit)}


@router.post("")
async def create_note(body: CreateNoteBody | None = None):
    body = body or CreateNoteBody()
    try:
        note = _svc().create(
            title=body.title, body_md=body.body_md or "", folder_id=body.folder_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"code": 200, "data": note}


@router.post("/folders")
async def create_folder(body: CreateFolderBody | None = None):
    body = body or CreateFolderBody()
    try:
        folder = _svc().create_folder(name=body.name, parent_id=body.parent_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"code": 200, "data": folder}


@router.patch("/folders/{folder_id}")
async def patch_folder(folder_id: str, body: PatchFolderBody):
    parent = ...
    if body.clear_parent:
        parent = None
    elif body.parent_id is not None:
        parent = body.parent_id
    try:
        folder = _svc().update_folder(
            folder_id,
            name=body.name,
            parent_id=parent,
            sort_order=body.sort_order,
        )
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"code": 200, "data": folder}


@router.delete("/folders/{folder_id}")
async def delete_folder(folder_id: str):
    try:
        result = _svc().delete_folder(folder_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"code": 200, "data": result}


@router.get("/{note_id}")
async def get_note(note_id: NoteIdPath):
    try:
        return {"code": 200, "data": _svc().get(note_id)}
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.patch("/{note_id}")
async def patch_note(note_id: NoteIdPath, body: PatchNoteBody):
    from app.services.notes_service import ConflictError
    folder = ...
    if body.clear_folder:
        folder = None
    elif body.folder_id is not None:
        folder = body.folder_id
    try:
        note = _svc().update(
            note_id,
            title=body.title,
            body_md=body.body_md,
            folder_id=folder,
            if_updated_at=body.if_updated_at,
        )
    except ConflictError as e:
        raise HTTPException(409, detail={"message": "文档已被更新", "current": e.current}) from e
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"code": 200, "data": note}


@router.delete("/{note_id}")
async def delete_note(note_id: NoteIdPath):
    try:
        _svc().delete(note_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"code": 200, "data": {}}


@router.post("/{note_id}/cursor")
async def set_cursor(note_id: NoteIdPath, body: CursorBody | None = None):
    """同步编辑器光标锚点，供 insert_at_cursor 定位。"""
    body = body or CursorBody()
    try:
        meta = _svc().set_cursor(
            note_id, before=body.before or "", after=body.after or "")
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"code": 200, "data": meta}


@router.post("/{note_id}/apply_edit")
async def apply_edit(note_id: NoteIdPath, body: ApplyEditBody):
    from app.services.notes_service import ConflictError
    try:
        note = _svc().apply_edit(
            note_id,
            op=body.op,
            content_md=body.content_md or "",
            selected_text=body.selected_text,
            insert_after=body.insert_after,
        )
    except ConflictError as e:
        raise HTTPException(409, detail={"message": "文档已被更新", "current": e.current}) from e
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"code": 200, "data": note}


class CreateRevisionBody(BaseModel):
    source: str | None = "checkpoint"


@router.get("/{note_id}/revisions")
async def list_revisions(
    note_id: NoteIdPath,
    limit: int = Query(10, ge=1, le=20, description="返回最近 N 份，默认 10（与磁盘保留一致）"),
    include_body: bool = Query(False, description="是否附带完整 body_md"),
):
    try:
        items = _svc().list_revisions(
            note_id, limit=limit, include_body=include_body)
        return {"code": 200, "data": {"list": items, "total": len(items)}}
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/{note_id}/revisions")
async def create_revision(note_id: NoteIdPath, body: CreateRevisionBody | None = None):
    """手动打检查点（打开 AI 写文档前调用）；空正文会跳过。"""
    body = body or CreateRevisionBody()
    src = (body.source or "checkpoint").strip() or "checkpoint"
    try:
        meta = _svc().create_revision(note_id, source=src)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"code": 200, "data": meta or {"skipped": True, "reason": "empty_or_duplicate"}}


@router.get("/{note_id}/revisions/{rev_id}")
async def get_revision(note_id: NoteIdPath, rev_id: RevIdPath):
    try:
        return {"code": 200, "data": _svc().get_revision(note_id, rev_id)}
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/{note_id}/revisions/restore")
async def restore_revision(note_id: NoteIdPath, body: RestoreBody | None = None):
    body = body or RestoreBody()
    try:
        note = _svc().restore_revision(note_id, body.rev_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"code": 200, "data": note}


@router.post("/{note_id}/assets")
async def upload_asset(note_id: NoteIdPath, file: UploadFile = File(...)):
    raw = await file.read()
    try:
        info = _svc().save_asset(note_id, raw, file.filename)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"code": 200, "data": info}


@router.get("/{note_id}/assets/{filename}")
async def get_asset(note_id: NoteIdPath, filename: str):
    try:
        path = _svc().get_asset_path(note_id, filename)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return FileResponse(path)
