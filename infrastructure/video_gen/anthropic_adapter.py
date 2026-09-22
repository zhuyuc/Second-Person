"""Anthropic 文生视频：探测与文本一样打 /v1/messages。该协议没有视频生成接口。"""
from __future__ import annotations

from pathlib import Path

from infrastructure.image_gen.named_adapters import probe_anthropic

from .profiles import VideoProfile
from .types import VideoGenRequest, VideoGenResult


class AnthropicVideoAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        data_dir: Path,
        profile: VideoProfile,
        model_id: str = "",
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.data_dir = Path(data_dir)
        self.profile = profile
        self.model_id = model_id or ""

    async def probe(self, timeout: float = 12.0) -> dict:
        return await probe_anthropic(
            self.base_url, self.api_key, self.model_id, timeout)

    async def generate(self, req: VideoGenRequest, **_kw) -> VideoGenResult:
        raise RuntimeError(
            "Anthropic 没有视频生成接口。生视频请改选 OpenAI 兼容、自定义或本地 ComfyUI")
