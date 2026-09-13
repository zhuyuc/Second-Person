"""Host-side tool execution safeguards."""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from tools import hooks

logger = logging.getLogger("second_person.tool_exec")

# 副作用工具：空结果/超时后禁止自动再跑一遍（避免双扣费、双写、远端双任务）
_SIDE_EFFECT_NO_EMPTY_RETRY = frozenset({
    "generate_image",
    "generate_video",
    "shell_exec",
    "web_fetch",
    "web_search",
    "memory_save",
    "file_write",
    "fs_write",
    "fs_edit",
    "generate_document",
    "format_template_save",
})

_NO_AUTO_RETRY_HINT = "请勿自动重试同一调用，以免远端/副作用重复执行"


class ToolExecutor:
    def __init__(self, registry, config,
                 notifier: Callable[[str, str], None] | None = None,
                 workspace_resolver=None,
                 data_dir=None,
                 file_cards=None):
        self.registry = registry
        self.config = config
        self.notify = notifier or (lambda _topic, _message: None)
        # M3：fs 工具族依赖此解析器；无解析器时 fs_* 工具不会被注册，无影响
        self.workspace_resolver = workspace_resolver
        self.data_dir = Path(data_dir) if data_dir else None
        self.file_cards = file_cards

    @staticmethod
    def _is_side_effect_tool(tool, tool_name: str) -> bool:
        if tool_name in _SIDE_EFFECT_NO_EMPTY_RETRY:
            return True
        spec = getattr(tool, "spec", None)
        if spec is None:
            return False
        if getattr(spec, "source", "") == "mcp":
            return True
        if not getattr(spec, "parallel_safe", True):
            return True
        return False

    async def execute_tool(self, tool_name: str, params: dict, *,
                           intent_summary: str = "",
                           emit: Callable[[str, dict], Awaitable[None]] | None = None,
                           session_id: str = "",
                           turn_image_names: list[str] | None = None) -> dict[str, Any]:
        """Validate, execute, redact, and return one tool result."""
        del intent_summary
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
        # M3：需要工作区上下文的工具由 spec.needs_workspace 标注
        if getattr(tool.spec, "needs_workspace", False):
            if self.workspace_resolver is None:
                error = "工具需要工作区上下文但未装配 WorkspaceResolver"
                span.end(level="ERROR", output={"ok": False, "error": error})
                return {"ok": False, "error": error}
            ctx = self.workspace_resolver.resolve(session_id)
            params = {**params, "_ws_ctx": ctx}
        # 文生图/文生视频：注入 emit / session_id / 本轮参考图
        if tool_name in ("generate_image", "generate_video"):
            params = {**params, "_emit": emit, "_session_id": session_id}
            if tool_name == "generate_video" and turn_image_names:
                params = {**params, "_turn_image_names": list(turn_image_names)}
        allow_retry = not self._is_side_effect_tool(tool, tool_name)
        result, error = await self._run_with_empty_retry(
            tool, params, tool_name=tool_name, empty_retry=allow_retry)
        if error:
            span.end(level="ERROR", output={"ok": False, "error": error})
            return {"ok": False, "error": error}
        redacted, credential_hit, injection_hit = hooks.post_tool_process(result)
        if credential_hit:
            logger.info("工具输出命中凭证，已脱敏：%s", tool_name)
        if injection_hit:
            logger.warning("工具输出疑似含注入指令，已隔离标注：%s", tool_name)
            self.notify("injection_guard", f"工具 {tool_name} 返回的外部内容疑似包含注入指令，已隔离标注")
        # 脱敏/隔离之后再 spill，避免磁盘残留明文密钥
        spill_cap = hooks.resolve_spill_inline_cap(self.config)
        spill_path = None
        if spill_cap and self.data_dir is not None:
            redacted, spill_path = hooks.maybe_spill_result(
                redacted,
                data_dir=self.data_dir,
                session_id=session_id,
                tool_name=tool_name,
                call_id=uuid.uuid4().hex[:12],
                max_inline_bytes=spill_cap,
                max_file_bytes=hooks.resolve_spill_max_file_bytes(self.config),
            )
            if spill_path and self.file_cards is not None and session_id:
                try:
                    self.file_cards.append(
                        session_id, spill_path, "spill",
                        spill_path=spill_path)
                except Exception:  # noqa: BLE001
                    logger.debug("记录 spill 文件卡片失败", exc_info=True)
        span.end(output={"ok": True, "redacted": credential_hit,
                         "injection": injection_hit,
                         "result": mark_preview(redacted, content_type="tool_result")})
        return {"ok": True, "result": redacted}

    async def _run_with_empty_retry(self, tool, params, *,
                                    tool_name: str = "",
                                    empty_retry: bool = True) -> tuple[Any, str | None]:
        timeout = self.config.get("tool_timeout_seconds", 60)
        # 图/视频：跟到远端终态或用户取消，不用墙钟 TimeoutError 判死刑。
        unbounded_media = tool_name in ("generate_image", "generate_video")
        if not unbounded_media:
            if tool_name == "generate_image":
                timeout = max(
                    int(timeout or 60),
                    int(self.config.get("image_gen_timeout_sec", 180) or 180) + 30)
            elif tool_name == "generate_video":
                timeout = max(
                    int(timeout or 60),
                    int(self.config.get("video_gen_timeout_sec", 600) or 600) + 60)
        side_effect = self._is_side_effect_tool(tool, tool_name)
        attempts = 2 if empty_retry and not side_effect else 1
        for attempt in range(attempts):
            try:
                if unbounded_media:
                    result = await tool.run(**params)
                else:
                    result = await asyncio.wait_for(
                        tool.run(**params), timeout=timeout)
            except asyncio.CancelledError:
                return None, "已停止生成"
            except asyncio.TimeoutError:
                msg = f"工具执行超时（>{timeout}s）"
                if side_effect:
                    msg = f"{msg}。{_NO_AUTO_RETRY_HINT}"
                return None, msg
            except Exception as exc:  # noqa: BLE001
                from infrastructure.remote_jobs import JobCancelled
                if isinstance(exc, JobCancelled) or "已停止生成" in str(exc):
                    return None, "已停止生成"
                return None, str(exc)
            if not hooks.is_empty_result(result) or attempt == attempts - 1:
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
