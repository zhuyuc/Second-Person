"""PDF extract_document parse_code and page-OCR gating."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from scheduler.ingest import ExtractResult, _looks_scanned, extract_document


def test_looks_scanned_by_empty_ratio():
    assert _looks_scanned(10, 6, "") is True
    assert _looks_scanned(10, 1, "x" * 500) is False


def test_extract_plain_text_ok(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("hello world", encoding="utf-8")
    result = asyncio.run(extract_document(p))
    assert result.parse_code == "ok"
    assert "hello" in result.text


def test_extract_empty_text_file(tmp_path):
    p = tmp_path / "empty.txt"
    p.write_text("   \n", encoding="utf-8")
    result = asyncio.run(extract_document(p))
    assert result.parse_code == "empty_text"


def test_extract_pdf_document_marks_scanned_when_no_text(tmp_path, monkeypatch):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    async def _fake_pdf(path, image_fn, *, page_ocr="auto"):
        return ExtractResult(
            "", "empty_text_scanned_pdf", page_count=3, empty_page_count=3)

    monkeypatch.setattr(
        "scheduler.ingest._extract_pdf_document", _fake_pdf)
    result = asyncio.run(extract_document(pdf, pdf_page_ocr="off"))
    assert result.parse_code == "empty_text_scanned_pdf"
    assert result.page_count == 3


@pytest.mark.asyncio
async def test_web_fetch_pdf_extract_fn_wired(monkeypatch, tmp_path):
    from tools.web_fetch import FetchResult, web_fetch_result

    raw = b"%PDF-1.4 hello"

    class _Resp:
        url = "https://example.com/a.pdf"
        status_code = 200
        headers = {"content-type": "application/pdf"}
        charset_encoding = "utf-8"

        async def aclose(self):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _StreamCM:
        def __init__(self, resp):
            self._resp = resp

        async def __aenter__(self):
            return self._resp

        async def __aexit__(self, *a):
            return False

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, *a, **k):
            return _StreamCM(_Resp())

    async def _read_capped(response, max_bytes):
        return raw, False

    monkeypatch.setattr("tools.web_fetch.httpx.AsyncClient", _Client)
    monkeypatch.setattr("tools.web_fetch._read_capped", _read_capped)
    async def _not_private(*_a, **_k):
        return False

    monkeypatch.setattr(
        "tools.web_fetch.is_private_host_async", _not_private)

    def _extract(data: bytes) -> str:
        assert data.startswith(b"%PDF")
        return "extracted pdf body " * 10

    result = await web_fetch_result(
        "https://example.com/a.pdf",
        prefer_pdf=False,
        pdf_extract_fn=_extract,
        max_response_bytes=1_000_000,
        max_body_chars=50_000,
    )
    assert isinstance(result, FetchResult)
    assert result.body_kind == "pdf"
    assert "extracted pdf body" in result.text
    assert "未配置解析器" not in result.text
