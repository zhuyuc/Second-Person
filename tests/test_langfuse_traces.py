"""Langfuse trace/span/generation 结构契约测试。

保护契约：后台任务只要显式 trace_start，就能让 span/generation 挂到同一 trace；
异常路径要能写入 level/statusMessage。测试使用 fake client，不访问真实 Langfuse。
"""
from __future__ import annotations

from langfuse.integration.config import LangfuseConfig
from langfuse.integration.tracer import PipelineTracer
from infrastructure.developer_trace import build_developer_trace, initial_developer_trace


class _FakeClient:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def enqueue(self, event: dict) -> None:
        self.events.append(event)


def _tracer(enabled: bool = True) -> tuple[PipelineTracer, _FakeClient]:
    tracer = PipelineTracer(LangfuseConfig(
        enabled=enabled,
        public_key="pk-test" if enabled else "",
        secret_key="sk-test" if enabled else "",
        host="http://localhost:3000",
    ))
    fake = _FakeClient()
    # 测试中替换网络客户端，确保 trace/span/generation 结构断言零外部依赖。
    tracer._client = fake  # noqa: SLF001
    return tracer, fake


def _events(fake: _FakeClient, event_type: str) -> list[dict]:
    return [e for e in fake.events if e["type"] == event_type]


def test_trace_span_generation_parent_chain_is_emitted():
    tracer, fake = _tracer()

    trace = tracer.trace_start("title_generation", session_id="sid-1",
                               input={"message": "hello"})
    span = tracer.span_start("title_generation", input={"session_id": "sid-1"})
    gen = tracer.generation_start("llm.title_gen", model="model-a",
                                  input=[{"role": "user", "content": "hello"}],
                                  metadata={"source": "title_gen"})
    gen.end(output="标题", usage={"input": 1, "output": 1, "total": 2,
                                "unit": "TOKENS"})
    span.end(output={"title": "标题"})
    trace.end(output={"title": "标题"})

    trace_events = _events(fake, "trace-create")
    span_creates = _events(fake, "span-create")
    span_updates = _events(fake, "span-update")
    gen_creates = _events(fake, "generation-create")
    gen_updates = _events(fake, "generation-update")

    assert len(trace_events) == 2
    assert len(span_creates) == 1
    assert len(span_updates) == 1
    assert len(gen_creates) == 1
    assert len(gen_updates) == 1

    trace_create = trace_events[0]["body"]
    trace_update = trace_events[1]["body"]
    span_create = span_creates[0]["body"]
    span_update = span_updates[0]["body"]
    gen_create = gen_creates[0]["body"]
    gen_update = gen_updates[0]["body"]

    assert trace_create["name"] == "title_generation"
    assert span_create["traceId"] == trace_create["id"]
    assert span_update["traceId"] == trace_create["id"]
    assert gen_create["traceId"] == trace_create["id"]
    assert gen_create["parentObservationId"] == span_create["id"]
    assert gen_update["traceId"] == trace_create["id"]
    # 当前 Langfuse 契约面向本地自托管实例，保留完整输入输出便于调试调用链。
    assert gen_create["input"] == [{"role": "user", "content": "hello"}]
    assert gen_update["output"] == "标题"
    assert gen_update["usage"]["unit"] == "TOKENS"
    assert trace_update["output"] == {"title": "标题"}


def test_error_level_and_status_message_are_emitted():
    tracer, fake = _tracer()

    trace = tracer.trace_start("handoff.summary", session_id="sid-2")
    span = tracer.span_start("handoff.summary_generation")
    span.end(level="ERROR", status_message="boom")
    trace.end(level="ERROR", status_message="boom")

    trace_events = _events(fake, "trace-create")
    span_updates = _events(fake, "span-update")
    assert len(trace_events) == 2
    assert len(span_updates) == 1

    trace_create = trace_events[0]["body"]
    trace_update = trace_events[1]["body"]
    span_update = span_updates[0]["body"]

    assert span_update["traceId"] == trace_create["id"]
    assert span_update["level"] == "ERROR"
    assert span_update["statusMessage"] == "boom"
    assert trace_update["level"] == "ERROR"
    assert trace_update["statusMessage"] == "boom"


def test_disabled_tracer_is_noop_and_never_enqueues():
    tracer, fake = _tracer(enabled=False)

    trace = tracer.trace_start("disabled.trace", session_id="sid-3")
    span = tracer.span_start("disabled.span")
    gen = tracer.generation_start("disabled.generation", model="model-a")
    gen.end(output="ignored")
    span.end(output={"ignored": True})
    trace.end(output={"ignored": True})

    assert fake.events == []


def test_developer_trace_is_structured_and_excludes_raw_reasoning():
    initial = initial_developer_trace(
        requested_mode="deep", client_request_id="cr-1")
    assert initial["status"] == "running"

    trace = build_developer_trace(
        requested_mode="deep", effective_mode="deep", route_reason="需要多步骤分析",
        history_messages=6, raw_rounds=3, compressed=False,
        memories=[{"id": "m1", "title": "偏好", "source_type": "memory",
                   "confidence": "high", "detail": "不应进入调试摘要的全文"}],
        retrieval_diagnostics={"gate": "passed", "vector_hits": 3},
        intents=[type("Intent", (), {"intent_type": "query_knowledge"})()],
        tool_results=[{"tool": "web_search", "ok": True, "result": "完整工具原文"}],
        problem_model=None, quality_report=None, delivery_job_id=None,
        latency_ms=123, llm_call_count=2, model_id="model-a")

    assert trace["schema_version"] == "developer-trace-v1"
    assert trace["route"]["effective_mode"] == "deep"
    assert trace["context"]["selected_memories"][0]["title"] == "偏好"
    assert "detail" not in trace["context"]["selected_memories"][0]
    assert "result" not in trace["execution"]["tools"][0]
