"""
BaseTool + 工具注册表（产品文档 §工具系统 / 开发文档 §6.2）。

统一接口分层实现：Agent 看到同一种工具，底层分两条路径。
- Path A 内置：进程内直接调用，零网络开销
- Path B MCP：通过 MCP 协议标准化调用，外部工具前缀 {connector_id}__{tool_name}
每轮对话把所有已注册工具的 schema 全部加载到 prompt。
破坏性工具（file_write / shell_exec / memory_save 非主动触发）标记 destructive=True。
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]        # JSON Schema
    destructive: bool = False
    source: str = "builtin"           # builtin / mcp
    connector_id: str | None = None


class BaseTool:
    spec: ToolSpec

    async def run(self, **kwargs) -> Any:  # noqa: D401
        raise NotImplementedError


class FunctionTool(BaseTool):
    """把普通函数包装为工具。"""

    def __init__(self, spec: ToolSpec, fn: Callable[..., Any | Awaitable[Any]]):
        self.spec = spec
        self._fn = fn

    async def run(self, **kwargs) -> Any:
        result = self._fn(**kwargs)
        if inspect.isawaitable(result):
            return await result
        return result


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        self._tools[tool.spec.name] = tool

    def register_function(self, spec: ToolSpec,
                          fn: Callable[..., Any]) -> None:
        self.register(FunctionTool(spec, fn))

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def unregister_connector(self, connector_id: str) -> None:
        """注销某连接器的全部工具。"""
        for name in [n for n, t in self._tools.items()
                     if t.spec.connector_id == connector_id]:
            self._tools.pop(name, None)

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        return name in self._tools

    def all_specs(self) -> list[ToolSpec]:
        return [t.spec for t in self._tools.values()]

    def openai_schemas(self) -> list[dict]:
        """输出 OpenAI function-calling tools schema 供注入 prompt。"""
        return [{
            "type": "function",
            "function": {
                "name": t.spec.name,
                "description": t.spec.description,
                "parameters": t.spec.parameters,
            },
        } for t in self._tools.values()]

    def is_destructive(self, name: str) -> bool:
        t = self._tools.get(name)
        return bool(t and t.spec.destructive)
