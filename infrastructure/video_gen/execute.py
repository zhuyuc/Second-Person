"""文生视频共享执行体：工具 generate_video 与视频工坊 render 共用。

不改变工具对外契约；仅把「profile → refine → adapter → 落盘结果」抽成一处。
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from infrastructure.json_repair import repair_json
from infrastructure.prompt_loader import PROMPTS
from infrastructure.provider_modality import is_local_gpu
from langfuse.integration import get_tracer, mark_preview

from .factory import get_video_adapter
from .profiles import clamp_duration, normalize_video_size, video_profile_for
from .types import VideoGenRequest

logger = logging.getLogger("second_person.video_gen.execute")

ProgressCb = Callable[[str, str], Awaitable[None]] | None


async def execute_video_gen(
    *,
    prompt: str,
    providers,
    config,
    data_dir,
    llm=None,
    negative_prompt: str = "",
    size: str = "480x832",
    duration_sec: float = 3,
    n: int = 1,
    motion_hint: str = "",
    style_hint: str = "",
    session_id: str = "",
    on_progress: ProgressCb = None,
    langfuse_source: str = "video_gen",
    resolution: str = "720p",
    image_name: str | None = None,
    image_names: list[str] | None = None,
) -> dict[str, Any]:
    """执行一次文生/图生视频，返回 VideoGenResult.to_dict() 形态。"""
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("generate_video 需要非空 prompt")

    if providers is None:
        raise RuntimeError("未配置文生视频模型：ProviderRegistry 不可用")
    snap = providers.snapshot_for("video_gen")
    if snap is None:
        raise RuntimeError(
            "未配置文生视频模型：请在设置页为「文生视频模型」绑定视频模态的 Provider")

    from .images import pick_first_image_name, resolve_chat_image

    img_name = (image_name or "").strip() or pick_first_image_name(image_names)
    if img_name and resolve_chat_image(data_dir, img_name) is None:
        raise RuntimeError(f"找不到参考图：{img_name}")

    profile = video_profile_for(snap, config)
    local_gpu = is_local_gpu(snap)
    clamped_notes: list[str] = []
    try:
        n_req = int(n or 1)
    except (TypeError, ValueError):
        n_req = 1
    if n_req != 1:
        clamped_notes.append("当前一次只生成 1 条")

    size_raw = normalize_video_size(profile, size)
    if (size or "").strip() and size_raw != (size or "").lower().replace("*", "x"):
        clamped_notes.append(f"尺寸已映射为 {size_raw}")

    duration_i, dur_clamped = clamp_duration(
        profile, duration_sec, profile.default_duration)
    if dur_clamped:
        clamped_notes.append(f"时长已夹紧为 {duration_i} 秒")
    if img_name:
        clamped_notes.append("已使用参考图（图生视频）")

    refined_neg = (negative_prompt or "").strip()
    final_prompt = prompt

    async def _progress(stage: str, label: str):
        if on_progress is None:
            return
        try:
            await on_progress(stage, label)
        except Exception:  # noqa: BLE001
            pass

    await _progress(
        "prepare",
        ("准备本地生视频…" if local_gpu else "正在生成视频…")
        if not img_name else
        ("本地暂不支持图生视频…" if local_gpu else "正在图生视频…"),
    )
    if local_gpu:
        from infrastructure.lazy_services import ensure_comfyui
        await _progress("prepare", "检查本地生视频服务…")
        ensured = await ensure_comfyui(timeout=180.0, data_dir=data_dir)
        if not ensured.get("ok"):
            raise RuntimeError(
                "本地生视频服务未启动："
                f"{ensured.get('error') or 'ComfyUI 不可用'}")
        if ensured.get("started"):
            await _progress("prepare", "本地生视频服务已拉起，继续…")
    refine_on = profile.refine == "wan_en" and llm is not None and not img_name
    if refine_on:
        refine_snap = providers.snapshot_for("retriever_refine") \
            or providers.snapshot_for("agent") or snap
        try:
            await _progress("refining", "正在润色镜头描述…")
            sys_p = PROMPTS.load_raw("agent/prompts/video_prompt_refine")
            user_p = prompt if not refined_neg else (
                f"描述：{prompt}\n负面词：{refined_neg}")
            resp = await llm.chat(
                refine_snap,
                [{"role": "system", "content": sys_p},
                 {"role": "user", "content": user_p}],
                source="video_prompt_refine")
            data = repair_json(resp.get("content") or "")
            if (data.get("prompt") or "").strip():
                final_prompt = data["prompt"].strip()
            if (data.get("negative_prompt") or "").strip():
                refined_neg = data["negative_prompt"].strip()
            await _progress("refined", "镜头描述已就绪，准备提交引擎…")
        except Exception:  # noqa: BLE001
            logger.debug("video_prompt_refine 失败，回退原 prompt", exc_info=True)
            await _progress("refined", "润色跳过，使用原始镜头描述…")

    fps = int(config.get("video_gen_fps", 16) or 16)
    res = (resolution or "720p").strip().lower()
    if res not in ("720p", "480p"):
        res = "720p"
    adapter = get_video_adapter(snap, config, data_dir)
    req = VideoGenRequest(
        prompt=final_prompt,
        negative_prompt=refined_neg,
        size=size_raw,
        duration_sec=duration_i,
        fps=fps,
        steps=int(config.get("video_gen_steps", 20) or 20),
        n=1,
        motion_hint=(motion_hint or "").strip(),
        style_hint=(style_hint or "").strip(),
        model_id=snap.model_id or "",
        resolution=res,
        image_filename=img_name,
    )

    tracer = get_tracer()
    gen = tracer.generation_start(
        "llm.video_gen",
        model=snap.model_id or "video",
        input={"prompt": final_prompt[:500], "n": 1, "size": size_raw,
               "duration_sec": duration_i, "fps": fps,
               "backend": profile.engine, "resolution": res,
               "steps": int(config.get("video_gen_steps", 20) or 20),
               "image_filename": img_name or ""},
        metadata={"source": langfuse_source, "provider_id": snap.provider_id,
                  "provider_type": getattr(snap, "provider_type", "")},
    )
    try:
        result = await adapter.generate(
            req, provider_id=snap.provider_id, model_id=snap.model_id or "",
            session_id=session_id, on_progress=_progress)
        payload = result.to_dict()
        if final_prompt != prompt:
            clamped_notes.append("已自动润色英文 prompt")
        if clamped_notes:
            payload["summary"] = (
                (payload.get("summary") or "") + "（" + "；".join(clamped_notes) + "）"
            ).strip()
        rec = getattr(llm, "recorder", None) if llm is not None else None
        billed_sec = payload.get("duration_sec") or duration_i
        if rec is not None and (snap.output_price is not None or snap.input_price is not None):
            rec.record(
                snap.model_id or "", "video_gen", 0, 0, session_id or None,
                input_price=snap.input_price, output_price=snap.output_price,
                usage_kind="second", quantity=float(billed_sec or 0))
        if gen is not None:
            gen.end(output={
                "n": payload.get("n"),
                "filenames": payload.get("filenames"),
                "latency_ms": payload.get("latency_ms"),
                "duration_sec": payload.get("duration_sec"),
                "preview": mark_preview(
                    payload.get("summary") or "", content_type="tool_result"),
            })
        return payload
    except Exception as exc:  # noqa: BLE001
        if gen is not None:
            try:
                raw = (str(exc) or "").strip() or type(exc).__name__
                gen.end(level="ERROR", status_message=raw[:300])
            except Exception:  # noqa: BLE001
                pass
        msg = str(exc)
        low = msg.lower()
        if "未启动" in msg or "ConnectError" in type(exc).__name__ or "连接" in msg:
            if local_gpu:
                raise RuntimeError(
                    "本地生视频服务未启动：请先运行 image_gen/comfyui 下的 "
                    "run_nvidia_gpu.bat，并确认 http://127.0.0.1:8188 可访问"
                ) from exc
            raise RuntimeError("云端生视频服务连接失败，请检查地址与 API Key") from exc
        if "out of memory" in low or ("cuda" in low and "memory" in low) \
                or "oom" in low:
            raise RuntimeError(
                "显存不足：请关闭向日葵/ToDesk 等远控及其他占显存程序后重试；"
                "也可将时长改为更短（2–3 秒）"
            ) from exc
        raise
