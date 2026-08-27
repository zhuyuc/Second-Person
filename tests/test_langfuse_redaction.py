"""LangFuse span 出入口敏感信息脱敏契约。

保护：无论何时上报的 body 里都不能出现原文 API key / 手机 / 邮箱。
"""
from __future__ import annotations

from langfuse.integration.config import LangfuseConfig
from langfuse.integration.tracer import PipelineTracer


class _FakeClient:
    def __init__(self):
        self.events: list[dict] = []

    def enqueue(self, e):
        self.events.append(e)


def _tracer():
    t = PipelineTracer(LangfuseConfig(
        enabled=True, public_key="pk", secret_key="sk", host="x"))
    fake = _FakeClient()
    t._client = fake
    return t, fake


def _dump(fake):
    import json
    return json.dumps([e["body"] for e in fake.events], ensure_ascii=False)


def test_trace_input_is_redacted():
    tr, fake = _tracer()
    trace = tr.trace_start("chat.turn", session_id="s1",
                           input={"message": "帮我记 api_key: sk-abcd1234abcd1234"})
    trace.end(output="收到")
    dumped = _dump(fake)
    assert "sk-abcd1234abcd1234" not in dumped
    assert "[REDACTED:token_literal]" in dumped or "[REDACTED:api_key]" in dumped


def test_span_output_is_redacted():
    tr, fake = _tracer()
    tr.trace_start("t", input="hello")
    sp = tr.span_start("assemble", input={"user_content": "打给 13812345678"})
    sp.end(output={"answer": "已收到你的手机 13812345678"})
    dumped = _dump(fake)
    assert "13812345678" not in dumped
    assert "[REDACTED:cn_mobile]" in dumped


def test_generation_output_is_redacted():
    tr, fake = _tracer()
    tr.trace_start("t")
    gen = tr.generation_start("llm.foo", model="m",
                              input=[{"role": "user",
                                      "content": "邮箱 alice@example.com"}])
    gen.end(output="收到 alice@example.com")
    dumped = _dump(fake)
    assert "alice@example.com" not in dumped
    assert "[REDACTED:email]" in dumped


def test_plain_text_untouched():
    tr, fake = _tracer()
    tr.trace_start("t", input="用户偏好直接沟通")
    dumped = _dump(fake)
    assert "用户偏好直接沟通" in dumped
    assert "[REDACTED" not in dumped
