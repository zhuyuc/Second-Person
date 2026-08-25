"""Host-side tool execution safeguards."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from tools import hooks

logger = logging.getLogger("second_person.tool_exec")


class ToolExecutor:
    def __init__(self, registry, config,
                 notifier: Callable[[str, str], None] | None = None):
        self.registry = registry
        self.config = config
        self.notify = notifier or (lambda _topic, _message: None)

    async def execute_tool(self, tool_name: str, params: dict, *,
                           intent_summary: str = "",
                           emit: Callable[[str, dict], Awaitable[None]] | None = None,
                           session_id: str = "") -> dict[str, Any]:
        """Validate, execute, redact, and return one tool result."""
        del intent_summary, emit, session_id
        from langfuse.integration import get_tracer, mark_preview
        import json

        tracer = get_tracer()
        span = tracer.span_start("tool_execute", input={
            "tool": tool_name,
            "params": mark_preview(json.dumps(params, ensure_ascii=False, default=str),
                                    content_type="tool_params"),
        })
        tool = self.registry.get(tool_name)
        if not tool:
            error = f"工具不存在：{tool_name}"
            span.end(level="ERROR", output={"ok": False, "error": error})
            return {"ok": False, "error": error}
        error = hooks.validate_params(tool.spec.parameters, params)
        if error:
            span.end(level="ERROR", output={"ok": False, "error": error})
            return {"ok": False, "error": error}
        result, error = await self._run_with_empty_retry(tool, params)
        if error:
            span.end(level="ERROR", output={"ok": False, "error": error})
            return {"ok": False, "error": error}
        redacted, credential_hit, injection_hit = hooks.post_tool_process(result)
        if credential_hit:
            logger.info("工具输出命中凭证，已脱敏：%s", tool_name)
        if injection_hit:
            logger.warning("工具输出疑似含注入指令，已隔离标注：%s", tool_name)
            self.notify("injection_guard", f"工具 {tool_name} 返回的外部内容疑似包含注入指令，已隔离标注")
        span.end(output={"ok": True, "redacted": credential_hit,
                         "injection": injection_hit,
                         "result": mark_preview(redacted, content_type="tool_result")})
        return {"ok": True, "result": redacted}

    async def _run_with_empty_retry(self, tool, params) -> tuple[Any, str | None]:
        timeout = self.config.get("tool_timeout_seconds", 60)
        for attempt in range(2):
            try:
                result = await asyncio.wait_for(tool.run(**params), timeout=timeout)
            except asyncio.TimeoutError:
                return None, f"工具执行超时（>{timeout}s）"
            except Exception as exc:  # noqa: BLE001
                return None, str(exc)
            if not hooks.is_empty_result(result) or attempt == 1:
                return result, None
        return None, "工具返回空结果"

    async def replan(self, failed_intent_summary: str, tool_name: str, error: str,
                     replan_fn: Callable[[str, str, str], Awaitable[dict]]) -> dict:
        """Retained as a host hook for callers that want an explicit retry plan."""
        try:
            return await replan_fn(failed_intent_summary, tool_name, error)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Replan 失败：%s", exc)
            return {"action": "skip", "reason": str(exc)}
