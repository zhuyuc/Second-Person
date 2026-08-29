"""
BaseTool + 工具注册表（产品文档 §工具系统 / 开发文档 §6.2）。

统一接口分层实现：Agent 看到同一种工具，底层分两条路径。
- Path A 内置：进程内直接调用，零网络开销
- Path B MCP：通过 MCP 协议标准化调用，外部工具前缀 {connector_id}__{tool_name}
每轮对话把所有已注册工具的 schema 全部加载到 prompt。
"""
from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


def _canonical_schema(schema: dict) -> dict:
    """Deterministic JSON key order via sort_keys roundtrip.

    LLM 提供商的 prefix cache 按 tools 字段的 JSON 字节序列比对；不同注册路径
    （MCP 动态注册、手写 spec、旧序列化）可能给出相同语义但键顺序不同的 dict，
    导致同一 tool 每次序列化字节不同，从而击穿 cache。这里强制字典按键名字母序
    排列，保证同一 tool schema 每次输出字节完全一致（键顺序对模型无语义影响）。
    """
    return json.loads(json.dumps(schema, sort_keys=True, ensure_ascii=False))


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]        # JSON Schema
    source: str = "builtin"           # builtin / mcp
    connector_id: str | None = None
    parallel_safe: bool = True


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
        """输出 OpenAI function-calling tools schema 供注入 prompt。

        _canonical_schema 强制 canonical 键顺序，保证跨请求字节稳定 → provider
        prefix cache 命中。
        """
        return [_canonical_schema({
            "type": "function",
            "function": {
                "name": t.spec.name,
                "description": t.spec.description,
                "parameters": t.spec.parameters,
            },
        }) for t in self._tools.values()]

    def openai_schemas_for(self, names: set[str] | None = None) -> list[dict]:
        """Return schemas constrained by a host-selected allowlist."""
        if names is None:
            return self.openai_schemas()
        return [_canonical_schema({
            "type": "function",
            "function": {
                "name": tool.spec.name,
                "description": tool.spec.description,
                "parameters": tool.spec.parameters,
            },
        }) for tool in self._tools.values() if tool.spec.name in names]

