"""
PipelineTracer —— 面向对话流水线的高层追踪器。

层级模型（对齐 Langfuse 的 trace → observation 结构）：
- trace：一次对话轮次（chat.turn），带 session_id / 用户输入 / 最终输出
- span：流水线中的一个步骤（上下文加载、记忆检索、意图识别、工具执行、响应合成、后置处理）
- generation：一次 LLM 模型调用（模型名、输入消息、输出、token 用量、延迟）

用法（手动 start/end，避免大范围改动缩进）：
    tr = get_tracer()
    trace = tr.trace_start(name="chat.turn", session_id=sid, input=message)
    try:
        sp = tr.span_start("memory_retrieval", input=message)
        ... ; sp.end(output={"count": n})
        ... # 期间的 LLM 调用由 llm_provider 自动记录为 generation，挂在当前活跃 span/trace 下
        trace.update(output=answer)
    finally:
        trace.end()

未启用（无密钥/关闭）时所有方法返回空实现，零开销、绝不抛错。
活跃 trace/observation 通过 contextvars 传播（同一 asyncio Task 内的 await 链可见）。
"""
from __future__ import annotations

import contextvars
import logging
import uuid
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from .client import IngestionClient
from .config import LangfuseConfig

logger = logging.getLogger("second_person.langfuse")

# 当前活跃的 trace_id 与父 observation_id（跨 await 传播，仅当前 Task 内）
_active_trace: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "lf_active_trace", default=None)
_active_obs: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "lf_active_obs", default=None)

_MAX_STR = 32000
_MAX_GEN_INPUT = 64000

_CST = ZoneInfo("Asia/Shanghai")


def _now() -> str:
    # 中国标准时间（带 +08:00 偏移的 aware ISO，Langfuse 按绝对时刻解析，语义不变）
    return datetime.now(_CST).isoformat()


def _uid() -> str:
    return uuid.uuid4().hex


def _trim(v: Any, depth: int = 0, max_str: int = _MAX_STR) -> Any:
    """裁剪过大的输入/输出，避免上报体积失控。截断时附加原始长度信息。"""
    if v is None or isinstance(v, (int, float, bool)):
        return v
    if isinstance(v, str):
        if len(v) <= max_str:
            return v
        return v[:max_str] + f"…(truncated, original_length={len(v)})"
    if depth >= 4:
        s = str(v)
        if len(s) <= max_str:
            return s
        return s[:max_str] + f"…(truncated, original_length={len(s)})"
    if isinstance(v, list):
        return [_trim(x, depth + 1, max_str) for x in v[:50]]
    if isinstance(v, dict):
        return {str(k): _trim(val, depth + 1, max_str) for k, val in list(v.items())[:50]}
    return _trim(str(v), depth + 1, max_str)


