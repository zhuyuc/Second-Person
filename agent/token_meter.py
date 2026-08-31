"""Hybrid token metering for compaction pressure detection.

Two facts drive the design:

- provider precise usage is the ground truth for pricing decisions but only
  arrives after a request completes;
- tiktoken estimation is cheap and available before a request but drifts on
  systematic per-provider counting differences (tool-schema framing, image
  tokens, cached-prefix discounts).

`TokenMeter` reconciles the two: it keeps a per-session **anchor** — the
provider's own accounting from the last successful step — and prices the
current request as `anchor + tiktoken(delta)` where the delta is only the
messages added after the anchor was committed. Errors bound to the delta
never compound across turns because every successful step re-anchors.

The anchor exposes `cache_read_tokens` separately so the caller can decide
whether to use `total_tokens` (raw prompt size) or `uncached_tokens` (bytes
the provider is actually going to spend uncached tokens on this turn) as
its pressure signal — cache reads price at ~1/10 of uncached input for
DeepSeek/Anthropic today.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("second_person.token_meter")

# 无锚点冷启动时的密度兜底：中文场景 tiktoken 已足够准，1.0 = 直接用估算值
DEFAULT_ROLE_OVERHEAD = 4    # 每条 message 的角色/框架开销（对齐 dsh）
DEFAULT_BLOCK_OVERHEAD = 4   # 每个 content block 的 JSON 结构开销
# tail_hash 覆盖尾部消息数——足够检出"截断/重放/编辑"，太长影响命中
TAIL_HASH_MSG_COUNT = 5


@dataclass(frozen=True)
class Anchor:
    """Last-known-good provider accounting for one session.

    A step commits an anchor when its LLM call succeeded and reported usage.
    The next `measure()` reprices new messages against this anchor rather
    than re-estimating the whole prompt, so tokenizer differences that
    would otherwise compound across a turn are bounded to the tail delta.
    """
    exact_prompt_tokens: int      # provider 报的 input（已含 cache_read/write，Anthropic 归一后）
    cache_read_tokens: int        # cache 命中部分（未 cache 才是真实计费压力）
    cache_write_tokens: int
    message_count: int            # 锚点时的 messages 长度（不含 system）
    tail_hash: str                # 尾部 N 条消息内容 hash，验证锚点仍适用
    system_hash: str              # system prompt 的 hash（system 变了必须重估）


@dataclass(frozen=True)
class TokenMeasurement:
    """Snapshot of what one prompt costs right now."""
    total_tokens: int
    uncached_tokens: int          # 与 total 相同当无 anchor 信息时；有 anchor 时扣掉 cache_read
    source: str                   # "anchor+delta" | "estimate-only"
    anchor: Anchor | None = None  # 复算用的锚点（如果本次走了 anchor 分支）
    per_message: tuple[int, ...] = field(default_factory=tuple)


class TokenMeter:
    """In-memory anchor store + hybrid pricing.

    Anchors are per-session and per-process; a restart discards them, and
    the next `measure()` after restart re-estimates from scratch. Losing
    anchors is safe — it degrades to pure estimation, never to over-count
    to the point of skipping a needed compaction.
    """

    def __init__(self, encoding_name: str = "cl100k_base"):
        self._anchors: dict[str, Anchor] = {}
        try:
            import tiktoken
            self._enc = tiktoken.get_encoding(encoding_name)
        except Exception:  # noqa: BLE001
            logger.warning("tiktoken 不可用，退回字符密度估算")
            self._enc = None

    # ---- 公共 API -------------------------------------------------------

    def measure(self, session_id: str, messages: list[dict],
                system: str = "", tools: list[dict] | None = None
                ) -> TokenMeasurement:
        """Price the current prompt for `session_id`.

        Returns `total_tokens` (full prompt bytes as pricing) and
        `uncached_tokens` (drop cache_read to see the real per-turn spend).
        """
        anchor = self._anchors.get(session_id)
        system_hash = self._hash_text(system or "")
        if anchor is not None and self._anchor_valid(anchor, messages, system_hash):
            delta_msgs = messages[anchor.message_count:]
            delta_tokens = sum(self._price_message(m) for m in delta_msgs)
            total = anchor.exact_prompt_tokens + delta_tokens
            uncached = (anchor.exact_prompt_tokens - anchor.cache_read_tokens) + delta_tokens
            return TokenMeasurement(
                total_tokens=total,
                uncached_tokens=max(0, uncached),
                source="anchor+delta",
                anchor=anchor,
                per_message=tuple(self._price_message(m) for m in messages),
            )
        # 冷启动 / 锚点失效：全量估算
        system_tokens = self._price_text(system) if system else 0
        tools_tokens = self._price_tools(tools) if tools else 0
        per_msg = tuple(self._price_message(m) for m in messages)
        total = system_tokens + tools_tokens + sum(per_msg)
        return TokenMeasurement(
            total_tokens=total,
            uncached_tokens=total,
            source="estimate-only",
            anchor=None,
            per_message=per_msg,
        )

    def commit_anchor(self, session_id: str, messages: list[dict],
                       usage: dict[str, Any] | None,
                       system: str = "") -> None:
        """Fold a successful step's provider usage into the session's anchor.

        `usage` is the shape returned by `infrastructure.llm_provider.normalize_usage`:
        `{input_tokens, output_tokens, cache_read_tokens, cache_write_tokens}`.
        `input_tokens` already includes cache_read/write for Anthropic (the
        normalize_usage boundary reconciles the two conventions).
        A missing / empty usage silently keeps the previous anchor — the
        adapter may not have surfaced usage on this attempt, and we would
        rather stick with an old anchor than throw one away.
        """
        if not usage:
            return
        try:
            exact_prompt = int(usage.get("input_tokens") or 0)
            cache_read = int(usage.get("cache_read_tokens") or 0)
            cache_write = int(usage.get("cache_write_tokens") or 0)
        except (TypeError, ValueError):
            return
        if exact_prompt <= 0:
            return
        self._anchors[session_id] = Anchor(
            exact_prompt_tokens=exact_prompt,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            message_count=len(messages),
            tail_hash=self._tail_hash(messages),
            system_hash=self._hash_text(system or ""),
        )

    def drop_anchor(self, session_id: str) -> None:
        """Explicitly clear a session's anchor (used by compaction which
        rewrites history and invalidates the prior message count)."""
        self._anchors.pop(session_id, None)

    def has_anchor(self, session_id: str) -> bool:
        return session_id in self._anchors

    # ---- 内部：锚点校验 --------------------------------------------------

    def _anchor_valid(self, anchor: Anchor, messages: list[dict],
                      system_hash: str) -> bool:
        # System 变化必须重估（tool_prompts 版本升级、SOUL 变化等）
        if anchor.system_hash != system_hash:
            return False
        # messages 短于 anchor 时必然失效（压缩/删除/重生成）
        if len(messages) < anchor.message_count:
            return False
        # 锚点位置的尾部内容变化时失效（消息被就地编辑）
        if len(messages) == anchor.message_count:
            return anchor.tail_hash == self._tail_hash(messages)
        # messages 长于 anchor：只需锚点位置往前的尾部未被篡改
        return anchor.tail_hash == self._tail_hash(messages[:anchor.message_count])

    # ---- 内部：定价 -----------------------------------------------------

    def _price_message(self, message: dict) -> int:
        """Content + role framing overhead. Handles str/list content and
        tool_calls; unknown shapes fall back to str() coercion."""
        content = message.get("content", "")
        tokens = DEFAULT_ROLE_OVERHEAD
        if isinstance(content, str):
            tokens += self._price_text(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    text = (block.get("text") or block.get("content") or "")
                    tokens += self._price_text(str(text)) + DEFAULT_BLOCK_OVERHEAD
                else:
                    tokens += self._price_text(str(block)) + DEFAULT_BLOCK_OVERHEAD
        else:
            tokens += self._price_text(str(content))
        # tool_calls 结构：name + arguments JSON
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            name = fn.get("name") or ""
            args = fn.get("arguments") or ""
            tokens += (self._price_text(name) + self._price_text(str(args))
                       + DEFAULT_BLOCK_OVERHEAD)
        return tokens

    def _price_tools(self, tools: list[dict]) -> int:
        """OpenAI-style tool schemas — JSON-serialize and price at density."""
        import json
        try:
            body = json.dumps(tools, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            body = str(tools)
        return self._price_text(body)

    def _price_text(self, text: str) -> int:
        if not text:
            return 0
        if self._enc is not None:
            try:
                return len(self._enc.encode(text))
            except Exception:  # noqa: BLE001
                pass
        # 兜底：中文经验密度
        return max(1, int(len(text) / 2.5))

    # ---- 内部：hash -----------------------------------------------------

    def _tail_hash(self, messages: list[dict]) -> str:
        tail = messages[-TAIL_HASH_MSG_COUNT:]
        h = hashlib.sha256()
        for m in tail:
            h.update((m.get("role") or "").encode("utf-8"))
            h.update(b"\0")
            content = m.get("content") or ""
            if not isinstance(content, str):
                content = str(content)
            h.update(content.encode("utf-8", errors="replace"))
            h.update(b"\0")
        return h.hexdigest()

    @staticmethod
    def _hash_text(text: str) -> str:
        return hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest()
