-- 048: 第三轮优化补的三个热点索引。
-- 1) get_messages 高频查询 WHERE session_id=? AND (is_active=1 OR is_active IS NULL)
--    ORDER BY id DESC，原 idx_conv_session 只覆盖 session_id，分支化后扫描大量
--    inactive 版本。partial index 只索引活跃行，体积小、命中快。
CREATE INDEX IF NOT EXISTS idx_conv_session_active_id
ON conversations(session_id, id DESC) WHERE is_active=1 OR is_active IS NULL;

-- 2) 会话列表 ORDER BY pinned DESC, last_active DESC（含 archived/project_id 过滤）
--    无匹配复合索引，侧栏刷新 sort/scan。覆盖最常见过滤+排序路径。
CREATE INDEX IF NOT EXISTS idx_sessions_list
ON sessions(archived, project_id, pinned DESC, last_active DESC);

-- 3) vector_compensator 轮询 WHERE vector_status='pending'，vectors 表无此索引，
--    pending 行增多后全表扫描。partial index 只索引 pending 行。
CREATE INDEX IF NOT EXISTS idx_vectors_pending
ON vectors(memory_id) WHERE vector_status='pending';
