"""
Langfuse 集成（可观测性）—— 将事件化 Agent 运行时全链路上报到 Langfuse。

注意：本包**不依赖** pip 的 langfuse SDK，而是直接对接
Langfuse 官方 Ingestion REST API（POST /api/public/ingestion，Basic Auth）。
位于 langfuse/integration/，与部署脚本（langfuse/deploy/）统一归入 langfuse/ 目录。
无需额外安装依赖（复用 httpx）。

对外主要接口：
- init_tracer(config)  在应用启动装配时初始化全局 tracer
- get_tracer()         任意位置获取全局 tracer（未初始化则返回禁用态的空实现）
- PipelineTracer       追踪器：trace_start / span_start / generation_start
- mark_preview(value, *, content_type, limit)
                       预览字段统一标记（content_type + 原始长度 + 截断标志）
"""
from __future__ import annotations

from .config import LangfuseConfig
from .tracer import PipelineTracer, get_tracer, init_tracer, mark_preview

__all__ = ["LangfuseConfig", "PipelineTracer", "get_tracer", "init_tracer",
           "mark_preview"]
