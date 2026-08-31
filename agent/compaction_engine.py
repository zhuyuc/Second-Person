"""Automatic history compaction driven by token pressure.

Called from `TurnRuntime` at each step boundary. When the current prompt
crosses `threshold_ratio × context_window`, this engine:

1. picks a head-anchored contiguous span that leaves at least
   `retain_ratio × context_window` recent tokens intact and never splits
   an assistant tool_calls / tool result pair;
2. asks the routed chat model to condense that span using the shared
   `compact_instruction` — the summary is generated with the session's OWN
   system prompt as prefix so the provider's KV cache is warm;
3. wraps the summary with `compact_preamble` and persists it through
   `SessionStore.save_summary`, advancing `sessions.last_compressed_message_id`
   so the next `load_recovery_context` returns the compacted history.

The engine never touches turn events directly — the caller emits a
`context.compacted` event with the returned metadata so telemetry surfaces
what happened.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from infrastructure.prompt_loader import PROMPTS

from .token_meter import TokenMeasurement, TokenMeter

logger = logging.getLogger("second_person.compaction")


# ---- 常量：加载 prompt 模板 ------------------------------------------------

def _load_instruction() -> str:
    return PROMPTS.load_raw("agent/prompts/compact_instruction")


def _load_preamble() -> str:
    return PROMPTS.load_raw("agent/prompts/compact_preamble")


@dataclass(frozen=True)
class CompactionResult:
    """One completed compaction — surfaced back to callers for telemetry."""
    trigger: str                     # "pressure" | "overflow" | "manual"
    shadowed_count: int              # 被替换掉的 message 条数
    shadowed_message_ids: tuple[int, ...]
    last_shadowed_message_id: int    # 推给 sessions.last_compressed_message_id
    released_tokens_est: int         # 释放的 tokens 估算
    summary_text: str                # 摘要正文（已含 preamble 框架）
    total_before: int                # 压缩前总 tokens
    total_after_est: int             # 压缩后总 tokens 估算


@dataclass(frozen=True)
class _Span:
    """A validated head-anchored contiguous range to compact."""
    messages: tuple[dict, ...]
    message_ids: tuple[int, ...]
    tokens_est: int


class CompactionEngine:
    """Threshold policy + range selection + LLM summarization + persistence."""

    def __init__(self, *, db, sessions, llm, providers, meter: TokenMeter,
                 threshold_ratio: float = 0.8,
                 retain_ratio: float = 0.2,
                 max_retries: int = 1):
        self.db = db
        self.sessions = sessions
        self.llm = llm
        self.providers = providers
        self.meter = meter
        self.threshold_ratio = threshold_ratio
        self.retain_ratio = retain_ratio
        self.max_retries = max_retries

    # ------------------------------------------------------------------
    # 入口 1：pressure-driven（pre-step 自动调用）

    async def compact_if_needed(self, *, session_id: str, snap,
                                 messages: list[dict],
                                 system: str,
                                 tools: list[dict] | None,
                                 message_ids: list[int] | None = None,
                                 trigger: str = "pressure",
                                 ) -> CompactionResult | None:
        """Check pressure and compact when needed.

        `messages` is the CURRENT model-visible message list (already
        includes history + memory + baseline). `message_ids` is the
        parallel list of DB row ids for the entries that were persisted
        as `conversations` rows — a None or unknown id blocks compacting
        past that position (safety: we cannot advance the watermark past
        a message we cannot identify).
        """
        window = getattr(snap, "context_window", None) or 128000
        threshold = int(window * self.threshold_ratio)
        retain = int(window * self.retain_ratio)
        measurement = self.meter.measure(session_id, messages, system, tools)
        pressure = self._pressure(measurement)
        if pressure < threshold:
            return None
        result: CompactionResult | None = None
        attempts = 0
        while attempts <= self.max_retries:
            span = self._select_span(messages, message_ids, measurement, retain)
            if span is None:
                if result is not None:
                    return result
                logger.info("压缩：无可压区间（尾部保留占满全部消息），跳过")
                return None
            try:
                summary_text = await self._summarize(snap, span, system, tools,
                                                      session_id=session_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("压缩生成失败：%s", exc)
                # 失败标记：不阻断主链路，但让摘要文件带 compression_failed 便于观察
                try:
                    await self.sessions.mark_compression_failed(session_id)
                except Exception:  # noqa: BLE001
                    pass
                return result
            wrapped = self._wrap_preamble(summary_text)
            # 落盘 + 推水位
            try:
                await self.sessions.save_summary(session_id, wrapped,
                                                 span.message_ids[-1])
            except Exception as exc:  # noqa: BLE001
                logger.warning("摘要落盘失败：%s", exc)
                return result
            # 压缩后锚点必然失效（历史结构变了）
            self.meter.drop_anchor(session_id)
            result = CompactionResult(
                trigger=trigger,
                shadowed_count=len(span.messages),
                shadowed_message_ids=span.message_ids,
                last_shadowed_message_id=span.message_ids[-1],
                released_tokens_est=span.tokens_est,
                summary_text=wrapped,
                total_before=pressure,
                total_after_est=max(0, pressure - span.tokens_est),
            )
            attempts += 1
            if attempts > self.max_retries:
                break
            # 重估：如果仍超阈值就再压一轮（很少见）
            remaining = list(messages[len(span.messages):])
            remaining_ids = ((message_ids or [])[len(span.messages):]
                             if message_ids else None)
            remeasure = self.meter.measure(session_id, remaining, system, tools)
            if self._pressure(remeasure) < threshold:
                break
            messages = remaining
            message_ids = remaining_ids
            measurement = remeasure
        return result

    # ------------------------------------------------------------------
    # 入口 2：manual / on-demand（未来由 /compact 命令调用）

    async def compact_now(self, *, session_id: str, snap,
                           messages: list[dict], system: str,
                           tools: list[dict] | None,
                           message_ids: list[int] | None = None,
                           ) -> CompactionResult | None:
        """Force one compaction pass regardless of threshold (for `/compact`)."""
        return await self.compact_if_needed(
            session_id=session_id, snap=snap, messages=messages,
            system=system, tools=tools, message_ids=message_ids,
            trigger="manual")

    # ------------------------------------------------------------------
    # 内部：pressure 与 span 选择

    @staticmethod
    def _pressure(m: TokenMeasurement) -> int:
        # 有 anchor 时用 uncached_tokens 作压力判据（cache 命中不算真实压力）
        if m.source == "anchor+delta":
            return m.uncached_tokens
        return m.total_tokens

    def _select_span(self, messages: list[dict],
                     message_ids: list[int] | None,
                     measurement: TokenMeasurement,
                     retain_tokens: int) -> _Span | None:
        """Head-anchored range: keep tail ≥ retain_tokens, drop tool-pair-safe head.

        If per-message pricing is available, use it directly; otherwise
        redo tiktoken pricing here.
        """
        if not messages:
            return None
        if measurement.per_message and len(measurement.per_message) == len(messages):
            per = list(measurement.per_message)
        else:
            per = [self.meter._price_message(m) for m in messages]
        # 从尾部往前累加至 retain_tokens
        accumulated = 0
        keep_from = len(messages)
        for i in range(len(messages) - 1, -1, -1):
            accumulated += per[i]
            keep_from = i
            if accumulated >= retain_tokens:
                break
        # 尾部就已经装满 retain → 无可压
        if keep_from == 0:
            return None
        # 避免劈开 tool_calls / tool result 对：如果 keep_from 位置是 tool 消息，
        # 或者上一条是含 tool_calls 的 assistant，往前退到成对边界之外
        while keep_from > 0 and not self._pair_balanced_before(messages, keep_from):
            keep_from -= 1
        if keep_from == 0:
            return None
        head_msgs = messages[:keep_from]
        head_tokens = sum(per[:keep_from])
        # message_ids 校验：必须每条 head 消息都有 id 才能推进水位
        if not message_ids or len(message_ids) < keep_from:
            logger.debug("消息缺失 db id，无法安全推进水位；跳过")
            return None
        head_ids = tuple(message_ids[:keep_from])
        if any(mid is None or mid <= 0 for mid in head_ids):
            logger.debug("head 存在 None/0 id，无法定位水位；跳过")
            return None
        return _Span(
            messages=tuple(head_msgs),
            message_ids=head_ids,
            tokens_est=head_tokens,
        )

    @staticmethod
    def _pair_balanced_before(messages: list[dict], idx: int) -> bool:
        """`messages[:idx]` 结尾必须是"配对平衡"的状态。

        规则：assistant 里带 tool_calls 后必须紧跟对应数量的 role=tool 消息。
        idx 位置作为分界点，若 idx 上一条 assistant 有 tool_calls 但对应的 tool
        role 消息落在 idx 之后 → 劈开了工具调用对，返回 False。
        """
        if idx == 0:
            return True
        # 从 idx-1 往前找最近的 assistant with tool_calls
        pending_calls = 0
        for i in range(idx - 1, -1, -1):
            m = messages[i]
            role = m.get("role")
            if role == "tool":
                # 这条 tool 是响应上游 tool_calls，本身平衡
                pending_calls += 1
                continue
            if role == "assistant":
                calls = m.get("tool_calls") or []
                if calls:
                    # assistant 有 N 个 tool_calls，需要 N 个 tool 响应在其后
                    if len(calls) > pending_calls:
                        return False   # 有未闭合的调用
                    pending_calls -= len(calls)
                return True
            # user / system 消息不参与对判定
            return True
        return True

    # ------------------------------------------------------------------
    # 内部：LLM 调用摘要

    async def _summarize(self, snap, span: _Span, system: str,
                          tools: list[dict] | None,
                          session_id: str | None = None) -> str:
        """Call the routed chat model with the SESSION'S OWN prefix.

        `system` + `tools` + span messages + final instruction — mirrors the
        last routed request as a strict prefix so provider KV cache reuses
        the warm prefix. Only the final compaction instruction is novel.
        """
        instruction = _load_instruction()
        prompt: list[dict] = []
        if system:
            prompt.append({"role": "system", "content": system})
        # 复用会话 message 前缀；直接引用不复制
        prompt.extend(span.messages)
        prompt.append({"role": "user", "content": instruction})
        resp = await self.llm.chat(
            snap, prompt, source="system_agent",
            session_id=session_id,
            tools=tools,           # 复用工具 schema 保 KV cache 完整前缀
        )
        content = (resp.get("content") or "").strip()
        if not content:
            raise RuntimeError("摘要模型返回空内容")
        return content

    @staticmethod
    def _wrap_preamble(summary_body: str) -> str:
        preamble = _load_preamble()
        # 兼容占位符缺失时也能拼装
        if "{summary_body}" in preamble:
            return preamble.replace("{summary_body}", summary_body)
        return preamble + "\n\n" + summary_body
