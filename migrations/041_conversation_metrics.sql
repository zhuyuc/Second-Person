-- 041: 会话级性能指标
-- 每个 Agent step 保存一次稳定的请求计时和 provider usage，供会话投影读取。
ALTER TABLE token_usage ADD COLUMN cache_read_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE token_usage ADD COLUMN cache_write_tokens INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS agent_step_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id TEXT NOT NULL,
    step INTEGER NOT NULL,
    llm_ms INTEGER NOT NULL DEFAULT 0,
    ttft_ms INTEGER,
    decode_ms INTEGER,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    tool_ms INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(turn_id, step)
);
CREATE INDEX IF NOT EXISTS idx_agent_step_metrics_turn
ON agent_step_metrics(turn_id, step);
