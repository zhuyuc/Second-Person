-- 040: 事件化 Agent Runtime。会话消息继续保留在 conversations，
-- 本表保存可恢复的任务步骤、工具调用和用户批准。
CREATE TABLE IF NOT EXISTS agent_turns (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    request_id TEXT,
    status TEXT NOT NULL,
    reasoning_effort TEXT NOT NULL,
    max_steps INTEGER NOT NULL,
    current_step INTEGER NOT NULL DEFAULT 0,
    end_reason TEXT,
    langfuse_trace_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    ended_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_turns_session_created
ON agent_turns(session_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_turns_request
ON agent_turns(request_id) WHERE request_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS agent_events (
    id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    step INTEGER NOT NULL DEFAULT 0,
    type TEXT NOT NULL,
    actor TEXT NOT NULL,
    call_id TEXT,
    model_visible INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL,
    artifact_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(turn_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_agent_events_turn_seq
ON agent_events(turn_id, seq);
CREATE INDEX IF NOT EXISTS idx_agent_events_call
ON agent_events(turn_id, call_id);

CREATE TABLE IF NOT EXISTS tool_approvals (
    id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    normalized_args_hash TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    scope_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decided_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(turn_id, call_id)
);

CREATE INDEX IF NOT EXISTS idx_tool_approvals_turn_status
ON tool_approvals(turn_id, status);

CREATE TABLE IF NOT EXISTS agent_artifacts (
    id TEXT PRIMARY KEY,
    event_id TEXT,
    storage_uri TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    preview TEXT,
    content_type TEXT,
    size INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_artifacts_event
ON agent_artifacts(event_id);
