"""文生视频能力表：本地 ComfyUI 与云端（可灵官方形态）参数隔离。"""
from __future__ import annotations

from dataclasses import dataclass

from .types import DEFAULT_DURATION_SEC, DEFAULT_SIZE, MAX_DURATION_SEC, MIN_DURATION_SEC


@dataclass(frozen=True)
class VideoProfile:
    engine: str
    local_gpu: bool
    min_duration: int
    max_duration: int
    default_duration: int
    allowed_sizes: tuple[str, ...]
    default_size: str
    timeout_sec: float
    refine: str  # wan_en | off
    aspect_by_size: dict[str, str]


LOCAL_PROFILE = VideoProfile(
    engine="comfyui",
    local_gpu=True,
    min_duration=MIN_DURATION_SEC,
    max_duration=MAX_DURATION_SEC,
    default_duration=DEFAULT_DURATION_SEC,
    allowed_sizes=("480x832", "832x480"),
    default_size=DEFAULT_SIZE,
    timeout_sec=600.0,
    refine="wan_en",
    aspect_by_size={"480x832": "9:16", "832x480": "16:9", "1:1": "9:16"},
)

CLOUD_PROFILE = VideoProfile(
    engine="cloud",
    local_gpu=False,
    min_duration=3,
    max_duration=15,
    default_duration=5,
    allowed_sizes=("480x832", "832x480", "1:1", "9:16", "16:9"),
    default_size="480x832",
    # 与主对话 generate_video 工具预算对齐（tool_executor 用 video_gen_timeout_sec）
    timeout_sec=600.0,
    refine="off",
    aspect_by_size={
        "480x832": "9:16", "9:16": "9:16",
        "832x480": "16:9", "16:9": "16:9",
        "1:1": "1:1", "1024x1024": "1:1",
    },
)


def _video_gen_timeout_sec(config) -> float:
    """出片等待上限：主对话工具与云端/本地适配器共用同一配置。"""
    if config is None:
        return 600.0
    try:
        return max(60.0, float(config.get("video_gen_timeout_sec", 600) or 600))
    except (TypeError, ValueError):
        return 600.0


def video_profile_for(snap, config=None) -> VideoProfile:
    ptype = (getattr(snap, "provider_type", "") or "").strip().lower()
    timeout = _video_gen_timeout_sec(config)
    if ptype == "comfyui":
        max_dur = MAX_DURATION_SEC
        default_dur = DEFAULT_DURATION_SEC
        if config is not None:
            max_dur = min(
                MAX_DURATION_SEC,
                int(config.get("video_gen_max_duration_sec", MAX_DURATION_SEC)
                    or MAX_DURATION_SEC))
            default_dur = int(
                config.get("video_gen_default_duration_sec", DEFAULT_DURATION_SEC)
                or DEFAULT_DURATION_SEC)
        return VideoProfile(
            engine="comfyui",
            local_gpu=True,
            min_duration=MIN_DURATION_SEC,
            max_duration=max_dur,
            default_duration=min(max(default_dur, MIN_DURATION_SEC), max_dur),
            allowed_sizes=LOCAL_PROFILE.allowed_sizes,
            default_size=LOCAL_PROFILE.default_size,
            timeout_sec=timeout,
            refine="wan_en" if (
                config is None or config.get("video_gen_refine_enabled", True)
            ) else "off",
            aspect_by_size=LOCAL_PROFILE.aspect_by_size,
        )
    # 云端：时长上下限仍用 CLOUD_PROFILE；等待上限必须跟主对话工具预算一致，
    # 否则云端还在渲染时本地会先 TimeoutError 判死刑。
    if config is None and timeout == CLOUD_PROFILE.timeout_sec:
        return CLOUD_PROFILE
    return VideoProfile(
        engine=CLOUD_PROFILE.engine,
        local_gpu=False,
        min_duration=CLOUD_PROFILE.min_duration,
        max_duration=CLOUD_PROFILE.max_duration,
        default_duration=CLOUD_PROFILE.default_duration,
        allowed_sizes=CLOUD_PROFILE.allowed_sizes,
        default_size=CLOUD_PROFILE.default_size,
        timeout_sec=timeout,
        refine=CLOUD_PROFILE.refine,
        aspect_by_size=CLOUD_PROFILE.aspect_by_size,
    )


def clamp_duration(profile: VideoProfile, value, default: int | None = None) -> tuple[int, bool]:
    fallback = int(default if default is not None else profile.default_duration)
    try:
        raw = int(float(value))
    except (TypeError, ValueError):
        return fallback, True
    clamped = max(profile.min_duration, min(profile.max_duration, raw))
    return clamped, clamped != raw


def normalize_video_size(profile: VideoProfile, size: str | None) -> str:
    raw = (size or profile.default_size).strip().lower().replace("*", "x")
    if raw in profile.allowed_sizes:
        return raw
    if raw in ("portrait", "vertical", "竖屏"):
        return "480x832"
    if raw in ("landscape", "horizontal", "横屏"):
        return "832x480"
    return profile.default_size


def aspect_ratio_of(profile: VideoProfile, size: str) -> str:
    return profile.aspect_by_size.get(
        (size or "").strip().lower(),
        profile.aspect_by_size.get(profile.default_size, "9:16"),
    )


def capability_hint(profile: VideoProfile) -> str:
    sizes = " / ".join(profile.allowed_sizes[:3])
    if profile.local_gpu:
        return (
            f"当前文生视频：本地短片；{profile.min_duration}–{profile.max_duration} 秒；"
            f"{sizes}；无配音；可能需要数分钟；同轮不要既出图又出视频。"
        )
    return (
        f"当前文生视频：云端；{profile.min_duration}–{profile.max_duration} 秒，"
        f"默认 {profile.default_duration} 秒；画幅 {sizes}；生成完成后气泡直接播放。"
    )