def mark_preview(value: Any, *, content_type: str, limit: int | None = None) -> dict:
    """预览字段统一标记：说明"这块是什么内容" + 原始长度 + 截断标志 + 内容。

    默认全量记录（limit=None，preview 为完整内容，truncated 恒 False）；
    仅对确需体积兑底的超大动态内容显式传 limit 截断并标注。
    """
    text = "" if value is None else str(value)
    truncated = limit is not None and len(text) > limit
    return {
        "content_type": content_type,
        "chars": len(text),
        "truncated": truncated,
        "preview": text[:limit] if truncated else text,
    }


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并两个 dict，override 覆盖 base，嵌套 dict 做深度合并。"""
    result = {**(base or {})}
    for k, v in (override or {}).items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


# ---- 空实现（禁用态） ------------------------------------------------------
class _NoopSpan:
    def end(self, *a, **k): ...
    def update(self, *a, **k): ...


class _NoopGen:
    def end(self, *a, **k): ...


class _NoopTrace:
    def update(self, *a, **k): ...
    def end(self, *a, **k): ...


_NOOP_SPAN = _NoopSpan()
_NOOP_GEN = _NoopGen()
_NOOP_TRACE = _NoopTrace()


# ---- 真实句柄 --------------------------------------------------------------
class _Trace:
    def __init__(self, tracer: "PipelineTracer", tid: str, tok_t, tok_o):
        self._t = tracer
        self.id = tid
        self._tok_t = tok_t
        self._tok_o = tok_o
        self._metadata: dict | None = None

    def update(self, output=None, metadata=None) -> None:
        body: dict = {"id": self.id, "timestamp": _now()}
        if output is not None:
            body["output"] = _trim(output)
        if metadata is not None:
            # 深度合并：保留已有 metadata 字段，新值覆盖同名 key
            existing = getattr(self, "_metadata", None)
            merged = _deep_merge(existing, metadata) if existing else metadata
            self._metadata = merged
            body["metadata"] = _trim(merged)
        self._t._emit("trace-create", body)

    def end(self, output=None) -> None:
        if output is not None:
            self.update(output=output)
        try:
            _active_obs.reset(self._tok_o)
            _active_trace.reset(self._tok_t)
        except (ValueError, LookupError):
            pass


class _Span:
    def __init__(self, tracer: "PipelineTracer", tid: str, sid: str, tok):
        self._t = tracer
        self.trace_id = tid
        self.id = sid
        self._tok = tok
        self._output = None
        self._meta = None

    def update(self, output=None, metadata=None) -> None:
        if output is not None:
            self._output = output
        if metadata is not None:
            self._meta = metadata
        # 同步发送 span-update 事件，确保中间状态更新被记录
        body: dict = {"id": self.id,
                      "traceId": self.trace_id, "timestamp": _now()}
        if self._output is not None:
            body["output"] = _trim(self._output)
        if self._meta is not None:
            body["metadata"] = _trim(self._meta)
        self._t._emit("span-update", body)

    def end(self, output=None, level=None, status_message=None) -> None:
        if output is not None:
            self._output = output
        body: dict = {"id": self.id,
                      "traceId": self.trace_id, "endTime": _now()}
        if self._output is not None:
            body["output"] = _trim(self._output)
        if self._meta is not None:
            body["metadata"] = _trim(self._meta)
        if level:
            body["level"] = level
        if status_message:
            body["statusMessage"] = status_message[:_MAX_STR]
        self._t._emit("span-update", body)
        try:
            _active_obs.reset(self._tok)
        except (ValueError, LookupError):
            pass


class _Generation:
    def __init__(self, tracer: "PipelineTracer", tid: str, gid: str):
        self._t = tracer
        self.trace_id = tid
        self.id = gid

    def end(self, output=None, usage=None, level=None, status_message=None) -> None:
        body: dict = {"id": self.id,
                      "traceId": self.trace_id, "endTime": _now()}
        if output is not None:
            body["output"] = _trim(output)
        if usage:
            body["usage"] = usage
        if level:
            body["level"] = level
        if status_message:
            body["statusMessage"] = status_message[:_MAX_STR]
        self._t._emit("generation-update", body)


class PipelineTracer:
    def __init__(self, config: LangfuseConfig):
        self.config = config
        self.enabled = bool(config.enabled)
        self._client: Optional[IngestionClient] = None
        if self.enabled:
            self._client = IngestionClient(
                config.host, config.public_key, config.secret_key,
                flush_interval=config.flush_interval, batch_size=config.flush_batch)

    async def start(self) -> None:
        if self._client:
            await self._client.start()

    async def stop(self) -> None:
        if self._client:
            await self._client.stop()

    async def flush(self) -> None:
        if self._client:
            await self._client.flush()

    def _emit(self, etype: str, body: dict) -> None:
        if self._client:
            self._client.enqueue(
                {"id": _uid(), "type": etype, "timestamp": _now(), "body": body})

    # ---- trace ----
    def trace_start(self, name: str, *, session_id: str | None = None,
                    user_id: str | None = None, input: Any = None,
                    metadata: dict | None = None, tags: list | None = None):
        if not self.enabled:
            return _NOOP_TRACE
        tid = _uid()
        body: dict = {"id": tid, "timestamp": _now(), "name": name}
        if session_id is not None:
            body["sessionId"] = session_id
        if user_id is not None:
            body["userId"] = user_id
        if input is not None:
            body["input"] = _trim(input)
        if metadata is not None:
            body["metadata"] = _trim(metadata)
        if tags:
            body["tags"] = tags
        if self.config.release:
            body["release"] = self.config.release
        self._emit("trace-create", body)
        tok_t = _active_trace.set(tid)
        tok_o = _active_obs.set(None)
        return _Trace(self, tid, tok_t, tok_o)

    # ---- span（步骤，进入时设为当前活跃 observation，其内的调用挂在它下面） ----
    def span_start(self, name: str, *, input: Any = None,
                   metadata: dict | None = None):
        tid = _active_trace.get()
        if not self.enabled or not tid:
            return _NOOP_SPAN
        sid = _uid()
        body: dict = {"id": sid, "traceId": tid, "name": name, "startTime": _now(),
                      "parentObservationId": _active_obs.get()}
        if input is not None:
            body["input"] = _trim(input)
        if metadata is not None:
            body["metadata"] = _trim(metadata)
        self._emit("span-create", body)
        tok = _active_obs.set(sid)
        return _Span(self, tid, sid, tok)

    # ---- generation（LLM 调用，不改变活跃上下文，挂在当前 span/trace 下） ----
    def generation_start(self, name: str, *, model: str = "", input: Any = None,
                         model_parameters: dict | None = None,
                         metadata: dict | None = None):
        tid = _active_trace.get()
        if not self.enabled or not tid:
            return _NOOP_GEN
        gid = _uid()
        body: dict = {"id": gid, "traceId": tid, "name": name, "startTime": _now(),
                      "parentObservationId": _active_obs.get(), "model": model}
        if model_parameters is not None:
            body["modelParameters"] = _trim(model_parameters)
        if input is not None:
            body["input"] = _trim(input, max_str=_MAX_GEN_INPUT)
        if metadata is not None:
            body["metadata"] = _trim(metadata)
        self._emit("generation-create", body)
        return _Generation(self, tid, gid)


# ---- 全局单例 --------------------------------------------------------------
_tracer: Optional[PipelineTracer] = None


def init_tracer(config: LangfuseConfig) -> PipelineTracer:
    global _tracer
    _tracer = PipelineTracer(config)
    if _tracer.enabled:
        logger.info("Langfuse 追踪已启用 → %s", config.host)
    return _tracer


def get_tracer() -> PipelineTracer:
    global _tracer
    if _tracer is None:
        _tracer = PipelineTracer(LangfuseConfig(enabled=False))
    return _tracer
