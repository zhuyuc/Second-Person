"""web_fetch 有界抓取 + spill 溢写回归。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from tools import hooks
from tools.web_fetch import (
    FetchError, format_fetch_result, prefer_pdf_url, web_fetch, web_fetch_result,
)


def test_prefer_pdf_url_arxiv():
    src = "https://arxiv.org/html/2401.12345v1"
    out, note = prefer_pdf_url(src)
    assert out == "https://arxiv.org/pdf/2401.12345v1"
    assert note and "→" in note
    same, note2 = prefer_pdf_url("https://example.com/page")
    assert same == "https://example.com/page"
    assert note2 is None
    disabled, note3 = prefer_pdf_url(src, enabled=False)
    assert disabled == src and note3 is None


def test_format_fetch_result_truncation_footer():
    from tools.web_fetch import FetchResult
    r = FetchResult(
        url="https://a.test", final_url="https://a.test", status_code=200,
        content_type="text/plain", body_kind="text", text="hello",
        truncated=True, reason="max_chars", bytes_received=5)
    out = format_fetch_result(r)
    assert "已抓取 https://a.test" in out
    assert "内容已截断：原因=max_chars" in out
    assert "fs_read" in out


@pytest.mark.asyncio
async def test_web_fetch_stream_byte_cap(monkeypatch):
    body = b"A" * 5000

    async def handler(request: httpx.Request) -> httpx.Response:
        # 无 Content-Length，走流式截断（有 CL 且超限会直接拒绝）
        async def stream():
            yield body
        return httpx.Response(
            200, content=stream(),
            headers={"content-type": "text/plain; charset=utf-8"})

    transport = httpx.MockTransport(handler)

    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr("tools.web_fetch.httpx.AsyncClient", client_factory)

    result = await web_fetch_result(
        "https://example.com/big", allow_private=True,
        max_response_bytes=1000, max_body_chars=10_000, prefer_pdf=False)
    assert result.truncated is True
    assert result.reason == "max_bytes"
    assert result.bytes_received == 1000
    assert len(result.text) == 1000


@pytest.mark.asyncio
async def test_web_fetch_content_length_too_large(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"nope",
            headers={"content-type": "text/plain", "content-length": "99999999"})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr("tools.web_fetch.httpx.AsyncClient", client_factory)

    with pytest.raises(FetchError, match="Content-Length"):
        await web_fetch_result(
            "https://example.com/huge", allow_private=True,
            max_response_bytes=1000, prefer_pdf=False)


@pytest.mark.asyncio
async def test_web_fetch_char_cap_and_format(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=("字" * 200).encode("utf-8"),
            headers={"content-type": "text/plain; charset=utf-8"})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr("tools.web_fetch.httpx.AsyncClient", client_factory)

    text = await web_fetch(
        "https://example.com/chars", allow_private=True,
        max_response_bytes=1_000_000, max_body_chars=50, prefer_pdf=False)
    assert "已抓取" in text
    assert "内容已截断：原因=max_chars" in text


@pytest.mark.asyncio
async def test_web_fetch_html_denoise(monkeypatch):
    html = (
        "<html><head><script>secret()</script></head>"
        "<body><nav>nav</nav><p>正文段落</p><footer>f</footer></body></html>"
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=html.encode("utf-8"),
            headers={"content-type": "text/html; charset=utf-8"})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr("tools.web_fetch.httpx.AsyncClient", client_factory)

    result = await web_fetch_result(
        "https://example.com/doc", allow_private=True, prefer_pdf=False)
    assert result.body_kind == "html"
    assert "正文段落" in result.text
    assert "secret" not in result.text


def test_maybe_spill_writes_preview_and_file(tmp_path: Path):
    big = "X" * 5000
    out = hooks.maybe_spill_result(
        big, data_dir=tmp_path, session_id="sess1", tool_name="web_fetch",
        call_id="c1", max_inline_bytes=800, max_file_bytes=100_000)
    assert out != big
    assert len(out.encode("utf-8")) <= 800
    assert "完整结果：" in out
    assert "fs_read" in out
    spills = list((tmp_path / "temp" / "spills" / "sess1").glob("web_fetch_*.txt"))
    assert len(spills) == 1
    assert spills[0].read_text(encoding="utf-8") == big


def test_maybe_spill_skips_fs_read(tmp_path: Path):
    big = "Y" * 5000
    out = hooks.maybe_spill_result(
        big, data_dir=tmp_path, session_id="s", tool_name="fs_read",
        call_id="c", max_inline_bytes=100)
    assert out == big
    assert not (tmp_path / "temp" / "spills").exists()


def test_maybe_spill_disabled(tmp_path: Path):
    big = "Z" * 5000
    out = hooks.maybe_spill_result(
        big, data_dir=tmp_path, session_id="s", tool_name="web_fetch",
        call_id="c", max_inline_bytes=None)
    assert out == big


def test_resolve_spill_inline_cap_defaults_and_disable():
    class Cfg:
        def __init__(self, raw):
            self._raw = raw

        def get_raw(self, key, default=None):
            return self._raw.get(key, default)

    assert hooks.resolve_spill_inline_cap(Cfg({})) > 0
    assert hooks.resolve_spill_inline_cap(Cfg({"tool_spill": {"max_inline_bytes": 0}})) is None
    assert hooks.resolve_spill_inline_cap(
        Cfg({"tool_spill": {"max_inline_bytes": 1234}})) == 1234


def test_cleanup_temp_spills(tmp_path: Path):
    import os
    import time
    spill = tmp_path / "temp" / "spills" / "s"
    spill.mkdir(parents=True)
    f = spill / "old.txt"
    f.write_text("x", encoding="utf-8")
    old = time.time() - 10 * 86400
    os.utime(f, (old, old))
    n = hooks.cleanup_temp_spills(tmp_path, days=7)
    assert n >= 1
    assert not f.exists()


def test_policy_includes_spill_read_root(tmp_path: Path):
    from tools.fs.policy import PolicyStore

    class FakeDb:
        def query_one(self, *a, **k):
            return None

    class Cfg:
        def get_raw(self, *a, **k):
            return None

    ws = tmp_path / "workspace"
    ws.mkdir()
    spill = tmp_path / "temp" / "spills"
    spill.mkdir(parents=True)
    store = PolicyStore(
        FakeDb(), None, Cfg(), legacy_workspace=ws, spill_read_root=spill)
    policy = store.resolve("missing-session")
    assert spill.resolve() in policy.read_roots


@pytest.mark.asyncio
async def test_tool_executor_spills_large_result(tmp_path: Path):
    from agent.tool_executor import ToolExecutor
    from tools.base import ToolRegistry, ToolSpec

    async def big_tool() -> str:
        return "Q" * 5000

    class Cfg:
        def get(self, key, default=None):
            if key == "tool_timeout_seconds":
                return 30
            return default

        def get_raw(self, key, default=None):
            if key == "tool_spill":
                return {"max_inline_bytes": 600}
            return default

    reg = ToolRegistry()
    reg.register_function(ToolSpec("big_echo", "t", {"type": "object", "properties": {}}),
                          big_tool)
    ex = ToolExecutor(reg, Cfg(), data_dir=tmp_path)
    out = await ex.execute_tool("big_echo", {}, session_id="sess-spill")
    assert out["ok"] is True
    text = out["result"]
    assert len(text.encode("utf-8")) <= 600
    assert "完整结果：" in text
    assert list((tmp_path / "temp" / "spills" / "sess-spill").glob("big_echo_*.txt"))
