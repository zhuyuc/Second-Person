"""视频参考图：从 chat_images 解析本地文件并编码为可灵可用的 data URI。"""
from __future__ import annotations

import base64
import logging
from pathlib import Path

logger = logging.getLogger("second_person.video_gen.images")

_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}

# 可灵对单图常见上限约 10MB；过大直接拒，避免无意义上传
_MAX_BYTES = 10 * 1024 * 1024


def resolve_chat_image(data_dir: Path, name: str | None) -> Path | None:
    """把 basename 或 /chat-images/xxx 解析到 chat_images 下的真实文件。"""
    if not name:
        return None
    fname = Path(str(name).strip()).name
    if not fname or ".." in fname or "/" in fname or "\\" in fname:
        return None
    root = (Path(data_dir) / "chat_images").resolve()
    path = (root / fname).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    if not path.is_file():
        return None
    return path


def image_file_to_data_uri(path: Path) -> str:
    raw = path.read_bytes()
    if len(raw) > _MAX_BYTES:
        raise RuntimeError(
            f"参考图过大（{len(raw)} 字节，上限 {_MAX_BYTES}），请压缩后再试")
    mime = _MIME.get(path.suffix.lower(), "image/png")
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def pick_first_image_name(names: list[str] | None) -> str | None:
    for n in names or []:
        s = (n or "").strip()
        if s:
            return Path(s).name
    return None
