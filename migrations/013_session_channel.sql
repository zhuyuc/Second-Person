-- ============================================================
-- 013：会话渠道来源（IM 渠道创建的会话记录 platform_type，Web 端为 NULL）
-- ============================================================
ALTER TABLE sessions
ADD COLUMN channel TEXT;
-- 回填：已有 platform_sessions 映射的会话标记为对应渠道
UPDATE sessions
SET channel = (
        SELECT platform
        FROM platform_sessions ps
        WHERE ps.session_id = sessions.session_id
    )
WHERE session_id IN (
        SELECT session_id
        FROM platform_sessions
    );