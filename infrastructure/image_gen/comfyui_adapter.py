"""ComfyUI HTTP Adapter：提交工作流、轮询、取图、落盘（V1 仅 txt2img）。"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable

import httpx

from . import active_jobs
from .types import ALLOWED_SIZES, ImageGenRequest, ImageGenResult

logger = logging.getLogger("second_person.image_gen.comfyui")

ProgressCb = Callable[[str, str], Awaitable[None]] | None

_DEFAULT_NEGATIVE = (
    "lowres, bad anatomy, bad hands, text, error, missing fingers, "
    "extra digit, fewer digits, cropped, worst quality, low quality, "
    "jpeg artifacts, signature, watermark, username, blurry"
)

_STYLE_PREFIX = {
    "photo": "photorealistic, natural lighting, ",
    "illustration": "digital illustration, clean lines, ",
    "sketch": "pencil sketch, hand-drawn, ",
    "anime": "anime style, vibrant colors, ",
}


async def probe_comfyui(base_url: str, timeout: float = 8.0) -> dict:
    """健康检查：GET /system_stats。"""
    url = base_url.rstrip("/") + "/system_stats"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
        if resp.status_code == 200:
            return {"ok": True}
        return {"ok": False, "error": f"ComfyUI 返回 HTTP {resp.status_code}"}
    except httpx.ConnectError:
        return {"ok": False, "error": "本地生图服务未启动（无法连接 ComfyUI）"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


class ComfyUIAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        workflow_path: Path,
        data_dir: Path,
        timeout_sec: float = 180.0,
        default_steps: int = 24,
        default_size: str = "1024x1024",
        prompt_max_chars: int = 1500,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.workflow_path = Path(workflow_path)
        self.data_dir = Path(data_dir)
        self.timeout_sec = timeout_sec
        self.default_steps = default_steps
        self.default_size = default_size
        self.prompt_max_chars = prompt_max_chars

    def _load_workflow(self) -> dict:
        if not self.workflow_path.exists():
            raise FileNotFoundError(
                f"ComfyUI 工作流模板不存在：{self.workflow_path}")
        return json.loads(self.workflow_path.read_text(encoding="utf-8"))

    @staticmethod
    def _parse_size(size: str | None) -> tuple[int, int]:
        raw = (size or "1024x1024").lower().replace("*", "x")
        if raw not in ALLOWED_SIZES:
            raw = "1024x1024"
        w_s, h_s = raw.split("x", 1)
        return int(w_s), int(h_s)

    def _build_prompt_text(self, req: ImageGenRequest) -> str:
        prefix = _STYLE_PREFIX.get((req.style_hint or "").strip().lower(), "")
        return f"{prefix}{(req.prompt or '').strip()}"[: self.prompt_max_chars]

    def _apply_workflow(self, template: dict, req: ImageGenRequest,
                        ckpt_name: str) -> dict:
        wf = copy.deepcopy(template)
        prompt = self._build_prompt_text(req)
        negative = (req.negative_prompt or "").strip() or _DEFAULT_NEGATIVE
        width, height = self._parse_size(req.size or self.default_size)
        steps = int(req.steps or self.default_steps)
        seed = req.seed if req.seed is not None else (uuid.uuid4().int & 0xFFFFFFFF)
        if "4" in wf and "inputs" in wf["4"]:
            wf["4"]["inputs"]["ckpt_name"] = ckpt_name
        if "5" in wf and "inputs" in wf["5"]:
            wf["5"]["inputs"]["width"] = width
            wf["5"]["inputs"]["height"] = height
            wf["5"]["inputs"]["batch_size"] = 1
        if "6" in wf and "inputs" in wf["6"]:
            wf["6"]["inputs"]["text"] = prompt
        if "7" in wf and "inputs" in wf["7"]:
            wf["7"]["inputs"]["text"] = negative
        if "3" in wf and "inputs" in wf["3"]:
            wf["3"]["inputs"]["steps"] = steps
            wf["3"]["inputs"]["seed"] = seed
        return wf

    async def generate(
        self,
        req: ImageGenRequest,
        *,
        provider_id: str = "",
        model_id: str = "",
        session_id: str = "",
        on_progress: ProgressCb = None,
    ) -> ImageGenResult:
        t0 = time.perf_counter()
        ckpt = (model_id or req.model_id or "sd_xl_base_1.0.safetensors").strip()
        client_id = uuid.uuid4().hex
        template = self._load_workflow()
        workflow = self._apply_workflow(template, req, ckpt)

        async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
            if on_progress:
                await on_progress("queued", "已提交本地生图队列…")
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
                        "sampling", "本地 SDXL 采样中，可能需要十几秒到几十秒…")
                deadline = time.perf_counter() + self.timeout_sec
                outputs = None
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
                    await asyncio.sleep(0.8)
                else:
                    raise TimeoutError(
                        f"ComfyUI 生成超时（>{int(self.timeout_sec)}s），请检查服务与显存")

                image_meta = None
                for _nid, node_out in (outputs or {}).items():
                    images = (node_out.get("images")
                              if isinstance(node_out, dict) else None)
                    if images:
                        image_meta = images[0]
                        break
                if not image_meta:
                    raise RuntimeError("ComfyUI 未产出图片输出")

                if on_progress:
                    await on_progress("saving", "正在保存生成图…")
                view = await client.get(
                    f"{self.base_url}/view",
                    params={
                        "filename": image_meta["filename"],
                        "subfolder": image_meta.get("subfolder") or "",
                        "type": image_meta.get("type") or "output",
                    },
                )
                if view.status_code >= 400:
                    raise RuntimeError(f"下载生成图失败 HTTP {view.status_code}")
                img_bytes = view.content
            finally:
                active_jobs.clear_job(session_id)

        out_dir = self.data_dir / "chat_images"
        out_dir.mkdir(parents=True, exist_ok=True)
        fname = f"gen_{uuid.uuid4().hex[:12]}.png"
        (out_dir / fname).write_bytes(img_bytes)

        latency_ms = int((time.perf_counter() - t0) * 1000)
        prompt_text = self._build_prompt_text(req)
        result = ImageGenResult(
            type="generated_image",
            filenames=[fname],
            public_urls=[f"/chat-images/{fname}"],
            n=1,
            revised_prompt=prompt_text,
            negative_prompt=(req.negative_prompt or "").strip() or _DEFAULT_NEGATIVE,
            size="1024x1024",
            provider_id=provider_id,
            model_id=ckpt,
            latency_ms=latency_ms,
            backend="comfyui",
            summary=(
                f"已本地生成 1 张图（SDXL，约 {max(1, latency_ms // 1000)}s）"
            ),
        )
        logger.info("image_gen ok provider=%s file=%s ms=%s",
                    provider_id, fname, latency_ms)
        return result
