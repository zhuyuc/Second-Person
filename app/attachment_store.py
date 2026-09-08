"""Temporary, content-addressed document attachments for chat turns.

The browser only receives a bounded preview.  Parsed text stays under the
application data directory and is resolved by its opaque identifier when a
turn starts, so large documents never need to round-trip through ``/chat/send``.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import UploadFile


SOURCE_FILE_MAX_BYTES = 50 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024
PREVIEW_MAX_CHARS = 8_000
CHAT_CONTEXT_MAX_CHARS = 48_000
CHAT_ATTACHMENT_MAX_COUNT = 5
PARSE_CONCURRENCY = 2
_ATTACHMENT_ID_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
_SAFE_SUFFIX_RE = re.compile(r"^\.[A-Za-z0-9]{1,12}$")


class AttachmentError(ValueError):
    """A client-visible attachment admission or lookup failure."""


def _safe_filename(value: str | None) -> str:
    name = Path(value or "file").name.strip()
    return (name or "file")[:255]


def _safe_suffix(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    return suffix if _SAFE_SUFFIX_RE.fullmatch(suffix) else ""


def _attachment_digest(attachment_id: str) -> str:
    match = _ATTACHMENT_ID_RE.fullmatch(str(attachment_id or ""))
    if not match:
        raise AttachmentError("附件引用无效")
    return match.group(1)


class AttachmentStore:
    """Stores parsed chat documents as short-lived, content-addressed objects."""

    def __init__(self, data_dir: str | Path):
        self.root = Path(data_dir) / "temp" / "attachments"
        self.objects = self.root / "objects"
        self.staging = self.root / ".staging"
        self.objects.mkdir(parents=True, exist_ok=True)
        self.staging.mkdir(parents=True, exist_ok=True)
        self._parse_slots = asyncio.Semaphore(PARSE_CONCURRENCY)

    async def upload_and_parse(self, file: UploadFile) -> dict[str, Any]:
        """Receive at most 50 MiB, parse once, then publish an opaque reference."""
        declared_size = file.size
        if declared_size is not None and declared_size > SOURCE_FILE_MAX_BYTES:
            await file.close()
            raise AttachmentError("文件超过 50MB 上限")

        filename = _safe_filename(file.filename)
        suffix = _safe_suffix(filename)
        work = self.staging / uuid.uuid4().hex
        work.mkdir(mode=0o700)
        source = work / f"source{suffix}"
        total = 0
        digest = hashlib.sha256()
        try:
            with source.open("wb") as handle:
                while True:
                    chunk = await file.read(UPLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > SOURCE_FILE_MAX_BYTES:
                        raise AttachmentError("文件超过 50MB 上限")
                    digest.update(chunk)
                    handle.write(chunk)

            key = digest.hexdigest()
            target = self.objects / key[:2] / key
            if target.exists():
                existing = self._result_from_object(target, filename)
                shutil.rmtree(work, ignore_errors=True)
                return existing

            from scheduler.ingest import extract_text
            # PDF/DOCX extraction can be CPU- and memory-intensive. Keep a
            # small queue instead of allowing simultaneous 50 MiB parses.
            async with self._parse_slots:
                text = await asyncio.to_thread(extract_text, source)
            parsed = bool((text or "").strip())
            parsed_path = work / "parsed.txt"
            await asyncio.to_thread(parsed_path.write_text, text or "", "utf-8")
            meta = {
                "attachment_id": f"sha256:{key}",
                "filename": filename,
                "source_name": source.name,
                "size_bytes": total,
                "chars": len(text or ""),
                "parsed": parsed,
                "created_at": int(time.time()),
            }
            await asyncio.to_thread(
                (work / "meta.json").write_text,
                json.dumps(meta, ensure_ascii=False), "utf-8")
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(work, target)
            except FileExistsError:
                shutil.rmtree(work, ignore_errors=True)
            return self._result_from_object(target, filename)
        except Exception:
            shutil.rmtree(work, ignore_errors=True)
            raise
        finally:
            await file.close()

    def context_for(self, attachment_ids: list[str], *, max_chars: int) -> str:
        """Build a bounded model-visible context from uploaded document references."""
        if len(attachment_ids) > CHAT_ATTACHMENT_MAX_COUNT:
            raise AttachmentError(f"一次最多引用 {CHAT_ATTACHMENT_MAX_COUNT} 个附件")
        remaining = max(0, min(max_chars, CHAT_CONTEXT_MAX_CHARS))
        blocks: list[str] = []
        seen: set[str] = set()
        for attachment_id in attachment_ids:
            if remaining <= 0:
                break
            if attachment_id in seen:
                continue
            seen.add(attachment_id)
            obj = self._object_dir(attachment_id)
            meta = self._read_meta(obj)
            try:
                text = (obj / "parsed.txt").read_text(encoding="utf-8")
            except OSError as exc:
                raise AttachmentError("附件已过期，请重新上传") from exc
            header = f"【附件：{meta['filename']}】\n"
            if not text.strip():
                block = f"{header}（未能解析出文本内容）"
                blocks.append(block[:remaining])
                remaining -= len(block[:remaining])
                continue
            if len(header) + len(text) <= remaining:
                block = f"{header}{text}"
            else:
                suffix = (f"\n\n（附件全文共 {len(text)} 字；本轮内容已按上下文预算截断，"
                          f"引用：{attachment_id}）")
                allowance = max(0, remaining - len(header) - len(suffix))
                block = f"{header}{text[:allowance]}{suffix}"
                # A long filename can consume the entire remaining budget.
                block = block[:remaining]
            blocks.append(block)
            remaining -= len(block)
        return "\n\n".join(blocks)

    def cleanup(self, days: int = 7) -> int:
        """Remove complete expired objects and abandoned staging directories."""
        cutoff = time.time() - days * 86400
        removed = 0
        for base in (self.staging, self.objects):
            if not base.exists():
                continue
            entries = (base.iterdir() if base == self.staging else
                       (p for prefix in base.iterdir() if prefix.is_dir()
                        for p in prefix.iterdir() if p.is_dir()))
            for path in entries:
                try:
                    if path.stat().st_mtime < cutoff:
                        shutil.rmtree(path, ignore_errors=True)
                        removed += 1
                except OSError:
                    continue
        return removed

    def _object_dir(self, attachment_id: str) -> Path:
        digest = _attachment_digest(attachment_id)
        obj = self.objects / digest[:2] / digest
        if not obj.is_dir():
            raise AttachmentError("附件不存在或已过期，请重新上传")
        return obj

    @staticmethod
    def _read_meta(obj: Path) -> dict[str, Any]:
        try:
            meta = json.loads((obj / "meta.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AttachmentError("附件记录损坏，请重新上传") from exc
        if not isinstance(meta, dict) or not isinstance(meta.get("filename"), str):
            raise AttachmentError("附件记录损坏，请重新上传")
        return meta

    def _result_from_object(self, obj: Path, display_name: str) -> dict[str, Any]:
        meta = self._read_meta(obj)
        try:
            text = (obj / "parsed.txt").read_text(encoding="utf-8")
        except OSError as exc:
            raise AttachmentError("附件解析结果不可用，请重新上传") from exc
        preview = text[:PREVIEW_MAX_CHARS]
        return {
            "attachment_id": meta["attachment_id"],
            "filename": display_name,
            "chars": len(text),
            "text": preview,
            "truncated": len(preview) < len(text),
            "parsed": bool(text.strip()),
        }
