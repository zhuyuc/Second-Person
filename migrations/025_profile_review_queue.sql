-- ============================================================
-- 025：画像后台确认机制（三条轨道共用队列 + 频次累积 + 拒绝保护）
-- 轨道：persona（AI 人格）/ user_profile（用户画像）/ output_style（输出喜好）
-- ============================================================
-- 画像变更待确认队列
CREATE TABLE IF NOT EXISTS profile_review_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_type TEXT NOT NULL,
    -- persona | user_profile | output_style
    change_key TEXT NOT NULL,
    -- 变更方向唯一标识（MD5 前 16 位）
    title TEXT NOT NULL,
    -- 用户可读标题
    proposed_content TEXT NOT NULL,
    -- 系统提议的新内容
    current_content TEXT,
    -- 当前画像相关段落（供对照）
    evidence TEXT,
    -- 学习依据（次数/时间/来源）
    conflict_reason TEXT,
    -- 冲突原因（如与现有画像方向相反）
    priority INTEGER DEFAULT 3,
    -- 1=高 2=中 3=低
    status TEXT DEFAULT 'pending',
    -- pending | confirmed | rejected | postponed | expired
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewed_by TEXT -- 'user' | 'system_expire'
);
CREATE INDEX IF NOT EXISTS idx_prq_status ON profile_review_queue(status, priority DESC);
CREATE INDEX IF NOT EXISTS idx_prq_created ON profile_review_queue(created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_prq_key ON profile_review_queue(change_key, status);
-- 拒绝保护记录（承接原 soul_preference_rejections 职责，扩充为三轨通用）
CREATE TABLE IF NOT EXISTS profile_review_rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_type TEXT NOT NULL,
    change_key TEXT NOT NULL UNIQUE,
    proposed_content_summary TEXT,
    rejected_at TEXT NOT NULL,
    protected_until TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prr_key ON profile_review_rejections(change_key, protected_until);
-- SOUL 反馈频次累积日志（替代原 ctx_entry.add_pending 即时注入）
CREATE TABLE IF NOT EXISTS soul_feedback_log (
    direction_key TEXT PRIMARY KEY,
    ptype TEXT NOT NULL DEFAULT 'behavior',
    proposed_change TEXT,
    summary TEXT,
    occurrences INTEGER DEFAULT 1,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    enqueued INTEGER DEFAULT 0
);
-- 输出喜好状态追踪（累积路径 vs 手编锁定）
CREATE TABLE IF NOT EXISTS output_style_state (
    id INTEGER PRIMARY KEY DEFAULT 1,
    last_instant_update_at TEXT,
    last_user_edit_at TEXT,
    last_build_at TEXT,
    locked_by_user INTEGER DEFAULT 0
);
INSERT
    OR IGNORE INTO output_style_state(id)
VALUES(1);