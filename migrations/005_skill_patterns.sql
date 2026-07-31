-- ============================================================
-- 005：技能提炼「同类任务 3 次」计数
-- 记录 Distiller 判定为 skill 的任务模式出现次数，达阈值才生成 draft
-- ============================================================
CREATE TABLE IF NOT EXISTS skill_patterns (
    pattern_key TEXT PRIMARY KEY,
    -- 归一化后的技能标题/模式
    title TEXT,
    detail TEXT,
    occurrences INTEGER DEFAULT 1,
    drafted INTEGER DEFAULT 0,
    -- 是否已生成 draft（避免重复）
    first_seen TEXT,
    last_seen TEXT
);