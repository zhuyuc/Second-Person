-- 029_session_handoff：会话继承关系与 handoff 摘要（会话上下文管理方案 v2）
ALTER TABLE sessions
ADD COLUMN from_session TEXT;
ALTER TABLE sessions
ADD COLUMN readonly INTEGER DEFAULT 0;
ALTER TABLE sessions
ADD COLUMN ended_at TEXT;
ALTER TABLE sessions
ADD COLUMN succeeded_by TEXT;
ALTER TABLE sessions
ADD COLUMN handoff_summary_path TEXT;
-- from_session 索引：加速链路回溯查询
CREATE INDEX IF NOT EXISTS idx_sessions_from ON sessions(from_session);
-- readonly 索引：侧栏分组过滤
CREATE INDEX IF NOT EXISTS idx_sessions_readonly ON sessions(readonly);