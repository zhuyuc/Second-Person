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
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator

import httpx

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
    input_price: float = 0.0
    output_price: float = 0.0
    context_window: int = 128000


class TokenRecorder:
    def __init__(self, db):
        self.db = db

    def record(self, model_name: str, source: str, input_tokens: int,
               output_tokens: int, session_id: str | None = None) -> None:
        try:
            # 火忘式写入：聊天热路径上的高频小写，入队即返回零等待，
            # 由单写线程串行落库，失败由写线程记日志
            self.db.execute_nowait(
                "INSERT INTO token_usage(model_name,source,session_id,input_tokens,"
                "output_tokens,trace_id,create_time) VALUES(?,?,?,?,?,?,?)",
                (model_name, source, session_id, input_tokens, output_tokens,
                 get_trace_id(), now_cst().isoformat(timespec="seconds")))
        except Exception:  # noqa: BLE001
            logger.exception("token_usage 记录失败")


class LLMError(RuntimeError):
    pass


class CircuitOpenError(LLMError):
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
                   extra_body: dict | None = None,
                   **kw) -> dict[str, Any]:
        """返回 {content, tool_calls, usage}。

        images：可选图片 dataURL 列表（多模态）。
        extra_body：透传至 API 请求体的额外字段（如 {"thinking_enabled": false}
          对推理模型禁用思考模式）。收敛分析等轻量结构化任务建议传
          extra_body={"thinking_enabled": False} 以避免思考令牌拖慢响应。
        """
        from observability_langfuse import get_tracer
        gen = get_tracer().generation_start(
            name=f"llm.{source}", model=snap.model_id, input=messages,
            metadata={"source": source, "session_id": session_id,
                      "images": len(images) if images else 0},
            model_parameters={k: kw[k] for k in ("temperature", "max_tokens")
                              if k in kw} or None)
        try:
            if extra_body:
                kw["extra_body"] = extra_body
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
            gen.end(output=out, usage={
                "input": u.get("input_tokens", 0), "output": u.get("output_tokens", 0),
                "total": u.get("input_tokens", 0) + u.get("output_tokens", 0),
                "unit": "TOKENS"})
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
        from observability_langfuse import get_tracer
        gen = get_tracer().generation_start(
            name=f"llm.{source}", model=snap.model_id, input=messages,
            metadata={"source": source, "session_id": session_id,
                      "images": len(images) if images else 0,
                      "builtin_tools": len(extra_tools) if extra_tools else 0},
            model_parameters={k: kw[k] for k in ("temperature", "max_tokens")
                              if k in kw} or None)
        breaker = self.breaker(snap.model_id)
        if not breaker.allow():
            gen.end(level="ERROR", status_message="熔断中")
            raise CircuitOpenError(f"模型 {snap.model_id} 熔断中")
        started = False   # 是否已向下游产出过任意内容（含 reasoning/annotations）
        try:
            full: list[str] = []
            usage = {"input_tokens": 0, "output_tokens": 0}
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
                                     inp, outp, session_id)
            gen.end(output="".join(full), usage={
                "input": usage["input_tokens"], "output": usage["output_tokens"],
                "total": usage["input_tokens"] + usage["output_tokens"],
                "unit": "TOKENS"})
        except Exception as e:  # noqa: BLE001
            # 熔断计数已在重试循环内按口径处理，此处只负责上报与透传
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
                                         u.get("output_tokens", 0), session_id)
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
            return await self._anthropic_chat(snap, messages, tools, **kw)
        if snap.provider_type == "google":
            return await self._google_chat(snap, messages, **kw)
        return await self._openai_chat(snap, _inject_images(messages, images), tools, **kw)

    async def _openai_chat(self, snap, messages, tools, **kw) -> dict[str, Any]:
        body: dict[str, Any] = {"model": snap.model_id, "messages": messages}
        if tools:
            body["tools"] = tools
        body.update({k: v for k, v in kw.items()
                    if k in ("temperature", "max_tokens")})
        # extra_body：透传至请求体的额外字段（如 thinking_enabled=False 禁用思考模式）
        if "extra_body" in kw and isinstance(kw["extra_body"], dict):
            body.update(kw["extra_body"])
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(f"{snap.base_url.rstrip('/')}/chat/completions",
                             json=body,
                             headers={"Authorization": f"Bearer {snap.api_key}"})
            r.raise_for_status()
            data = r.json()
        choices = data.get("choices") or [{}]
        choice = choices[0].get("message", {}) if choices else {}
        usage = data.get("usage", {})
        return {
            "content": choice.get("content") or "",
            "tool_calls": choice.get("tool_calls") or [],
            "annotations": choice.get("annotations") or [],
            "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                      "output_tokens": usage.get("completion_tokens", 0)},
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
        if "extra_body" in kw and isinstance(kw["extra_body"], dict):
            body.update(kw["extra_body"])
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(f"{snap.base_url.rstrip('/')}/messages", json=body,
                             headers={"x-api-key": snap.api_key,
                                      "anthropic-version": "2023-06-01"})
            r.raise_for_status()
            data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text")
        usage = data.get("usage", {})
        return {"content": text, "tool_calls": [],
                "usage": {"input_tokens": usage.get("input_tokens", 0),
                          "output_tokens": usage.get("output_tokens", 0)}}

    async def _google_chat(self, snap, messages, **kw) -> dict[str, Any]:
        contents = [{"role": "user" if m["role"] != "assistant" else "model",
                     "parts": [{"text": m.get("content", "")}]}
                    for m in messages if m["role"] != "system"]
        url = (f"{snap.base_url.rstrip('/')}/models/{snap.model_id}:generateContent"
               f"?key={snap.api_key}")
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(url, json={"contents": contents})
            r.raise_for_status()
            data = r.json()
        text = "".join(
            p.get("text", "") for p in
            data.get("candidates", [{}])[0].get("content", {}).get("parts", []))
        um = data.get("usageMetadata", {})
        return {"content": text, "tool_calls": [],
                "usage": {"input_tokens": um.get("promptTokenCount", 0),
                          "output_tokens": um.get("candidatesTokenCount", 0)}}

    async def _do_embed(self, snap, texts) -> list[list[float]]:
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(f"{snap.base_url.rstrip('/')}/embeddings",
                             json={"model": snap.model_id, "input": texts},
                             headers={"Authorization": f"Bearer {snap.api_key}"})
            r.raise_for_status()
            data = r.json()
        return [item["embedding"] for item in data["data"]]

    async def _do_stream(self, snap, messages, usage, images=None,
                         extra_tools=None, **kw) -> AsyncIterator[tuple]:
        """先带 stream_options.include_usage 请求（OpenAI 官方系必须显式开启才返回
        流式 usage）；个别网关不认识该参数报 400 时去掉降级重试一次（400 发生在
        首 chunk 之前，重试不会产生重复内容）。"""
        try:
            async for item in self._stream_request(snap, messages, usage,
                                                   images=images, include_usage=True,
                                                   extra_tools=extra_tools, **kw):
                yield item
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 400:
                raise
            async for item in self._stream_request(snap, messages, usage,
                                                   images=images, include_usage=False,
                                                   extra_tools=extra_tools, **kw):
                yield item

    async def _stream_request(self, snap, messages, usage, images=None,
                              include_usage=True, extra_tools=None,
                              **kw) -> AsyncIterator[tuple]:
        """yield (kind, text)：kind 为 content / reasoning（推理模型思考增量）/
        annotations（内置搜索引用源，chunk 为 list）。"""
        messages = _inject_images(messages, images)
        body = {"model": snap.model_id, "messages": messages, "stream": True}
        if include_usage:
            body["stream_options"] = {"include_usage": True}
        if extra_tools:
            body["tools"] = extra_tools
        body.update({k: v for k, v in kw.items()
                    if k in ("temperature", "max_tokens")})
        async with httpx.AsyncClient(timeout=120) as c:
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
                        rc = delta.get("reasoning_content")
                        if rc:
                            yield "reasoning", rc
                        content = delta.get("content")
                        if content:
                            yield "content", content
                    if obj.get("usage"):
                        usage["input_tokens"] = obj["usage"].get(
                            "prompt_tokens", 0)
                        usage["output_tokens"] = obj["usage"].get(
                            "completion_tokens", 0)
