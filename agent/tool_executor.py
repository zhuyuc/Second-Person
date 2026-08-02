"""
工具执行（开发文档 §1.1 第 6 步 / §6.4 Replan）。

- 无确认设计：所有工具直接执行，不弹确认框不阻塞对话
 （做错了通过重新生成/重新提问纠正，优于中断式确认）
- 按轮次执行：同层内 asyncio 并行；ReAct 模式 LLM 驱动工具调用
- post_tool hook：空结果重试一次、凭证脱敏
- Replan：部分失败时换工具/参数重试（每请求最多 1 次）
"""
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
        self.notify = notifier or (lambda t, m: None)

    async def execute_tool(self, tool_name: str, params: dict, *,
                           intent_summary: str = "",
                           emit: Callable[[str, dict],
                                          Awaitable[None]] | None = None
                           ) -> dict[str, Any]:
        """执行单个工具，含 pre/post hook。返回 {ok, result, skipped, error}。
        每个工具调用记录独立 Langfuse span（挂当前 trace/span 下），
        参数/结果统一走 mark_preview 标记（content_type + 原始长度 + 截断标志）。"""
        from observability_langfuse import get_tracer, mark_preview
        import json as _json
        _sp = get_tracer().span_start("tool_execute", input={
            "tool": tool_name,
            "intent_summary": intent_summary or "",
            "params": mark_preview(
                _json.dumps(params, ensure_ascii=False, default=str),
                content_type="tool_params")})
        tool = self.registry.get(tool_name)
        if not tool:
            _sp.end(level="ERROR",
                    output={"ok": False, "error": f"工具不存在：{tool_name}"})
            return {"ok": False, "error": f"工具不存在：{tool_name}"}

        # 参数校验
        err = hooks.validate_params(tool.spec.parameters, params)
        if err:
            _sp.end(level="ERROR", output={"ok": False, "error": err})
            return {"ok": False, "error": err}

        # 执行（空结果重试一次）
        result, error = await self._run_with_empty_retry(tool, params)
        if error:
            _sp.end(level="ERROR", output={"ok": False, "error": error})
            return {"ok": False, "error": error}

        # post_tool：凭证脱敏 + 外部内容注入防护
        redacted, hit, inj = hooks.post_tool_process(result)
        if hit:
            logger.info("工具输出命中凭证，已脱敏：%s", tool_name)
        if inj:
            logger.warning("工具输出疑似含注入指令，已隔离标注：%s", tool_name)
            self.notify("injection_guard",
                        f"工具 {tool_name} 返回的外部内容疑似包含注入指令，已隔离标注")
        _sp.end(output={
            "ok": True, "redacted": hit, "injection": inj,
            "result": mark_preview(redacted, content_type="tool_result")})
        return {"ok": True, "result": redacted}

    async def _run_with_empty_retry(self, tool, params) -> tuple[Any, str | None]:
        result = None
        # 单工具独立超时：某工具卡死不再吞掉整个请求 600s 预算
        timeout = self.config.get("tool_timeout_seconds", 60)
        for attempt in range(2):
            try:
                result = await asyncio.wait_for(tool.run(**params), timeout=timeout)
            except asyncio.TimeoutError:
                return None, f"工具执行超时（>{timeout}s）"
            except Exception as e:  # noqa: BLE001
                return None, str(e)
            if not hooks.is_empty_result(result) or attempt == 1:
                break
        return result, None

    async def replan(self, failed_intent_summary: str, tool_name: str, error: str,
                     replan_fn: Callable[[str, str, str], Awaitable[dict]]) -> dict:
        """DAG 层面 Replan（每请求最多 1 次）。replan_fn 由 core 提供（调 chat_model）。"""
        try:
            return await replan_fn(failed_intent_summary, tool_name, error)
        except Exception as e:  # noqa: BLE001
            logger.warning("Replan 失败：%s", e)
            return {"action": "skip", "reason": str(e)}
