"""Shared MEDIA: attachment marker parsing for IM platform adapters."""
from __future__ import annotations

WEBHOOK_MEDIA_FALLBACK_SUFFIX = (
    "\n\n（回复内容较长，部分以附件形式生成，但因平台限制无法直接发送文件，"
    "请通过 Web 端查看完整回复）"
)


def split_media_marker(text: str) -> tuple[str, str | None]:
    """Strip ``MEDIA:`` lines from outbound text; return (body, attachment_path)."""
    if "MEDIA:" not in text:
        return text, None
    lines = text.splitlines()
    body = "\n".join(line for line in lines if not line.startswith("MEDIA:"))
    media_path = next((line[6:] for line in lines if line.startswith("MEDIA:")), None)
    return body, media_path
