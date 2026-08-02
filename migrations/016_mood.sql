-- ============================================================
-- 016：情绪模块（双源：用户情绪 + AI 自身情绪）
-- 全局单例状态 + 变化历史；SQLite-only 动态状态（不走 md 主副本）
-- 情绪标签自由文本存储，无枚举约束（全放开设计）
-- ============================================================
CREATE TABLE IF NOT EXISTS mood_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    -- 全局单例
    user_mood TEXT NOT NULL DEFAULT 'neutral',
    -- 用户情绪标签
    user_intensity REAL NOT NULL DEFAULT 0.0,
    -- 用户情绪强度 0~1
    user_updated_at TEXT NOT NULL,
    -- 用户情绪更新时间
    user_source TEXT DEFAULT '',
    -- 来源（analysis/decay/reset）
    ai_mood TEXT NOT NULL DEFAULT 'neutral',
    -- AI 自身情绪标签
    ai_intensity REAL NOT NULL DEFAULT 0.0,
    -- AI 情绪强度 0~1
    ai_updated_at TEXT NOT NULL,
    -- AI 情绪更新时间
    ai_source TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS mood_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    -- 'user' | 'ai'
    mood TEXT NOT NULL,
    intensity REAL NOT NULL,
    source TEXT NOT NULL,
    note TEXT DEFAULT '',
    create_time TEXT NOT NULL
);