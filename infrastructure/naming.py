"""Cross-layer naming helpers (IDs / filenames with no memory-domain dependency).

Memory-specific naming (mem_*, entity_id, domain normalization) stays in
``memory.naming``; functions needed by infrastructure or gateway live here.
"""
from __future__ import annotations

import re
import uuid

from infrastructure.timeutil import now_cst

_ILLEGAL_FS = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def _ts() -> str:
    return now_cst().strftime("%Y%m%d_%H%M%S")


def pending_id() -> str:
    return f"pending_{uuid.uuid4().hex[:8]}"


def im_attachment_name(trace_id: str) -> str:
    return f"reply_{_ts()}_{trace_id[:8]}.md"
