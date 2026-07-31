-- 第 8 步主动记忆检测：含新事实但未走 remember_intent 的用户消息，
-- 标记为下次被动回顾的优先候选（回顾 Agent 消费后删除）
CREATE TABLE IF NOT EXISTS review_candidates (
    message_id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    created_at TEXT
);