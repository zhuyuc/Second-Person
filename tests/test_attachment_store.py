"""Chat document attachment storage and bounded-context tests."""
from __future__ import annotations

import asyncio
import io

import pytest
from fastapi import UploadFile

from app import attachment_store
from app.attachment_store import AttachmentError, AttachmentStore


def _upload(name: str, content: bytes) -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(content))


def test_upload_is_content_addressed_and_returns_bounded_preview(tmp_path):
    store = AttachmentStore(tmp_path)
    content = (b"first line\n" * 1_000)

    first = asyncio.run(store.upload_and_parse(_upload("notes.txt", content)))
    second = asyncio.run(store.upload_and_parse(_upload("copy.txt", content)))

    assert first["attachment_id"] == second["attachment_id"]
    assert first["parsed"] is True
    assert first["chars"] == len(content.decode())
    assert first["truncated"] is True
    assert len(first["text"]) == attachment_store.PREVIEW_MAX_CHARS


def test_attachment_context_is_resolved_server_side_with_budget(tmp_path):
    store = AttachmentStore(tmp_path)
    uploaded = asyncio.run(store.upload_and_parse(
        _upload("notes.txt", b"alpha\n" * 10_000)))

    context = store.context_for([uploaded["attachment_id"]], max_chars=500)

    assert "【附件：notes.txt】" in context
    assert "alpha" in context
    assert "本轮内容已按上下文预算" in context
    assert len(context) <= 500


def test_upload_rejects_bytes_over_the_configured_limit(tmp_path, monkeypatch):
    store = AttachmentStore(tmp_path)
    monkeypatch.setattr(attachment_store, "SOURCE_FILE_MAX_BYTES", 5)

    with pytest.raises(AttachmentError, match="50MB"):
        asyncio.run(store.upload_and_parse(_upload("too-large.txt", b"123456")))

    assert not list(store.objects.rglob("meta.json"))


def test_attachment_context_rejects_invalid_or_expired_reference(tmp_path):
    store = AttachmentStore(tmp_path)

    with pytest.raises(AttachmentError, match="附件引用无效"):
        store.context_for(["invalid"], max_chars=100)

    with pytest.raises(AttachmentError, match="附件不存在"):
        store.context_for(["sha256:" + "a" * 64], max_chars=100)
