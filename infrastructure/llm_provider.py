"""
LLM Provider 抽象层（产品文档 §LLM Provider 抽象层 / 开发文档 §6.11）。

- 统一 chat / stream / embed / function_call 四接口
- 支持 openai_compatible / anthropic / google / custom
- 熔断器按模型独立监测：连续 3 次调用失败进入 unavailable，60s 半开探测恢复
- 单次调用失败（429/超时/5xx）指数退避重试 1s→2s→4s 最多 3 次；
  3 次全败才计一次熔断失败
- 所有调用统一拦截记录 token_usage（真实 usage 优先，缺失用 tiktoken 估算触发判定）
- 请求级 Provider 快照：调用方传入快照 dict，不在调用中读库
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

from .http_client import timeout_for
from .observability import get_trace_id
from infrastructure.timeutil import now_cst

logger = logging.getLogger("second_person.llm")

RETRY_DELAYS = [1.0, 2.0, 4.0]
CIRCUIT_THRESHOLD = 3
HALF_OPEN_INTERVAL = 60.0


def _inject_images(messages: list[dict], images: list[str] | None) -> list[dict]:
    """将图片 dataURL 注入最后一条 user 消息，组装为 OpenAI 多模态 content 数组。

    无图片时原样返回（不改变纯文本行为）。"""
    if not images:
        return messages
    out = [dict(m) for m in messages]
    for m in reversed(out):
        if m.get("role") == "user":
            text = m.get("content", "")
            parts: list[dict] = [{"type": "text",
                                  "text": text if isinstance(text, str) else ""}]
            for url in images:
                if url:
                    parts.append(
                        {"type": "image_url", "image_url": {"url": url}})
            m["content"] = parts
            break
    return out


# ---------------------------------------------------------------------------
# token 估算（触发压缩判定用，不写 token_usage）
# ---------------------------------------------------------------------------
def estimate_tokens(messages: list[dict] | str) -> int:
    """估算 token 数，兜底场景上限 8192 防止虚高。"""
    text = messages if isinstance(messages, str) else "".join(
        m.get("content", "") or "" for m in messages)
    _CAP = 8192
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return min(len(enc.encode(text)), _CAP)
    except Exception:  # noqa: BLE001
        return min(int(len(text) / 2.5), _CAP)  # 中文经验值


def normalize_usage(usage: dict | None, *, input_key: str = "prompt_tokens",
                    output_key: str = "completion_tokens") -> dict[str, int]:
    """Normalize provider usage and cache counters at the adapter boundary."""
    usage = usage or {}
    details = (usage.get("prompt_tokens_details")
               or usage.get("input_tokens_details") or {})
    cache_read = (details.get("cached_tokens")
                  or details.get("cache_read_tokens")
                  or usage.get("prompt_cache_hit_tokens")
                  or usage.get("cache_read_input_tokens") or 0)
    cache_write = (details.get("cache_write_tokens")
                   or details.get("cache_creation_input_tokens")
                   or usage.get("cache_write_input_tokens")
                   or usage.get("cache_creation_input_tokens") or 0)
    raw_input = max(0, int(usage.get(input_key, usage.get("input_tokens", 0)) or 0))
    # Anthropic reports cache reads/writes beside uncached input_tokens,
    # whereas OpenAI-compatible APIs report prompt_tokens as the billed total.
    billed_input = (raw_input + max(0, int(cache_read or 0))
                    + max(0, int(cache_write or 0))
                    if input_key == "input_tokens" else raw_input)
    return {
        "input_tokens": billed_input,
        "output_tokens": max(0, int(usage.get(output_key, usage.get("output_tokens", 0)) or 0)),
        "cache_read_tokens": max(0, int(cache_read or 0)),
        "cache_write_tokens": max(0, int(cache_write or 0)),
    }


# ---------------------------------------------------------------------------
# extra_body 厂商适配：通用开关 → 厂商原生参数
# ---------------------------------------------------------------------------
def _normalize_extra_body(snap, extra_body: dict | None) -> dict:
    """把通用 extra_body 字段翻译成厂商原生参数。

    thinking_enabled 是项目统一的思考模式开关，但各厂商参数名不同，
    原样透传不会被厂商识别（DeepSeek 直接忽略，导致思考模式关不掉：
    轻量结构化调用白白消耗思考令牌，且偶发只思考不给最终答复的空返回）：
    - DeepSeek：{"thinking": {"type": "enabled"/"disabled"}}
    - Anthropic/Google：无统一映射，思考默认关闭，直接剔除
    - 其他 OpenAI 兼容厂商：保持透传语义不变
    """
    eb = dict(extra_body or {})
    # The agent runtime has one four-level contract.  Map it at the provider
    # boundary; never expose vendor-specific request fields to callers.
    effort = eb.pop("reasoning_effort", None)
    if effort is not None:
        if effort not in {"off", "low", "high", "max"}:
            raise ValueError("reasoning_effort must be off, low, high, or max")
        if "deepseek" in (snap.base_url or "").lower():
            eb["thinking"] = {"type": "disabled" if effort == "off" else "enabled"}
            if effort != "off":
                eb["reasoning_effort"] = effort
        elif snap.provider_type not in ("anthropic", "google"):
            eb["reasoning_effort"] = effort
    if "thinking_enabled" not in eb:
        return eb
    enabled = bool(eb.pop("thinking_enabled"))
    if "deepseek" in (snap.base_url or "").lower():
        eb["thinking"] = {"type": "enabled" if enabled else "disabled"}
    elif snap.provider_type in ("anthropic", "google"):
        pass   # 思考默认关闭，剔除即可
    else:
        eb["thinking_enabled"] = enabled
    return eb


def _observability_parameters(kw: dict, extra_body: dict | None = None) -> dict | None:
    """生成 Langfuse 可记录的调用参数，不把任意透传体当作业务字段。"""
    params = {k: kw[k] for k in ("temperature", "max_tokens") if k in kw}
    if extra_body and "thinking_enabled" in extra_body:
        params["thinking_enabled"] = bool(extra_body["thinking_enabled"])
    if extra_body and "reasoning_effort" in extra_body:
        params["reasoning_effort"] = extra_body["reasoning_effort"]
    return params or None


@dataclass
class CircuitBreaker:
    model: str
    failures: int = 0
    state: str = "healthy"           # healthy / unavailable
    opened_at: float = 0.0

    def allow(self) -> bool:
        if self.state == "healthy":
            return True
        # unavailable：每 60s 允许一次半开探测
        if time.monotonic() - self.opened_at >= HALF_OPEN_INTERVAL:
            return True
        return False

    def record_success(self) -> None:
        self.failures = 0
        self.state = "healthy"

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= CIRCUIT_THRESHOLD:
            self.state = "unavailable"
            self.opened_at = time.monotonic()


@dataclass
class ProviderSnapshot:
    """请求级 Provider 快照（base_url/api_key/model_id 冻结）。"""
    provider_id: str
    provider_type: str
    base_url: str
    api_key: str
    model_id: str
    input_price: float | None = None   # None = 未配置单价（费用不计入）
    output_price: float | None = None
    context_window: int = 128000
    # Provider-neutral capability facts. Empty reasoning_efforts means the
    # adapter has no reliable catalog entry and preserves legacy pass-through.
    capabilities: frozenset[str] = frozenset({"chat", "stream"})
    reasoning_efforts: tuple[str, ...] = ()
    native_reasoning: bool = False


class TokenRecorder:
    def __init__(self, db):
        self.db = db

    def record(self, model_name: str, source: str, input_tokens: int,
               output_tokens: int, session_id: str | None = None,
               input_price: float | None = None,
               output_price: float | None = None,
               cache_read_tokens: int = 0,
               cache_write_tokens: int = 0) -> None:
        # 单价快照随用量落库：费用按用量发生时的单价冻结，后续调价不追溯；
        # 未配单价（双 None）时快照与金额留空，费用查询按当时单价兜底
        cost = None
        if input_price is not None or output_price is not None:
            cost = input_tokens / 1_000_000 * (input_price or 0) + \
                output_tokens / 1_000_000 * (output_price or 0)
        try:
            # 火忘式写入：聊天热路径上的高频小写，入队即返回零等待，
            # 由单写线程串行落库，失败由写线程记日志
            self.db.execute_nowait(
                "INSERT INTO token_usage(model_name,source,session_id,input_tokens,"
                "output_tokens,cache_read_tokens,cache_write_tokens,trace_id,create_time,"
                "input_price,output_price,cost) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (model_name, source, session_id, input_tokens, output_tokens,
                 cache_read_tokens, cache_write_tokens, get_trace_id(),
                 now_cst().isoformat(timespec="seconds"),
                 input_price, output_price, cost))
        except Exception:  # noqa: BLE001
            logger.exception("token_usage 记录失败")


class LLMError(RuntimeError):
    pass


class CircuitOpenError(LLMError):
    pass


class EmptyCompletionError(LLMError):
    """HTTP 200 但 content 为空（输出疑似全被思考内容占用）。

    属上游偶发异常，可重试：不携带 response 属性，_call_with_retry
    不会误判为 4xx 快速失败，会走完整退避重试链。"""
    pass


class LLMClient:
    """LLM 调用客户端：熔断 + 重试 + token 记录，按 ProviderSnapshot 调用。"""

    def __init__(self, token_recorder: TokenRecorder | None = None):
        self.recorder = token_recorder
        self._breakers: dict[str, CircuitBreaker] = {}

    def breaker(self, model: str) -> CircuitBreaker:
        return self._breakers.setdefault(model, CircuitBreaker(model))

    def status(self, model: str) -> str:
        return self.breaker(model).state

    # ---- 公共调用入口 -----------------------------------------------------
    async def chat(self, snap: ProviderSnapshot, messages: list[dict],
                   source: str = "main_chat", session_id: str | None = None,
                   tools: list[dict] | None = None, images: list[str] | None = None,
                   extra_body: dict | None = None, json_mode: bool = False,
                   **kw) -> dict[str, Any]:
        """返回 {content, tool_calls, usage}。

        images：可选图片 dataURL 列表（多模态）。
        json_mode：启用 Provider 原生 JSON 输出保证（OpenAI 兼容 →
          response_format={"type":"json_object"}）。Anthropic/Google 不支持时静默降级。
        extra_body：透传至 API 请求体的额外字段。thinking_enabled 为项目统一的
          思考模式开关，由 _normalize_extra_body 翻译成厂商原生参数（如
          DeepSeek 的 {"thinking": {"type": "disabled"}}）；收敛分析等轻量结构化
          任务建议传 extra_body={"thinking_enabled": False} 关闭思考，避免思考
          令牌拖慢响应、偶发只思考不给答复的空返回。
        """
        from langfuse.integration import get_tracer
        gen = get_tracer().generation_start(
            name=f"llm.{source}", model=snap.model_id, input=messages,
            metadata={"source": source, "session_id": session_id,
                      "images": len(images) if images else 0,
                      "thinking_enabled": (extra_body or {}).get(
                          "thinking_enabled")},
            model_parameters=_observability_parameters(kw, extra_body))
        try:
            if extra_body:
                kw["extra_body"] = extra_body
            if json_mode:
                kw["json_mode"] = True
            res = await self._call_with_retry(
                snap, source, session_id,
                lambda: self._do_chat(snap, messages, tools, images=images, **kw))
            u = res.get("usage", {}) or {}
            # 输出记录：纯文本调用保持字符串；function_call（工具参数推断）
            # 模型返回在 tool_calls 里（content 为空），结构化记录供 Langfuse 溯源
            out: Any = res.get("content") or ""
            tc = res.get("tool_calls") or []
            if tc:
                out = {"content": out, "tool_calls": tc}
            _cache_read = u.get("cache_read_tokens", 0)
            _cache_write = u.get("cache_write_tokens", 0)
            _billed_input = u.get("input_tokens", 0) + max(0, _cache_read) + max(0, _cache_write)
            gen.end(output=out, usage={
                "input": u.get("input_tokens", 0), "output": u.get("output_tokens", 0),
                "total": u.get("input_tokens", 0) + u.get("output_tokens", 0),
                "input_cache_read": _cache_read, "input_cache_creation": _cache_write,
                "unit": "TOKENS"}, metadata={
                    "cache_read_tokens": _cache_read,
                    "cache_write_tokens": _cache_write,
                    "cache_hit_rate": (_cache_read / _billed_input) if _billed_input else 0.0,
                })
            return res
        except Exception as e:  # noqa: BLE001
            gen.end(level="ERROR", status_message=str(e))
            raise

    async def function_call(self, snap: ProviderSnapshot, messages: list[dict],
                            tools: list[dict], source: str = "tool_infer",
                            session_id: str | None = None, **kw) -> dict[str, Any]:
        return await self.chat(snap, messages, source, session_id, tools=tools, **kw)

    async def embed(self, snap: ProviderSnapshot, texts: list[str],
                    source: str = "embedding") -> list[list[float]]:
        return await self._call_with_retry(
            snap, source, None, lambda: self._do_embed(snap, texts))

    async def stream(self, snap: ProviderSnapshot, messages: list[dict],
                     source: str = "main_chat", session_id: str | None = None,
                     images: list[str] | None = None,
                     on_reasoning=None, on_annotations=None,
                     extra_tools: list[dict] | None = None,
                     **kw) -> AsyncIterator[str]:
        """流式：yield content_delta 文本片段。images：可选图片 dataURL 列表。
        extra_tools：透传厂商内置工具（如 mimo web_search）；搜索引用通过
        on_annotations 回调返回（流式首包携带）。
        熔断口径与 chat 对齐：首包产出前允许 3 次退避重试，全败才计 1 次熔断失败；
        4xx（除 429）属配置错误快速失败不计熔断；首包后的中途失败不重试
        （已向下游吐出部分内容，重试会重复输出）且不计熔断。
        """
        from langfuse.integration import get_tracer
        gen = get_tracer().generation_start(
            name=f"llm.{source}", model=snap.model_id, input=messages,
            metadata={"source": source, "session_id": session_id,
                      "images": len(images) if images else 0,
                      "thinking_enabled": (kw.get("extra_body") or {}).get(
                          "thinking_enabled"),
                      "builtin_tools": len(extra_tools) if extra_tools else 0},
            model_parameters=_observability_parameters(
                kw, kw.get("extra_body")))
        breaker = self.breaker(snap.model_id)
        if not breaker.allow():
            gen.end(level="ERROR", status_message="熔断中")
            raise CircuitOpenError(f"模型 {snap.model_id} 熔断中")
        started = False   # 是否已向下游产出过任意内容（含 reasoning/annotations）
        try:
            full: list[str] = []
            usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}
            for i, delay in enumerate([0.0] + RETRY_DELAYS):
                if delay:
                    await asyncio.sleep(delay)
                try:
                    async for kind, chunk in self._do_stream(snap, messages, usage,
                                                             images=images,
                                                             extra_tools=extra_tools, **kw):
                        started = True
                        if kind == "reasoning":
                            if on_reasoning:
                                await on_reasoning(chunk)
                            continue
                        if kind == "annotations":
                            if on_annotations:
                                await on_annotations(chunk)
                            continue
                        full.append(chunk)
                        yield chunk
                    break
                except Exception as e:  # noqa: BLE001
                    if started:
                        raise   # 首包后中途失败：不重试（避免重复输出）
                    status = getattr(getattr(e, "response", None),
                                     "status_code", None)
                    if status is not None and 400 <= status < 500 and status != 429:
                        raise   # 配置/请求错误：快速失败，不重试不计熔断（外层豁免）
                    logger.warning("流式 LLM 连接失败(第 %d 次)：%s", i + 1, e)
                    if i == len(RETRY_DELAYS):
                        breaker.record_failure()   # 3 次全败才计 1 次熔断
                        raise
            breaker.record_success()
            if self.recorder:
                inp, outp = usage["input_tokens"], usage["output_tokens"]
                cache_read = usage.get("cache_read_tokens", 0)
                cache_write = usage.get("cache_write_tokens", 0)
                if inp == 0 and outp == 0 and full:
                    # Provider 未返回流式 usage：仅估算用户最后一条消息 + 全部输出
                    last_user_msg = ""
                    for m in reversed(messages):
                        if m.get("role") == "user":
                            last_user_msg = m.get("content", "") or ""
                            break
                    inp = estimate_tokens(
                        [{"role": "user", "content": last_user_msg}]) if last_user_msg else 0
                    outp = estimate_tokens("".join(full))
                    usage["input_tokens"], usage["output_tokens"] = inp, outp
                self.recorder.record(snap.model_id, source,
                                     inp, outp, session_id,
                                     input_price=snap.input_price,
                                     output_price=snap.output_price,
                                     cache_read_tokens=cache_read,
                                     cache_write_tokens=cache_write)
            _cache_read = usage.get("cache_read_tokens", 0)
            _cache_write = usage.get("cache_write_tokens", 0)
            _billed_input = usage["input_tokens"] + max(0, _cache_read) + max(0, _cache_write)
            gen.end(output="".join(full), usage={
                "input": usage["input_tokens"], "output": usage["output_tokens"],
                "total": usage["input_tokens"] + usage["output_tokens"],
                "input_cache_read": _cache_read, "input_cache_creation": _cache_write,
                "unit": "TOKENS"}, metadata={
                    "cache_read_tokens": _cache_read,
                    "cache_write_tokens": _cache_write,
                    "cache_hit_rate": (_cache_read / _billed_input) if _billed_input else 0.0,
                })
        except Exception as e:  # noqa: BLE001
            # 熔断计数已在重试循环内按口径处理，此处只负责上报与透传
            gen.end(level="ERROR", status_message=str(e))
            raise

    async def stream_chat(self, snap: ProviderSnapshot, messages: list[dict],
                          source: str = "agent_step",
                          session_id: str | None = None,
                          tools: list[dict] | None = None,
                          images: list[str] | None = None,
                          extra_body: dict | None = None,
                          **kw) -> AsyncIterator[tuple]:
        """流式带工具调用：yield 结构化事件供 agent 步循环消费。

        - ("content", str)：正文增量（可多次）
        - ("reasoning", str)：思考增量（推理模型；调用方决定是否外露）
        - ("annotations", list)：内置搜索引用（首包）
        - ("done", {"content": str, "tool_calls": list, "usage": {...}})：
          终态事件，一定出现一次

        契约与 chat() 对齐：
        - 空返回护栏：内容与 tool_calls 都为空视作可重试异常（模型只思考没给答复）
        - 熔断口径：首包（含内容与 tool_call）前允许 3 次退避重试，全败才计 1 次
          熔断失败；4xx（除 429）快速失败不计熔断；首包后中途失败不重试。
        - 已把 tool_call delta 从"面向调用方产出"排除：如果只有 tool_call 尚在累积
          且流式失败，仍可重试（不会造成重复输出）；一旦有 content 增量被 yield
          出去，即视为 started 不再重试。
        """
        from langfuse.integration import get_tracer
        gen = get_tracer().generation_start(
            name=f"llm.{source}", model=snap.model_id, input=messages,
            metadata={"source": source, "session_id": session_id,
                      "images": len(images) if images else 0,
                      "thinking_enabled": (extra_body or {}).get(
                          "thinking_enabled"),
                      "tools": len(tools) if tools else 0},
            model_parameters=_observability_parameters(kw, extra_body))
        breaker = self.breaker(snap.model_id)
        if not breaker.allow():
            gen.end(level="ERROR", status_message="熔断中")
            raise CircuitOpenError(f"模型 {snap.model_id} 熔断中")
        if extra_body:
            kw["extra_body"] = extra_body
        started = False   # 是否已向调用方 yield 过 content（仅 content 计入）
        try:
            content_parts: list[str] = []
            tool_calls_acc: dict = {}
            usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}
            reasoning_chars = 0
            for i, delay in enumerate([0.0] + RETRY_DELAYS):
                if delay:
                    await asyncio.sleep(delay)
                # 重试新一轮前清空累积（started=True 时不会进入这里）
                content_parts.clear()
                tool_calls_acc.clear()
                try:
                    async for kind, chunk in self._do_stream(
                            snap, messages, usage, images=images,
                            tools=tools, **kw):
                        if kind == "content":
                            started = True
                            content_parts.append(chunk)
                            yield "content", chunk
                        elif kind == "reasoning":
                            reasoning_chars += len(chunk or "")
                            yield "reasoning", chunk
                        elif kind == "annotations":
                            yield "annotations", chunk
                        elif kind == "tool_call":
                            if not tool_calls_acc:
                                # 首个 tool_call 增量 → 立即告知调用方"本步是工具步"，
                                # 不必等整步结束，便于尽早撤回旁白/定性。
                                yield "tool_start", True
                            _merge_tool_call_delta(tool_calls_acc, chunk)
                    if not content_parts and not tool_calls_acc:
                        # 空返回护栏：走退避重试；不带 response 属性，
                        # 不会被外层 4xx 快速失败误判
                        raise EmptyCompletionError(
                            f"模型 {snap.model_id} 返回空内容"
                            f"（疑似输出全被思考内容占用）")
                    break
                except Exception as e:  # noqa: BLE001
                    if started:
                        raise
                    status = getattr(getattr(e, "response", None),
                                     "status_code", None)
                    if status is not None and 400 <= status < 500 and status != 429:
                        raise
                    logger.warning("流式 LLM(带工具)连接失败(第 %d 次)：%s", i + 1, e)
                    if i == len(RETRY_DELAYS):
                        breaker.record_failure()
                        raise
            breaker.record_success()
            final_content = "".join(content_parts)
            final_tool_calls = _finalize_tool_calls(tool_calls_acc)
            if self.recorder:
                inp, outp = usage["input_tokens"], usage["output_tokens"]
                cache_read = usage.get("cache_read_tokens", 0)
                cache_write = usage.get("cache_write_tokens", 0)
                if inp == 0 and outp == 0 and (final_content or final_tool_calls):
                    last_user_msg = ""
                    for m in reversed(messages):
                        if m.get("role") == "user":
                            last_user_msg = m.get("content", "") or ""
                            break
                    inp = estimate_tokens(
                        [{"role": "user", "content": last_user_msg}]) if last_user_msg else 0
                    outp = estimate_tokens(final_content)
                    usage["input_tokens"], usage["output_tokens"] = inp, outp
                self.recorder.record(snap.model_id, source,
                                     inp, outp, session_id,
                                     input_price=snap.input_price,
                                     output_price=snap.output_price,
                                     cache_read_tokens=cache_read,
                                     cache_write_tokens=cache_write)
            gen_output: Any = final_content
            if final_tool_calls:
                gen_output = {"content": final_content,
                              "tool_calls": final_tool_calls}
            _cache_read = usage.get("cache_read_tokens", 0)
            _cache_write = usage.get("cache_write_tokens", 0)
            _billed_input = usage["input_tokens"] + max(0, _cache_read) + max(0, _cache_write)
            gen.end(output=gen_output, usage={
                "input": usage["input_tokens"], "output": usage["output_tokens"],
                "total": usage["input_tokens"] + usage["output_tokens"],
                "input_cache_read": _cache_read, "input_cache_creation": _cache_write,
                "unit": "TOKENS"}, metadata={
                    "reasoning_received": reasoning_chars > 0,
                    "reasoning_chars": reasoning_chars,
                    "tool_calls": len(final_tool_calls),
                    "cache_read_tokens": _cache_read,
                    "cache_write_tokens": _cache_write,
                    "cache_hit_rate": (_cache_read / _billed_input) if _billed_input else 0.0,
                })
            yield "done", {"content": final_content,
                           "tool_calls": final_tool_calls,
                           "usage": {"input_tokens": usage["input_tokens"],
                                     "output_tokens": usage["output_tokens"],
                                     "cache_read_tokens": usage.get("cache_read_tokens", 0),
                                     "cache_write_tokens": usage.get("cache_write_tokens", 0)}}
        except Exception as e:  # noqa: BLE001
            gen.end(level="ERROR", status_message=str(e))
            raise

    # ---- 重试封装 ---------------------------------------------------------
    async def _call_with_retry(self, snap, source, session_id, fn):
        breaker = self.breaker(snap.model_id)
        if not breaker.allow():
            raise CircuitOpenError(f"模型 {snap.model_id} 熔断中，请切换其他模型")
        last: Exception | None = None
        for i, delay in enumerate([0.0] + RETRY_DELAYS):
            if delay:
                await asyncio.sleep(delay)
            try:
                result = await fn()
                breaker.record_success()
                if self.recorder and isinstance(result, dict) and "usage" in result:
                    u = result["usage"]
                    self.recorder.record(snap.model_id, source,
                                         u.get("input_tokens", 0),
                                         u.get("output_tokens", 0), session_id,
                                         input_price=snap.input_price,
                                         output_price=snap.output_price,
                                         cache_read_tokens=u.get("cache_read_tokens", 0),
                                         cache_write_tokens=u.get("cache_write_tokens", 0))
                return result
            except Exception as e:  # noqa: BLE001
                last = e
                logger.warning("LLM 调用失败(第 %d 次)：%s", i + 1, e)
                # 4xx 客户端错误（除 429）属配置/请求问题，重试无意义，
                # 快速失败且不计入熔断计数（配置错不该触发熔断遮蔽）
                status = getattr(getattr(e, "response", None),
                                 "status_code", None)
                if status is not None and 400 <= status < 500 and status != 429:
                    # 透出厂商错误体（如“模型名不存在，支持的是 xxx”），
                    # 避免只报 HTTP 状态码让用户无从排查
                    detail = ""
                    try:
                        detail = (e.response.text or "").strip()[:300]
                    except Exception:  # noqa: BLE001
                        pass
                    raise LLMError(
                        f"LLM 调用失败（HTTP {status}，不重试）：{detail or e}")
        breaker.record_failure()
        raise LLMError(f"LLM 调用失败（已重试）：{last}")

    # ---- 具体 Provider 实现 ----------------------------------------------
    async def _do_chat(self, snap, messages, tools, images=None, **kw) -> dict[str, Any]:
        if snap.provider_type == "anthropic":
            kw.pop("json_mode", None)
            return await self._anthropic_chat(snap, messages, tools, **kw)
        if snap.provider_type == "google":
            kw.pop("json_mode", None)
            return await self._google_chat(snap, messages, **kw)
        return await self._openai_chat(snap, _inject_images(messages, images), tools, **kw)

    async def _openai_chat(self, snap, messages, tools, **kw) -> dict[str, Any]:
        body: dict[str, Any] = {"model": snap.model_id, "messages": messages}
        if tools:
            body["tools"] = tools
        body.update({k: v for k, v in kw.items()
                    if k in ("temperature", "max_tokens")})
        if kw.get("json_mode"):
            body["response_format"] = {"type": "json_object"}
        # extra_body：通用开关经 _normalize_extra_body 翻译成厂商原生参数
        # （如 DeepSeek 的 thinking.type；thinking_enabled 原样透传会被忽略）
        body.update(_normalize_extra_body(snap, kw.get("extra_body")))
        async with httpx.AsyncClient(timeout=timeout_for("default")) as c:
            r = await c.post(f"{snap.base_url.rstrip('/')}/chat/completions",
                             json=body,
                             headers={"Authorization": f"Bearer {snap.api_key}"})
            r.raise_for_status()
            data = r.json()
        choices = data.get("choices") or [{}]
        choice = choices[0].get("message", {}) if choices else {}
        usage = data.get("usage", {})
        content = choice.get("content") or ""
        tool_calls = choice.get("tool_calls") or []
        # 空返回护栏：HTTP 200 但 content 为空且无工具调用——上游偶发将输出
        # 全部消耗在 reasoning_content（只思考不给最终答复），抛可重试异常
        # 走退避重试，避免空内容直接透传导致下游 JSON 解析失败
        if not content and not tool_calls:
            raise EmptyCompletionError(
                f"模型 {snap.model_id} 返回空内容"
                f"（completion_tokens={usage.get('completion_tokens', 0)}，"
                f"疑似输出全被思考内容占用）")
        usage_norm = normalize_usage(usage)
        return {
            "content": content,
            "tool_calls": tool_calls,
            "annotations": choice.get("annotations") or [],
            "usage": usage_norm,
        }

    async def _anthropic_chat(self, snap, messages, tools, **kw) -> dict[str, Any]:
        sys = "".join(m["content"] for m in messages if m["role"] == "system")
        conv = [m for m in messages if m["role"] != "system"]
        body: dict[str, Any] = {"model": snap.model_id, "messages": conv,
                                "max_tokens": kw.get("max_tokens", 4096)}
        if sys:
            body["system"] = sys
        if tools:
            body["tools"] = tools
        body.update(_normalize_extra_body(snap, kw.get("extra_body")))
        async with httpx.AsyncClient(timeout=timeout_for("default")) as c:
            r = await c.post(f"{snap.base_url.rstrip('/')}/messages", json=body,
                             headers={"x-api-key": snap.api_key,
                                      "anthropic-version": "2023-06-01"})
            r.raise_for_status()
            data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text")
        usage = data.get("usage", {})
        return {"content": text, "tool_calls": [],
                "usage": normalize_usage(usage, input_key="input_tokens",
                                          output_key="output_tokens")}

    async def _google_chat(self, snap, messages, **kw) -> dict[str, Any]:
        contents = [{"role": "user" if m["role"] != "assistant" else "model",
                     "parts": [{"text": m.get("content", "")}]}
                    for m in messages if m["role"] != "system"]
        # Google 无统一思考开关映射，extra_body 不并入请求体（思考默认关闭，
        # 与归一化剔除 thinking_enabled 的语义一致）
        url = (f"{snap.base_url.rstrip('/')}/models/{snap.model_id}:generateContent"
               f"?key={snap.api_key}")
        async with httpx.AsyncClient(timeout=timeout_for("default")) as c:
            r = await c.post(url, json={"contents": contents})
            r.raise_for_status()
            data = r.json()
        text = "".join(
            p.get("text", "") for p in
            data.get("candidates", [{}])[0].get("content", {}).get("parts", []))
        um = data.get("usageMetadata", {})
        return {"content": text, "tool_calls": [],
                "usage": {"input_tokens": um.get("promptTokenCount", 0),
                          "output_tokens": um.get("candidatesTokenCount", 0),
                          "cache_read_tokens": 0, "cache_write_tokens": 0}}

    async def _do_embed(self, snap, texts) -> list[list[float]]:
        # 本地 Embedding 微服务毫秒级返回，用短读超时快速失败（不再傻等 120s）
        async with httpx.AsyncClient(timeout=timeout_for("embedding")) as c:
            r = await c.post(f"{snap.base_url.rstrip('/')}/embeddings",
                             json={"model": snap.model_id, "input": texts},
                             headers={"Authorization": f"Bearer {snap.api_key}"})
            r.raise_for_status()
            data = r.json()
        return [item["embedding"] for item in data["data"]]

    async def _do_stream(self, snap, messages, usage, images=None,
                         extra_tools=None, tools=None,
                         **kw) -> AsyncIterator[tuple]:
        """先带 stream_options.include_usage 请求（OpenAI 官方系必须显式开启才返回
        流式 usage）；个别网关不认识该参数报 400 时去掉降级重试一次（400 发生在
        首 chunk 之前，重试不会产生重复内容）。"""
        try:
            async for item in self._stream_request(snap, messages, usage,
                                                   images=images, include_usage=True,
                                                   extra_tools=extra_tools,
                                                   tools=tools, **kw):
                yield item
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 400:
                raise
            async for item in self._stream_request(snap, messages, usage,
                                                   images=images, include_usage=False,
                                                   extra_tools=extra_tools,
                                                   tools=tools, **kw):
                yield item

    async def _stream_request(self, snap, messages, usage, images=None,
                              include_usage=True, extra_tools=None,
                              tools=None, **kw) -> AsyncIterator[tuple]:
        """yield (kind, payload)：kind 为
        content / reasoning（推理模型思考增量）/
        annotations（内置搜索引用源，payload 为 list）/
        tool_call（OpenAI 流式 function call delta，payload 为单条 delta dict，
        含 index/id/type/function 增量，需在调用方按 index 累积）。"""
        messages = _inject_images(messages, images)
        body = {"model": snap.model_id, "messages": messages, "stream": True}
        if include_usage:
            body["stream_options"] = {"include_usage": True}
        # 函数调用工具与厂商内置工具（如 mimo web_search）共用同一个 tools 数组
        all_tools = []
        if tools:
            all_tools.extend(tools)
        if extra_tools:
            all_tools.extend(extra_tools)
        if all_tools:
            body["tools"] = all_tools
        body.update({k: v for k, v in kw.items()
                    if k in ("temperature", "max_tokens")})
        body.update(_normalize_extra_body(snap, kw.get("extra_body")))
        # 流式回复可持续数分钟：读超时按 chunk 间隔计时，用 stream 长超时
        async with httpx.AsyncClient(timeout=timeout_for("stream")) as c:
            async with c.stream("POST", f"{snap.base_url.rstrip('/')}/chat/completions",
                                json=body,
                                headers={"Authorization": f"Bearer {snap.api_key}"}) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        import json
                        obj = json.loads(data)
                    except ValueError:
                        continue
                    # 部分 Provider 会发 choices 为空的 chunk（仅 usage/心跳），需容错
                    choices = obj.get("choices") or []
                    if choices:
                        ch0 = choices[0]
                        delta = ch0.get("delta") or {}
                        # 内置联网搜索（mimo 等）：首包携带结构化引用源
                        ann = (delta.get("annotations") or ch0.get("annotations")
                               or (ch0.get("message") or {}).get("annotations"))
                        if ann:
                            yield "annotations", ann
                        # DeepSeek 等推理模型：思考过程走 reasoning_content 增量
                        # Providers use several names for the same semantic
                        # reasoning block. Normalize them at the adapter edge.
                        rc = (delta.get("reasoning_content")
                              or delta.get("reasoning")
                              or delta.get("thinking")
                              or delta.get("thought"))
                        if not rc:
                            message = ch0.get("message") or {}
                            rc = (message.get("reasoning_content")
                                  or message.get("reasoning")
                                  or message.get("thinking"))
                        if isinstance(rc, dict):
                            rc = rc.get("text") or rc.get("content") or ""
                        if rc:
                            yield "reasoning", str(rc)
                        content = delta.get("content")
                        if content:
                            yield "content", content
                        tc_deltas = delta.get("tool_calls")
                        if tc_deltas:
                            for tcd in tc_deltas:
                                yield "tool_call", tcd
                    if obj.get("usage"):
                        normalized = normalize_usage(obj["usage"])
                        usage.update(normalized)


def _merge_tool_call_delta(acc: dict, delta: dict) -> None:
    """按 index 累积 OpenAI 流式 tool_call 增量：id/type 覆盖，
    function.name/arguments 拼接。"""
    idx = delta.get("index", 0)
    slot = acc.get(idx)
    if slot is None:
        slot = {"id": delta.get("id") or "",
                "type": delta.get("type") or "function",
                "function": {"name": "", "arguments": ""}}
        acc[idx] = slot
    if delta.get("id"):
        slot["id"] = delta["id"]
    if delta.get("type"):
        slot["type"] = delta["type"]
    fn_delta = delta.get("function") or {}
    if fn_delta.get("name"):
        slot["function"]["name"] += fn_delta["name"]
    if fn_delta.get("arguments"):
        slot["function"]["arguments"] += fn_delta["arguments"]


def _finalize_tool_calls(acc: dict) -> list[dict]:
    return [acc[k] for k in sorted(acc.keys())] if acc else []
