"""视频工坊 API。

- GET    /workshop/capabilities
- GET    /workshop/types
- GET    /workshop/projects
- POST   /workshop/projects
- GET    /workshop/projects/{id}
- PATCH  /workshop/projects/{id}
- DELETE /workshop/projects/{id}
- POST   /workshop/projects/{id}/ensure-session
- POST   /workshop/projects/{id}/refs     (multipart 参考图)
- POST   /workshop/projects/{id}/render   (SSE)
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sse_starlette.sse import EventSourceResponse

from agent.video_workshop import (
    MAX_REFS,
    WORKSHOP_TYPE_DESCS,
    WORKSHOP_TYPES,
    WorkshopConflict,
    WorkshopError,
    VideoWorkshopStore,
)
from infrastructure.video_gen import (
    aspect_ratio_of,
    normalize_video_size,
    video_profile_for,
)
from infrastructure.video_gen.execute import execute_video_gen
from langfuse.integration import get_tracer

logger = logging.getLogger("second_person.routes.workshop")
router = APIRouter()

# aspect UI 值 → 优先匹配的 size 候选
_ASPECT_PREF = {
    "16:9": ("16:9", "832x480"),
    "9:16": ("9:16", "480x832"),
    "1:1": ("1:1",),
    "4:3": ("4:3",),
    "3:4": ("3:4",),
    "21:9": ("21:9",),
}

_PROGRESS_BY_STAGE = {
    "start": 5,
    "prepare": 10,
    "refining": 20,
    "refined": 30,
    "submit": 40,
    "waiting": 55,
    "saving": 85,
    "downloading": 90,
}


def _c():
    from app.main import get_container
    return get_container()


def _store() -> VideoWorkshopStore:
    return _c().workshop


class ProjectCreateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str = Field(..., min_length=1)
    type: str

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("视频名称必填")
        return v

    @field_validator("type")
    @classmethod
    def _type(cls, v: str) -> str:
        if v not in WORKSHOP_TYPES:
            raise ValueError(f"不支持的类型：{v}")
        return v


class ProjectPatchRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str | None = None
    script: str | None = None
    aspect: str | None = None
    duration_sec: float | None = None
    refs: list | None = None
    params: dict | None = None


def _size_for_aspect(profile, aspect: str) -> str:
    prefs = _ASPECT_PREF.get((aspect or "").strip(), ())
    for cand in prefs:
        if cand in profile.allowed_sizes:
            return cand
    for size, asp in profile.aspect_by_size.items():
        if asp == aspect and size in profile.allowed_sizes:
            return size
    return profile.default_size


def _aspect_options(profile) -> list[dict]:
    seen = set()
    opts = []
    for size in profile.allowed_sizes:
        asp = aspect_ratio_of(profile, size)
        if asp in seen:
            continue
        seen.add(asp)
        opts.append({"value": asp, "label": asp, "size": size})
    return opts


def _http_from_workshop(exc: WorkshopError) -> HTTPException:
    if isinstance(exc, WorkshopConflict):
        return HTTPException(409, str(exc))
    code = 404 if "不存在" in str(exc) else 400
    return HTTPException(code, str(exc))


@router.get("/workshop/capabilities")
async def workshop_capabilities():
    c = _c()
    snap = c.providers.snapshot_for("video_gen")
    if snap is None:
        return {"code": 200, "data": {
            "configured": False,
            "message": "未配置文生视频模型：请在设置页绑定「文生视频模型」",
            "aspects": [],
            "resolutions": [],
            "min_duration": 0,
            "max_duration": 0,
            "default_duration": 0,
            "engine": None,
            "types": [
                {"name": t, "desc": WORKSHOP_TYPE_DESCS.get(t, "")}
                for t in WORKSHOP_TYPES
            ],
            "max_refs": MAX_REFS,
            "refs_for_rewrite_only": True,
            "supports_i2v": False,
        }}
    profile = video_profile_for(snap, c.config)
    resolutions = (
        [{"value": "720p", "label": "720p"}, {"value": "480p", "label": "480p"}]
        if profile.engine == "cloud"
        else [{"value": "480p", "label": "480p"}]
    )
    supports_i2v = profile.engine == "cloud"
    return {"code": 200, "data": {
        "configured": True,
        "message": None,
        "aspects": _aspect_options(profile),
        "resolutions": resolutions,
        "min_duration": profile.min_duration,
        "max_duration": profile.max_duration,
        "default_duration": profile.default_duration,
        "engine": profile.engine,
        "model_id": snap.model_id,
        "types": [
            {"name": t, "desc": WORKSHOP_TYPE_DESCS.get(t, "")}
            for t in WORKSHOP_TYPES
        ],
        "max_refs": MAX_REFS,
        # 云端可灵：参考图可作出片首帧；本地 Wan：仅代写
        "refs_for_rewrite_only": not supports_i2v,
        "supports_i2v": supports_i2v,
    }}


@router.get("/workshop/types")
async def workshop_types():
    return {"code": 200, "data": [
        {"name": t, "desc": WORKSHOP_TYPE_DESCS.get(t, "")}
        for t in WORKSHOP_TYPES
    ]}


@router.get("/workshop/projects")
async def list_projects(type: str | None = None, q: str | None = None,
                        status: str | None = None,
                        limit: int = 100, offset: int = 0):
    items = _store().list(type_name=type, q=q, status=status,
                          limit=limit, offset=offset)
    return {"code": 200, "data": {"list": [p.to_dict() for p in items]}}


@router.post("/workshop/projects")
async def create_project(body: ProjectCreateRequest):
    try:
        proj = _store().create(body.title, body.type)
    except WorkshopError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"code": 200, "data": proj.to_dict()}


@router.get("/workshop/projects/{project_id}")
async def get_project(project_id: str):
    try:
        proj = _store().get(project_id)
    except WorkshopError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"code": 200, "data": proj.to_dict()}


@router.patch("/workshop/projects/{project_id}")
async def patch_project(project_id: str, body: ProjectPatchRequest):
    store = _store()
    try:
        proj = store.get(project_id)
    except WorkshopError as exc:
        raise HTTPException(404, str(exc)) from exc

    creative = any(v is not None for v in (
        body.title, body.script, body.aspect, body.duration_sec,
        body.refs, body.params,
    ))
    if proj.status == "doing" and creative:
        raise HTTPException(409, "视频正在生成中，暂不可编辑")

    fields = {}
    if body.title is not None:
        fields["title"] = body.title
    if body.script is not None:
        fields["script"] = body.script
    if body.aspect is not None:
        fields["aspect"] = body.aspect
    if body.duration_sec is not None:
        fields["duration_sec"] = body.duration_sec
    if body.refs is not None:
        fields["refs_json"] = body.refs
    if body.params is not None:
        fields["params_json"] = body.params
    try:
        proj = store.update(project_id, **fields)
    except WorkshopError as exc:
        raise _http_from_workshop(exc) from exc
    return {"code": 200, "data": proj.to_dict()}


@router.post("/workshop/projects/{project_id}/refs")
async def upload_refs(
    project_id: str,
    files: list[UploadFile] = File(...),
):
    """上传参考图：multipart 落盘 chat_images（与主对话图片落盘一致）。"""
    store = _store()
    try:
        proj = store.get(project_id)
    except WorkshopError as exc:
        raise HTTPException(404, str(exc)) from exc
    if proj.status == "doing":
        raise HTTPException(409, "视频正在生成中，暂不可编辑")
    if not files:
        raise HTTPException(400, "请选择图片")
    uploads: list[tuple[bytes, str]] = []
    for f in files:
        raw = await f.read()
        hint = (f.content_type or f.filename or "image/png")
        uploads.append((raw, hint))
    try:
        proj = store.add_ref_uploads(project_id, uploads)
    except WorkshopError as exc:
        raise _http_from_workshop(exc) from exc
    return {"code": 200, "data": proj.to_dict()}


@router.delete("/workshop/projects/{project_id}")
async def delete_project(project_id: str):
    try:
        _store().delete(project_id, sessions=_c().sessions)
    except WorkshopError as exc:
        raise _http_from_workshop(exc) from exc
    return {"code": 200, "data": {"ok": True}}


@router.post("/workshop/projects/{project_id}/ensure-session")
async def ensure_session(project_id: str):
    """确保工坊代写会话存在（channel=workshop，不进主列表）。"""
    store = _store()
    try:
        proj = store.get(project_id)
    except WorkshopError as exc:
        raise HTTPException(404, str(exc)) from exc
    sessions = _c().sessions
    sid = proj.session_id
    if sid:
        row = _c().db.query_one(
            "SELECT session_id, channel FROM sessions WHERE session_id=?", (sid,))
        if row and row["channel"] == "workshop":
            return {"code": 200, "data": {"session_id": sid}}
    sid = sessions.create_session(channel="workshop")
    try:
        sessions.rename(sid, f"工坊·{proj.title}"[:40])
    except Exception:  # noqa: BLE001
        pass
    store.update(project_id, session_id=sid)
    return {"code": 200, "data": {"session_id": sid}}


def _sse_pack(event: str, data: dict) -> dict:
    return {"event": event, "data": json.dumps(data, ensure_ascii=False)}


@router.post("/workshop/projects/{project_id}/render")
async def render_project(project_id: str):
    """触发出片：SSE 推大致进度，完成时可播放。"""
    store = _store()
    c = _c()
    try:
        proj = store.get(project_id)
    except WorkshopError as exc:
        raise HTTPException(404, str(exc)) from exc

    script = (proj.script or "").strip()
    if not script:
        raise HTTPException(400, "请先填写脚本后再生成视频")

    snap = c.providers.snapshot_for("video_gen")
    if snap is None:
        raise HTTPException(
            400, "未配置文生视频模型：请在设置页绑定「文生视频模型」")

    try:
        proj = store.try_claim_doing(project_id)
    except WorkshopError as exc:
        raise _http_from_workshop(exc) from exc

    profile = video_profile_for(snap, c.config)
    size = _size_for_aspect(profile, proj.aspect)
    size = normalize_video_size(profile, size)
    duration = proj.duration_sec if proj.duration_sec and proj.duration_sec > 0 \
        else profile.default_duration
    resolution = str((proj.params_json or {}).get("resolution") or "720p")

    queue: asyncio.Queue = asyncio.Queue()
    last_pct = {"v": 5}

    async def emit(event: str, data: dict):
        await queue.put(_sse_pack(event, data))

    async def run_render():
        tracer = get_tracer()
        trace = tracer.trace_start(
            "workshop.render",
            session_id=proj.session_id or project_id,
            input={"project_id": project_id, "title": proj.title,
                   "aspect": proj.aspect, "duration_sec": duration,
                   "resolution": resolution},
            metadata={"source": "workshop"},
            tags=["workshop"])
        span = tracer.span_start(
            "workshop.render",
            input={"project_id": project_id, "script_chars": len(script)})
        try:
            async def on_progress(stage: str, label: str):
                base = _PROGRESS_BY_STAGE.get(stage, max(40, last_pct["v"]))
                # waiting 阶段允许缓慢爬升，避免长时间卡在 55%
                if stage == "waiting":
                    base = max(base, min(82, last_pct["v"] + 2))
                elif stage == "downloading":
                    base = max(base, min(96, last_pct["v"] + 1))
                pct = max(last_pct["v"], min(96, int(base)))
                last_pct["v"] = pct
                try:
                    store.update(project_id, progress=pct)
                except WorkshopError:
                    return
                await emit("workshop_progress", {
                    "status": "doing", "stage": stage,
                    "label": label, "progress": pct,
                    "project_id": project_id,
                })

            await emit("workshop_progress", {
                "status": "doing", "stage": "start",
                "label": "开始生成视频…", "progress": 5,
                "project_id": project_id,
            })
            payload = await execute_video_gen(
                prompt=script,
                providers=c.providers,
                config=c.config,
                data_dir=c.data_dir,
                llm=c.llm,
                size=size,
                duration_sec=duration,
                resolution=resolution,
                session_id=proj.session_id or project_id,
                on_progress=on_progress,
                langfuse_source="workshop",
                # 云端可灵：首张参考图作图生视频；本地 Wan 仍只吃脚本
                image_names=(
                    list(proj.refs_json or [])
                    if profile.engine == "cloud" else None
                ),
            )
            filenames = payload.get("filenames") or []
            urls = payload.get("public_urls") or []
            fname = filenames[0] if filenames else None
            url = urls[0] if urls else (
                f"/chat-videos/{fname}" if fname else None)
            try:
                updated = store.update(
                    project_id,
                    status="done", progress=100, error_message=None,
                    filename=fname, public_url=url,
                    poster_url=payload.get("poster_url"),
                )
            except WorkshopError as exc:
                # 生成中被删除等：尽量不留悬空任务事件
                msg = str(exc)[:500] or "项目已不存在"
                await emit("workshop_error", {
                    "message": msg, "project": None, "project_id": project_id})
                return
            if span is not None:
                span.end(output={"filename": fname, "public_url": url})
            if trace is not None:
                try:
                    trace.end()
                except Exception:  # noqa: BLE001
                    pass
            await emit("workshop_done", {"project": updated.to_dict()})
        except Exception as exc:  # noqa: BLE001
            from infrastructure.remote_jobs import JobCancelled
            raw = (str(exc) or "").strip() or type(exc).__name__
            if isinstance(exc, JobCancelled) or "已停止生成" in raw:
                msg = "已停止生成"
                status = "cancelled"
                logger.info("workshop render cancelled: %s", project_id)
            else:
                msg = raw[:500] or "生成失败"
                status = "failed"
                logger.exception("workshop render failed: %s", project_id)
            updated = None
            try:
                updated = store.update(
                    project_id, status=status, progress=0, error_message=msg)
            except WorkshopError:
                pass
            if span is not None:
                try:
                    span.end(level="ERROR", status_message=msg[:300])
                except Exception:  # noqa: BLE001
                    pass
            if trace is not None:
                try:
                    trace.end()
                except Exception:  # noqa: BLE001
                    pass
            await emit("workshop_error", {
                "message": msg,
                "project": updated.to_dict() if updated else None,
                "project_id": project_id,
                "cancelled": status == "cancelled",
            })
        finally:
            await queue.put(None)

    asyncio.create_task(run_render())

    async def event_gen():
        while True:
            item = await queue.get()
            if item is None:
                break
            yield item

    return EventSourceResponse(event_gen(), ping=5)


@router.post("/workshop/projects/{project_id}/cancel")
async def cancel_project(project_id: str):
    """用户停止工坊出片：扇出取消远端任务，并通知进行中的 SSE。"""
    store = _store()
    try:
        proj = store.get(project_id)
    except WorkshopError as exc:
        raise HTTPException(404, str(exc)) from exc
    if proj.status != "doing":
        return {"code": 200, "data": {
            "cancelled": False, "message": "当前没有进行中的生成",
            "project": proj.to_dict(),
        }}
    from infrastructure.remote_jobs import cancel_session, get_store
    cancel_key = proj.session_id or project_id
    result = await cancel_session(cancel_key, store=get_store())
    # 若会话 id 与 project id 不同，两边都取消，避免漏网
    if proj.session_id and proj.session_id != project_id:
        await cancel_session(project_id, store=get_store())
    try:
        updated = store.update(
            project_id, status="cancelled", progress=0,
            error_message="已停止生成")
    except WorkshopError as exc:
        raise _http_from_workshop(exc) from exc
    return {"code": 200, "data": {
        "cancelled": True,
        "ports": result,
        "project": updated.to_dict(),
        "message": "已停止生成",
    }}
