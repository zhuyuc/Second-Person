"""
命名与标识规则（开发文档 §6.18）—— 全系统唯一实现，任何生成 id/文件名处都调用此模块。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from infrastructure.timeutil import now_cst

# md 文件名与 domain 目录中需要替换为下划线的非法字符
_ILLEGAL_FS = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def memory_id(seq: int) -> str:
    """mem_{6位零填充自增}。"""
    return f"mem_{seq:06d}"


def entity_id(name: str) -> str:
    """ent_{规范化后 SHA1 前 10 位}。规范化=去首尾空格+转小写+全角转半角。"""
    norm = normalize_entity_name(name)
    return "ent_" + hashlib.sha1(norm.encode("utf-8")).hexdigest()[:10]


def normalize_entity_name(name: str) -> str:
    # NFKC 把全角转半角，再去空格转小写
    return unicodedata.normalize("NFKC", name).strip().lower()


def normalize_domain(domain: str) -> str:
    """domain：小写英文或中文，长度 1-32，不含路径分隔符与空格（空格转下划线）。

    写盘前的最后一道防线：LLM 蒸馏可能产出含反斜杠/多段式的脏 domain
    （如"组织行为学 \\ 人力资源管理"），Windows 下 mkdir 会失败并造成
    写请求永久重试，必须规范化后才能拼入路径。
    """
    d = unicodedata.normalize("NFKC", str(domain)).strip() if domain else ""
    d = _ILLEGAL_FS.sub("_", d).replace(" ", "_")
    d = re.sub("_+", "_", d).strip("_")  # 压缩连续下划线并去两端
    d = d.lower() if d.isascii() else d
    if not d:
        d = "general"
    return d[:32]


def memory_filename(mid: str, title: str, existing: set[str] | None = None) -> str:
    """mem_{id}_{title前20字符}.md，非法字符转下划线，冲突追加 _2/_3。"""
    safe_title = _ILLEGAL_FS.sub("_", title)[:20].strip() or "untitled"
    base = f"{mid}_{safe_title}"
    name = f"{base}.md"
    if existing:
        n = 2
        while name in existing:
            name = f"{base}_{n}.md"
            n += 1
    return name


def _ts() -> str:
    return now_cst().strftime("%Y%m%d_%H%M%S")


def backup_filename(label: str | None = None) -> str:
    suffix = f"_{_ILLEGAL_FS.sub('_', label)}" if label else ""
    return f"sp_backup_{_ts()}{suffix}.zip"


def im_attachment_name(trace_id: str) -> str:
    return f"reply_{_ts()}_{trace_id[:8]}.md"


def suggestion_id() -> str:
    return f"sug_{uuid.uuid4().hex[:8]}"


def task_id(task_type: str) -> str:
    return f"{task_type}_{_ts()}_{uuid.uuid4().hex[:8]}"


def pending_id() -> str:
    return f"pending_{uuid.uuid4().hex[:8]}"


def provider_id(seq: int) -> str:
    return f"prov_{seq:03d}"


def raw_doc_id(seq: int) -> str:
    return f"doc_{seq:04d}"


def session_id(seq: int) -> str:
    return f"sess_{seq:04d}"
