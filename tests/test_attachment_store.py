"""Chat document attachment storage and bounded-context tests."""
from __future__ import annotations

import asyncio
import io

import pytest
from fastapi import UploadFile

from app import attachment_store
from app.attachment_store import (
    INLINE_BUDGET_CHARS, AttachmentError, AttachmentStore,
)
from scheduler.ingest import ExtractResult


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
    assert first["inline_budget_chars"] == INLINE_BUDGET_CHARS


def test_attachment_context_partial_inline_under_shared_budget(tmp_path):
    store = AttachmentStore(tmp_path)
    uploaded = asyncio.run(store.upload_and_parse(
        _upload("notes.txt", ("alpha\n" * 10_000).encode())))

    context, plan = store.build_attachment_context(
        [uploaded["attachment_id"]], inline_budget=500)

    assert plan[0]["mode"] == "partial"
    assert plan[0]["inline_chars"] == 500
    assert "本轮仅注入前 500 字" in context
    assert "fs_read" in context
    assert "parsed.txt" in context
    assert plan[0]["parsed_path"].endswith("parsed.txt")


def test_multi_attachment_shared_35k_budget(tmp_path, monkeypatch):
    store = AttachmentStore(tmp_path)
    monkeypatch.setattr(attachment_store, "INLINE_BUDGET_CHARS", 1000)

    a = asyncio.run(store.upload_and_parse(
        _upload("a.txt", ("A" * 700).encode())))
    b = asyncio.run(store.upload_and_parse(
        _upload("b.txt", ("B" * 700).encode())))

    context, plan = store.build_attachment_context(
        [a["attachment_id"], b["attachment_id"]], inline_budget=1000)

    assert plan[0]["mode"] == "full"
    assert plan[0]["inline_chars"] == 700
    assert plan[1]["mode"] == "partial"
    assert plan[1]["inline_chars"] == 300
    assert "剩余内容未进入上下文" in context
    assert context.count("【附件：") == 2


def test_budget_exhausted_uses_handle_only(tmp_path):
    store = AttachmentStore(tmp_path)
    first = asyncio.run(store.upload_and_parse(
        _upload("full.txt", ("X" * 800).encode())))
    second = asyncio.run(store.upload_and_parse(
        _upload("rest.txt", ("Y" * 200).encode())))

    context, plan = store.build_attachment_context(
        [first["attachment_id"], second["attachment_id"]], inline_budget=800)

    assert plan[0]["mode"] == "full"
    assert plan[1]["mode"] == "handle"
    assert plan[1]["inline_chars"] == 0
    assert "预算已用尽" in context
    assert "rest.txt" in context


def test_upload_rejects_bytes_over_the_configured_limit(tmp_path, monkeypatch):
    store = AttachmentStore(tmp_path)
    monkeypatch.setattr(attachment_store, "SOURCE_FILE_MAX_BYTES", 5)

    with pytest.raises(AttachmentError, match="50MB"):
        asyncio.run(store.upload_and_parse(_upload("too-large.txt", b"123456")))

    assert not list(store.objects.rglob("meta.json"))


def test_failed_parse_cache_is_retried_on_reupload(tmp_path, monkeypatch):
    """Empty first-pass cache must not permanently poison later uploads."""
    store = AttachmentStore(tmp_path)
    content = b"hello from pdf-like bytes"

    async def _empty(_path, *_a, **_k):
        return ExtractResult("", "empty_text")

    async def _ok(_path, *_a, **_k):
        return ExtractResult("recovered text", "ok")

    monkeypatch.setattr("scheduler.ingest.extract_document", _empty)
    first = asyncio.run(store.upload_and_parse(_upload("doc.pdf", content)))
    assert first["parsed"] is False
    assert first["chars"] == 0
    assert first["parse_code"] == "empty_text"

    monkeypatch.setattr("scheduler.ingest.extract_document", _ok)
    second = asyncio.run(store.upload_and_parse(_upload("doc.pdf", content)))
    assert second["attachment_id"] == first["attachment_id"]
    assert second["parsed"] is True
    assert second["text"] == "recovered text"


def test_attachment_context_rejects_invalid_or_expired_reference(tmp_path):
    store = AttachmentStore(tmp_path)

    with pytest.raises(AttachmentError, match="附件引用无效"):
        store.context_for(["invalid"], max_chars=100)

    with pytest.raises(AttachmentError, match="附件不存在"):
        store.context_for(["sha256:" + "a" * 64], max_chars=100)


def test_attachments_read_root_in_policy(tmp_path):
    from tools.fs.policy import PolicyStore

    class _Cfg:
        def get(self, *a, **k):
            return None

    spill = tmp_path / "spills"
    atts = tmp_path / "attachments"
    spill.mkdir()
    atts.mkdir()
    store = PolicyStore(
        db=None, projects_store=None, config=_Cfg(),
        legacy_workspace=tmp_path / "ws",
        spill_read_root=spill,
        attachments_read_root=atts,
    )
    (tmp_path / "ws").mkdir()
    # Force resolve without DB row → default policy with work roots
    store.db = type("DB", (), {
        "query_one": staticmethod(lambda *a, **k: None),
    })()
    policy = store.resolve("missing-session")
    assert atts.resolve() in policy.read_roots
    assert spill.resolve() in policy.read_roots
