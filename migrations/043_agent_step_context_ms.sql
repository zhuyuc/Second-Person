-- 043: 检索/prompt 组装耗时单独计量
-- ttft_ms 边界与 deepseek-harness 对齐后（覆盖 stream_chat 前后），
-- retrieval + LLM 精筛 + prompt 组装 耗时从 ttft_ms 里剥离到本字段，
-- 便于回归监控（context_ms 突然上涨 = 检索/记忆链路退化）。
ALTER TABLE agent_step_metrics ADD COLUMN context_ms INTEGER NOT NULL DEFAULT 0;
