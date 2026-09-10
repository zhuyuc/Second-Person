"""文生视频统一结果类型（与供应商解耦）。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

# V1：仅 480p 竖/横两档（方案 §4）
ALLOWED_SIZES = ("480x832", "832x480")
DEFAULT_SIZE = "480x832"
MIN_DURATION_SEC = 2
MAX_DURATION_SEC = 4
DEFAULT_DURATION_SEC = 3
DEFAULT_FPS = 16


@dataclass
class VideoGenRequest:
    prompt: str
    negative_prompt: str = ""
    size: str = DEFAULT_SIZE
    duration_sec: int = DEFAULT_DURATION_SEC
    fps: int = DEFAULT_FPS
    steps: int = 20
    n: int = 1
    motion_hint: str = ""
    style_hint: str = ""
    model_id: str = ""
    seed: int | None = None

    @property
    def num_frames(self) -> int:
        """由时长与 fps 推导帧数；Wan 常见要求奇数 length。"""
        frames = max(1, int(self.duration_sec) * int(self.fps))
        if frames % 2 == 0:
            frames += 1
        return frames


@dataclass
class VideoGenResult:
    type: str = "generated_video"
    filenames: list[str] = field(default_factory=list)
    public_urls: list[str] = field(default_factory=list)
    poster_url: str | None = None
    n: int = 0
    revised_prompt: str | None = None
    negative_prompt: str | None = None
    size: str | None = None
    duration_sec: float | None = None
    fps: int | None = None
    provider_id: str = ""
    model_id: str = ""
    latency_ms: int | None = None
    backend: str = "comfyui"
    summary: str = ""

    def to_dict(self) -> dict:
        return asdict(self)
