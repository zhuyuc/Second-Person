-- operation_logs: create_time 索引（加速 purge_expired 的范围删除）
CREATE INDEX IF NOT EXISTS idx_oplog_create_time ON operation_logs(create_time);

-- sessions: 补充 created_at 列（会话创建时间，列表排序/统计用）
ALTER TABLE sessions ADD COLUMN created_at TEXT;
