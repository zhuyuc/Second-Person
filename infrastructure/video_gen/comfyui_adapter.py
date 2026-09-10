"""ComfyUI HTTP Adapter：提交文生视频工作流、轮询、取视频、落盘（V1 仅 T2V）。"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from infrastructure.image_gen import active_jobs

from .types import (
    ALLOWED_SIZES,
    DEFAULT_DURATION_SEC,
    DEFAULT_FPS,
    DEFAULT_SIZE,
    MAX_DURATION_SEC,
    MIN_DURATION_SEC,
    VideoGenRequest,
    VideoGenResult,
)

logger = logging.getLogger("second_person.video_gen.comfyui")

ProgressCb = Callable[[str, str], Awaitable[None]] | None

_DEFAULT_NEGATIVE = (
    "low quality, worst quality, blurry, jitter, distorted, "
    "text, watermark, logo, static, still image, morphing"
)

_STYLE_PREFIX = {
    "cinematic": "cinematic lighting, film still, ",
    "anime": "anime style, vibrant colors, ",
    "realistic": "photorealistic, natural motion, ",
}

_MOTION_PREFIX = {
    "slow": "slow gentle motion, ",
    "medium": "smooth natural motion, ",
    "dynamic": "dynamic camera movement, energetic motion, ",
}

# 按 class_type 定位可改写节点（避免死绑单一模板节点号）
_LATENT_CLASSES = {
    "EmptyHunyuanLatentVideo",
    "EmptyMochiLatentVideo",
    "WanEmptyLatent",
}
_UNET_CLASSES = {
    "UNETLoader",
    "UnetLoaderGGUF",
    "CheckpointLoaderSimple",
    "WanVideoModelLoader",
}
_CREATE_VIDEO_CLASSES = {"CreateVideo"}


def clamp_duration_sec(value: Any, default: int = DEFAULT_DURATION_SEC) -> tuple[int, bool]:
    """返回 (夹紧后的秒数, 是否发生夹紧)。"""
    try:
        raw = int(float(value))
    except (TypeError, ValueError):
        return default, True
    clamped = max(MIN_DURATION_SEC, min(MAX_DURATION_SEC, raw))
    return clamped, clamped != raw


def normalize_size(size: str | None, default: str = DEFAULT_SIZE) -> str:
    raw = (size or default).lower().replace("*", "x")
    if raw not in ALLOWED_SIZES:
        return default
    return raw


class ComfyUIVideoAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        workflow_path: Path,
        data_dir: Path,
        timeout_sec: float = 600.0,
        default_steps: int = 20,
        default_size: str = DEFAULT_SIZE,
        default_duration_sec: int = DEFAULT_DURATION_SEC,
        fps: int = DEFAULT_FPS,
        prompt_max_chars: int = 1500,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.workflow_path = Path(workflow_path)
        self.data_dir = Path(data_dir)
        self.timeout_sec = timeout_sec
        self.default_steps = default_steps
        self.default_size = default_size
        self.default_duration_sec = default_duration_sec
        self.fps = fps
        self.prompt_max_chars = prompt_max_chars

    def _load_workflow(self) -> dict:
        if not self.workflow_path.exists():
            raise FileNotFoundError(
                f"ComfyUI 视频工作流模板不存在：{self.workflow_path}")
        return json.loads(self.workflow_path.read_text(encoding="utf-8"))

    def _build_prompt_text(self, req: VideoGenRequest) -> str:
        style = _STYLE_PREFIX.get((req.style_hint or "").strip().lower(), "")
        motion = _MOTION_PREFIX.get((req.motion_hint or "").strip().lower(), "")
        return f"{style}{motion}{(req.prompt or '').strip()}"[: self.prompt_max_chars]

    @staticmethod
    def _iter_nodes(wf: dict) -> list[tuple[str, dict]]:
        out: list[tuple[str, dict]] = []
        for nid, node in wf.items():
            if isinstance(node, dict) and "class_type" in node:
                out.append((str(nid), node))
        return out

    def _apply_workflow(self, template: dict, req: VideoGenRequest,
                        model_name: str) -> dict:
        wf = copy.deepcopy(template)
        prompt = self._build_prompt_text(req)
        negative = (req.negative_prompt or "").strip() or _DEFAULT_NEGATIVE
        size = normalize_size(req.size or self.default_size, self.default_size)
        width, height = (int(x) for x in size.split("x", 1))
        duration, _ = clamp_duration_sec(
            req.duration_sec or self.default_duration_sec,
            self.default_duration_sec)
        req.duration_sec = duration
        req.fps = int(req.fps or self.fps)
        frames = req.num_frames
        steps = int(req.steps or self.default_steps)
        seed = req.seed if req.seed is not None else (uuid.uuid4().int & 0xFFFFFFFF)

        for _nid, node in self._iter_nodes(wf):
            ctype = node.get("class_type") or ""
            inputs = node.setdefault("inputs", {})
            if ctype in _UNET_CLASSES:
                if "unet_name" in inputs or ctype in ("UNETLoader", "UnetLoaderGGUF"):
                    inputs["unet_name"] = model_name
                elif "ckpt_name" in inputs or ctype == "CheckpointLoaderSimple":
                    inputs["ckpt_name"] = model_name
            if ctype in _LATENT_CLASSES:
                inputs["width"] = width
                inputs["height"] = height
                inputs["length"] = frames
                if "num_frames" in inputs:
                    inputs["num_frames"] = frames
                inputs["batch_size"] = 1
            if ctype == "KSampler":
                inputs["steps"] = steps
                inputs["seed"] = seed
            if ctype in _CREATE_VIDEO_CLASSES:
                inputs["fps"] = req.fps
            if ctype == "VHS_VideoCombine":
                inputs["frame_rate"] = req.fps
                inputs["save_output"] = True

        # CLIPTextEncode：按节点 ID 排序后第一个正、第二个负
        clip_nodes = sorted(
            ((nid, n) for nid, n in self._iter_nodes(wf)
             if n.get("class_type") == "CLIPTextEncode"),
            key=lambda x: int(x[0]) if str(x[0]).isdigit() else str(x[0]),
        )
        if len(clip_nodes) >= 1:
            clip_nodes[0][1].setdefault("inputs", {})["text"] = prompt
        if len(clip_nodes) >= 2:
            clip_nodes[1][1].setdefault("inputs", {})["text"] = negative

        return wf

    @staticmethod
    def _extract_media_meta(outputs: dict) -> dict | None:
        """从 history.outputs 提取视频/动图元数据。"""
        for _nid, node_out in (outputs or {}).items():
            if not isinstance(node_out, dict):
                continue
            for key in ("videos", "gifs", "images"):
                items = node_out.get(key)
                if not items:
                    continue
                meta = items[0]
                if isinstance(meta, dict) and meta.get("filename"):
                    return meta
        return None

    async def generate(
        self,
        req: VideoGenRequest,
        *,
        provider_id: str = "",
        model_id: str = "",
        session_id: str = "",
        on_progress: ProgressCb = None,
    ) -> VideoGenResult:
        t0 = time.perf_counter()
        model_name = (
            model_id or req.model_id or "wan2.1_t2v_1.3B_fp16.safetensors"
        ).strip()
        client_id = uuid.uuid4().hex
        size = normalize_size(req.size or self.default_size, self.default_size)
        duration, _ = clamp_duration_sec(
            req.duration_sec or self.default_duration_sec,
            self.default_duration_sec)
        req.size = size
        req.duration_sec = duration
        req.fps = int(req.fps or self.fps)

        template = self._load_workflow()
        workflow = self._apply_workflow(template, req, model_name)

        async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
            if on_progress:
                await on_progress("queued", "已提交本地生视频队列…")
            submit = await client.post(
                f"{self.base_url}/prompt",
                json={"prompt": workflow, "client_id": client_id},
            )
            if submit.status_code >= 400:
                raise RuntimeError(
                    f"ComfyUI 提交失败 HTTP {submit.status_code}: {submit.text[:300]}")
            body = submit.json()
            prompt_id = body.get("prompt_id")
            if not prompt_id:
                raise RuntimeError(f"ComfyUI 未返回 prompt_id: {body}")

            active_jobs.register_job(
                session_id, base_url=self.base_url, prompt_id=prompt_id,
                client_id=client_id, backend="comfyui")
            try:
                if on_progress:
                    await on_progress(
                        "sampling",
                        "本地 Wan 文生视频采样中，常见需要几分钟，请稍候…")
                deadline = time.perf_counter() + self.timeout_sec
                outputs = None
                last_progress_at = 0.0
                while time.perf_counter() < deadline:
                    hist = await client.get(f"{self.base_url}/history/{prompt_id}")
                    if hist.status_code == 200:
                        data = hist.json() or {}
                        entry = data.get(prompt_id) or {}
                        status = entry.get("status") or {}
                        for m in status.get("messages") or []:
                            if isinstance(m, list) and m and m[0] == "execution_error":
                                raise RuntimeError(f"ComfyUI 执行失败: {m}")
                        if entry.get("outputs"):
                            outputs = entry["outputs"]
                            break
                    now = time.perf_counter()
                    if on_progress and now - last_progress_at >= 15.0:
                        elapsed = int(now - t0)
                        await on_progress(
                            "sampling",
                            f"本地生视频进行中（已等待 {elapsed}s，常见 2–6 分钟）…")
                        last_progress_at = now
                    await asyncio.sleep(1.2)
                else:
                    raise TimeoutError(
                        f"ComfyUI 生视频超时（>{int(self.timeout_sec)}s），"
                        "请检查服务、显存与队列")

                media_meta = self._extract_media_meta(outputs or {})
                if not media_meta:
                    raise RuntimeError("ComfyUI 未产出视频输出")

                if on_progress:
                    await on_progress("saving", "正在保存生成视频…")
                view = await client.get(
                    f"{self.base_url}/view",
                    params={
                        "filename": media_meta["filename"],
                        "subfolder": media_meta.get("subfolder") or "",
                        "type": media_meta.get("type") or "output",
                    },
                )
                if view.status_code >= 400:
                    raise RuntimeError(f"下载生成视频失败 HTTP {view.status_code}")
                video_bytes = view.content
                if not video_bytes:
                    raise RuntimeError("ComfyUI 返回空视频内容")
            finally:
                active_jobs.clear_job(session_id)

        out_dir = self.data_dir / "chat_videos"
        out_dir.mkdir(parents=True, exist_ok=True)
        # 统一对外 MP4 名；若上游是 webm 等仍用 .mp4 后缀存（浏览器侧依赖实际编码）
        src_name = str(media_meta.get("filename") or "")
        ext = Path(src_name).suffix.lower() if src_name else ".mp4"
        if ext not in (".mp4", ".webm", ".gif"):
            ext = ".mp4"
        fname = f"genv_{uuid.uuid4().hex[:12]}{ext}"
        (out_dir / fname).write_bytes(video_bytes)

        latency_ms = int((time.perf_counter() - t0) * 1000)
        prompt_text = self._build_prompt_text(req)
        result = VideoGenResult(
            type="generated_video",
            filenames=[fname],
            public_urls=[f"/chat-videos/{fname}"],
            n=1,
            revised_prompt=prompt_text,
            negative_prompt=(req.negative_prompt or "").strip() or _DEFAULT_NEGATIVE,
            size=size,
            duration_sec=float(duration),
            fps=int(req.fps),
            provider_id=provider_id,
            model_id=model_name,
            latency_ms=latency_ms,
            backend="comfyui",
            summary=(
                f"已本地生成 1 条短视频（Wan，约 {duration}s，"
                f"耗时 {max(1, latency_ms // 1000)}s）"
            ),
        )
        logger.info("video_gen ok provider=%s file=%s ms=%s",
                    provider_id, fname, latency_ms)
        return result
