"""Temporary, content-addressed document attachments for chat turns.

The browser only receives a bounded preview.  Parsed text stays under the
application data directory and is resolved by its opaque identifier when a
turn starts, so large documents never need to round-trip through ``/chat/send``.

Inline policy (方案 §3)：本轮附件正文共享 INLINE_BUDGET_CHARS 总额；超出部分
写入剩余告知 + parsed.txt 路径，供 fs_read / fs_grep 续读。
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
from typing import Any, Awaitable, Callable

from fastapi import UploadFile


SOURCE_FILE_MAX_BYTES = 50 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024
PREVIEW_MAX_CHARS = 8_000
CHAT_CONTEXT_MAX_CHARS = 48_000
INLINE_BUDGET_CHARS = 35_000
CHAT_ATTACHMENT_MAX_COUNT = 5
PARSE_CONCURRENCY = 2
_ATTACHMENT_ID_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
_SAFE_SUFFIX_RE = re.compile(r"^\.[A-Za-z0-9]{1,12}$")

ImageExtractFn = Callable[[Path], Awaitable[str]]


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


def _partial_remainder_notice(
        *, total: int, injected: int, parsed_path: str,
        attachment_id: str) -> str:
    return (
        f"\n\n（附件全文共 {total} 字；本轮仅注入前 {injected} 字。"
        f"剩余内容未进入上下文。\n"
        f"完整解析文本只读路径：{parsed_path}\n"
        f"请使用 fs_read(path, offset, limit) 或 fs_grep 继续读取未覆盖部分后再作答；"
        f"不要假设未注入部分不存在。）\n"
        f"引用：{attachment_id}"
    )


def _handle_only_notice(
        *, total: int, parsed_path: str, attachment_id: str) -> str:
    return (
        f"（全文共 {total} 字，本轮附件预算已用尽，正文未注入。\n"
        f"完整解析文本只读路径：{parsed_path}\n"
        f"请使用 fs_read / fs_grep 按需读取。）\n"
        f"引用：{attachment_id}"
    )


class AttachmentStore:
    """Stores parsed chat documents as short-lived, content-addressed objects."""

    def __init__(
            self, data_dir: str | Path, *,
            image_extract_fn: ImageExtractFn | None = None,
            pdf_page_ocr: str = "auto"):
        self.root = Path(data_dir) / "temp" / "attachments"
        self.objects = self.root / "objects"
        self.staging = self.root / ".staging"
        self.objects.mkdir(parents=True, exist_ok=True)
        self.staging.mkdir(parents=True, exist_ok=True)
        self._parse_slots = asyncio.Semaphore(PARSE_CONCURRENCY)
        self.image_extract_fn = image_extract_fn
        self.pdf_page_ocr = pdf_page_ocr

    def parsed_path(self, attachment_id: str) -> Path:
        """Absolute path to the durable parsed.txt for fs_read."""
        return (self._object_dir(attachment_id) / "parsed.txt").resolve()

    def object_dir(self, attachment_id: str) -> Path:
        return self._object_dir(attachment_id).resolve()

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
                # Hit only when a prior parse actually produced text. Empty /
                # failed caches (e.g. missing pdfplumber at first upload) must
                # be discarded so a later retry can re-extract.
                if existing.get("parsed"):
                    shutil.rmtree(work, ignore_errors=True)
                    return existing
                shutil.rmtree(target, ignore_errors=True)

            from scheduler.ingest import extract_document
            async with self._parse_slots:
                result = await extract_document(
                    source, self.image_extract_fn,
                    pdf_page_ocr=self.pdf_page_ocr)
            text = result.text or ""
            parse_code = result.parse_code
            if parse_code == "ok" and not text.strip():
                parse_code = "empty_text"
            parsed = parse_code == "ok" and bool(text.strip())
            parsed_path = work / "parsed.txt"
            await asyncio.to_thread(parsed_path.write_text, text, "utf-8")
            meta = {
                "attachment_id": f"sha256:{key}",
                "filename": filename,
                "source_name": source.name,
                "size_bytes": total,
                "chars": len(text),
                "parsed": parsed,
                "parse_code": parse_code,
                "page_count": result.page_count,
                "empty_page_count": result.empty_page_count,
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

    def build_attachment_context(
            self, attachment_ids: list[str], *,
            inline_budget: int | None = None,
            partial_inline: bool = True,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Assemble prompt context under a shared body-char budget.

        Returns ``(context_text, plan)`` where each plan row describes how much
        of that attachment was inlined vs deferred to fs_read.
        """
        if len(attachment_ids) > CHAT_ATTACHMENT_MAX_COUNT:
            raise AttachmentError(
                f"一次最多引用 {CHAT_ATTACHMENT_MAX_COUNT} 个附件")
        body_budget = max(
            0, INLINE_BUDGET_CHARS if inline_budget is None else inline_budget)
        remaining = body_budget
        blocks: list[str] = []
        plan: list[dict[str, Any]] = []
        seen: set[str] = set()

        for attachment_id in attachment_ids:
            if attachment_id in seen:
                continue
            seen.add(attachment_id)
            obj = self._object_dir(attachment_id)
            meta = self._read_meta(obj)
            parsed_file = (obj / "parsed.txt").resolve()
            try:
                text = (obj / "parsed.txt").read_text(encoding="utf-8")
            except OSError as exc:
                raise AttachmentError("附件已过期，请重新上传") from exc

            filename = meta["filename"]
            header = f"【附件：{filename}】\n"
            total_chars = len(text)
            path_str = str(parsed_file)

            if not text.strip():
                code = meta.get("parse_code") or "empty_text"
                hint = {
                    "empty_text_scanned_pdf": "（未能解析出文本：疑似扫描件，请启用页 OCR 或先转可检索 PDF）",
                    "encrypted": "（未能解析出文本：PDF 已加密，请解密后重新上传）",
                    "corrupt": "（未能解析出文本：文件损坏或无法打开）",
                }.get(code, "（未能解析出文本内容）")
                block = f"{header}{hint}"
                blocks.append(block)
                plan.append({
                    "attachment_id": attachment_id,
                    "filename": filename,
                    "mode": "failed",
                    "total_chars": total_chars,
                    "inline_chars": 0,
                    "parse_code": code,
                    "parsed_path": path_str,
                })
                continue

            if remaining <= 0:
                block = f"{header}{_handle_only_notice(total=total_chars, parsed_path=path_str, attachment_id=attachment_id)}"
                blocks.append(block)
                plan.append({
                    "attachment_id": attachment_id,
                    "filename": filename,
                    "mode": "handle",
                    "total_chars": total_chars,
                    "inline_chars": 0,
                    "parsed_path": path_str,
                })
                continue

            if total_chars <= remaining:
                block = f"{header}{text}"
                remaining -= total_chars
                blocks.append(block)
                plan.append({
                    "attachment_id": attachment_id,
                    "filename": filename,
                    "mode": "full",
                    "total_chars": total_chars,
                    "inline_chars": total_chars,
                    "parsed_path": path_str,
                })
                continue

            # Cannot fit entirely.
            if not partial_inline:
                block = f"{header}{_handle_only_notice(total=total_chars, parsed_path=path_str, attachment_id=attachment_id)}"
                remaining = 0
                blocks.append(block)
                plan.append({
                    "attachment_id": attachment_id,
                    "filename": filename,
                    "mode": "handle",
                    "total_chars": total_chars,
                    "inline_chars": 0,
                    "parsed_path": path_str,
                })
                continue

            injected = remaining
            body = text[:injected]
            notice = _partial_remainder_notice(
                total=total_chars, injected=injected,
                parsed_path=path_str, attachment_id=attachment_id)
            block = f"{header}{body}{notice}"
            remaining = 0
            blocks.append(block)
            plan.append({
                "attachment_id": attachment_id,
                "filename": filename,
                "mode": "partial",
                "total_chars": total_chars,
                "inline_chars": injected,
                "parsed_path": path_str,
            })

        context = "\n\n".join(blocks)
        # Safety cap on the serialized prompt blob (headers + notices).
        if len(context) > CHAT_CONTEXT_MAX_CHARS:
            context = (
                context[:CHAT_CONTEXT_MAX_CHARS]
                + "\n\n（附件上下文序列化超过安全上限，已截断；"
                  "请优先用 fs_read 读取各附件 parsed.txt。）"
            )
        return context, plan

    def context_for(self, attachment_ids: list[str], *, max_chars: int) -> str:
        """Backward-compatible wrapper; ``max_chars`` retained but body budget
        is ``INLINE_BUDGET_CHARS`` (scheme §3). ``max_chars`` only shrinks the
        inline body budget when callers pass a tighter value.
        """
        budget = INLINE_BUDGET_CHARS
        if max_chars is not None and max_chars >= 0:
            budget = min(budget, max_chars)
        text, _plan = self.build_attachment_context(
            attachment_ids, inline_budget=budget)
        return text

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
        parsed = bool(text.strip()) and meta.get("parse_code", "ok") == "ok"
        if "parsed" in meta:
            parsed = bool(meta["parsed"]) and bool(text.strip())
        return {
            "attachment_id": meta["attachment_id"],
            "filename": display_name,
            "chars": len(text),
            "text": preview,
            "truncated": len(preview) < len(text),
            "parsed": parsed,
            "parse_code": meta.get("parse_code") or (
                "ok" if parsed else "empty_text"),
            "inline_budget_chars": INLINE_BUDGET_CHARS,
            "parsed_path": str((obj / "parsed.txt").resolve()),
        }
