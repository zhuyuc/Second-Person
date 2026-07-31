"""
DAG 流程编排（开发文档 §1.1 第 5 步 / §6.4 DAG 容错）。

- 拓扑排序：无依赖并行，有依赖串行
- 环检测：发现循环依赖不终止本轮，降级为单意图直出
- 依赖容错：depends_on 引用不存在/自身 → 丢弃该依赖；含未注册工具 → 剔除
- SharedState：请求级键值容器，键 {intent_id}.{tool_name}.{序号}，1MB 上限
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .intent_parser import Intent

logger = logging.getLogger("second_person.dag")

MAX_KEY_BYTES = 1024 * 1024


class SharedState:
    """请求级键值容器，请求结束即销毁，不落库不跨请求。"""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        # 延迟写入：file_write 等工具在回复生成后才写入内容
        # 每项 {"path": str, "mode": str}
        self.deferred_writes: list[dict] = []
        # 延迟导出：generate_document 的正文由主回复填充，回复生成后再导出
        # 每项 {"title": str, "format": str}
        self.deferred_docs: list[dict] = []

    def put(self, intent_id: str, tool_name: str, seq: int, value: Any) -> str:
        key = f"{intent_id}.{tool_name}.{seq}"
        serialized = json.dumps(value, ensure_ascii=False, default=str)
        if len(serialized.encode("utf-8")) > MAX_KEY_BYTES:
            value = {"_summary": serialized[:500], "_truncated": True}
        self._data[key] = value
        return key

    def get_for_intent(self, depends_on: list[str]) -> dict[str, Any]:
        """只返回 depends_on 声明意图的键值。"""
        out = {}
        for k, v in self._data.items():
            owner = k.split(".", 1)[0]
            if owner in depends_on:
                out[k] = v
        return out


class DAGResult:
    def __init__(self, order: list[list[str]], degraded: bool, reason: str = ""):
        self.order = order          # 分层：每层内并行
        self.degraded = degraded    # 环检测降级为单意图直出
        self.reason = reason


def build_dag(intents: list[Intent], registered_tools: set[str]) -> DAGResult:
    ids = {it.id for it in intents}
    by_id = {it.id: it for it in intents}

    # 依赖容错
    for it in intents:
        cleaned = []
        for dep in it.depends_on:
            if dep == it.id:
                logger.warning("意图 %s 依赖自身，丢弃", it.id)
                continue
            if dep not in ids:
                logger.warning("意图 %s 依赖不存在的 %s，丢弃", it.id, dep)
                continue
            cleaned.append(dep)
        it.depends_on = cleaned
        # 未注册工具剔除
        it.tools_needed = [t for t in it.tools_needed if t in registered_tools]

    # 拓扑排序（Kahn），检测环
    indeg = {i: 0 for i in ids}
    adj: dict[str, list[str]] = {i: [] for i in ids}
    for it in intents:
        for dep in it.depends_on:
            adj[dep].append(it.id)
            indeg[it.id] += 1

    layers: list[list[str]] = []
    frontier = [i for i in ids if indeg[i] == 0]
    visited = 0
    while frontier:
        layers.append(sorted(frontier))
        nxt = []
        for node in frontier:
            visited += 1
            for m in adj[node]:
                indeg[m] -= 1
                if indeg[m] == 0:
                    nxt.append(m)
        frontier = nxt

    if visited < len(ids):
        # 存在环 → 降级为单意图直出
        return DAGResult([[i for i in ids]], degraded=True,
                         reason="意图间存在循环依赖，降级为直接回答")
    return DAGResult(layers, degraded=False)
