-- 051: Prompt 稳定前缀缓存观测。
-- 只保存 hash、计数和变化原因，不保存 system/tool/session 原文。
ALTER TABLE agent_step_metrics ADD COLUMN system_prompt_hash TEXT;
ALTER TABLE agent_step_metrics ADD COLUMN tool_schema_hash TEXT;
ALTER TABLE agent_step_metrics ADD COLUMN session_context_hash TEXT;
ALTER TABLE agent_step_metrics ADD COLUMN prefix_hash TEXT;
ALTER TABLE agent_step_metrics ADD COLUMN cache_change_reason TEXT;
ALTER TABLE agent_step_metrics ADD COLUMN prefix_reused INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_agent_step_metrics_prompt_cache
ON agent_step_metrics(cache_change_reason, created_at);
