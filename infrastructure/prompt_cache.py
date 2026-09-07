"""Stable prompt fingerprints and cache change diagnostics.

Only hashes and counters leave this module. Prompt contents are deliberately
never persisted, so the diagnostics can be enabled without expanding the
sensitive data surface of agent traces.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any


PROMPT_CACHE_VERSION = "prompt-cache-v1"
_SESSION_CONTEXT_KEYS = (
    "history",
    "history_ids",
    "handoff_context",
    "project_context",
    "project_instructions",
    "project_instructions_changes",
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def session_context_payload(context: dict[str, Any]) -> dict[str, Any]:
    """Return the stable context portion; retrieval results stay in the tail."""
    return {key: context.get(key) for key in _SESSION_CONTEXT_KEYS
            if context.get(key) is not None}


@dataclass(frozen=True)
class PromptFingerprint:
    system_prompt_hash: str
    tool_schema_hash: str
    session_context_hash: str
    prefix_hash: str


def build_prompt_fingerprint(system_prompt: str, tools: list[dict],
                             session_context: Any) -> PromptFingerprint:
    """Hash the exact stable components sent to the model.

    Dict keys are canonicalized while list ordering is retained. Tool ordering
    therefore remains visible because it can affect provider byte-prefix reuse.
    """
    system_hash = _digest(system_prompt or "")
    tool_hash = _digest(_canonical_json(tools or []))
    context_hash = _digest(_canonical_json(session_context))
    prefix_hash = _digest(_canonical_json({
        "version": PROMPT_CACHE_VERSION,
        "system_prompt_hash": system_hash,
        "tool_schema_hash": tool_hash,
    }))
    return PromptFingerprint(system_hash, tool_hash, context_hash, prefix_hash)


def classify_prompt_change(previous: PromptFingerprint | None,
                           current: PromptFingerprint,
                           *, compacted: bool = False) -> tuple[str, bool]:
    """Return a concise reason and whether the stable system/tool prefix reused."""
    if previous is None:
        return "initial", False
    prefix_reused = (previous.system_prompt_hash == current.system_prompt_hash
                     and previous.tool_schema_hash == current.tool_schema_hash)
    if compacted:
        return "compaction_rebuilt_prefix", prefix_reused
    if previous.system_prompt_hash != current.system_prompt_hash:
        return "system_prompt_changed", False
    if previous.tool_schema_hash != current.tool_schema_hash:
        return "tool_schema_changed", False
    if previous.session_context_hash != current.session_context_hash:
        return "session_context_changed", prefix_reused
    return "stable_prefix_reused", prefix_reused
