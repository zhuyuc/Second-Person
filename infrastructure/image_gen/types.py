"""文生图统一结果类型（与供应商解耦）。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

# V1 仅允许方图（方案 §4）
ALLOWED_SIZES = ("1024x1024",)


@dataclass
class ImageGenRequest:
    prompt: str
    negative_prompt: str = ""
    size: str = "1024x1024"
    steps: int = 24
    n: int = 1
    style_hint: str = ""
    model_id: str = ""
    seed: int | None = None


@dataclass
class ImageGenResult:
    type: str = "generated_image"
    filenames: list[str] = field(default_factory=list)
    public_urls: list[str] = field(default_factory=list)
    n: int = 0
    revised_prompt: str | None = None
    negative_prompt: str | None = None
    size: str | None = None
    provider_id: str = ""
    model_id: str = ""
    latency_ms: int | None = None
    backend: str = "comfyui"
    summary: str = ""

    def to_dict(self) -> dict:
        return asdict(self)
