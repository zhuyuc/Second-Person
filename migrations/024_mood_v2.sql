-- ============================================================
-- 024：双向情绪系统 v2（规则触发 + 归因 + 传染 + 平复 + 主动行为）
-- 基于 016_mood 扩展：新增触发日志表 + mood_state 归因/行为字段
-- ============================================================

-- 情绪触发日志（规则通道 + LLM 通道均可登记）
CREATE TABLE IF NOT EXISTS mood_triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    message_id INTEGER,
    scope TEXT NOT NULL,              -- 'user' | 'ai'
    source_type TEXT NOT NULL,        -- task | evaluation | relation | self | temporal | conflict
    event_key TEXT NOT NULL,          -- task_repeat_fail | user_thumbs_up | long_absence_return 等
    attribution TEXT,                 -- self | other | shared | none
    mood_hint TEXT,                   -- 建议情绪标签
    intensity_hint REAL,              -- 建议强度 0~1
    note TEXT,                        -- 补充说明
    detected_by TEXT NOT NULL,        -- 'rule' | 'llm'
    create_time TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mt_session_msg ON mood_triggers(session_id, message_id);
CREATE INDEX IF NOT EXISTS idx_mt_session_time ON mood_triggers(session_id, create_time);

-- mood_state 扩展：归因 + 主动行为 + 平复时间戳
ALTER TABLE mood_state ADD COLUMN user_attribution TEXT DEFAULT '';
ALTER TABLE mood_state ADD COLUMN ai_attribution TEXT DEFAULT '';
ALTER TABLE mood_state ADD COLUMN active_action TEXT DEFAULT '';
ALTER TABLE mood_state ADD COLUMN last_peace_event_at TEXT DEFAULT NULL;
