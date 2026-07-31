-- ============================================================
-- 014_time_cst_fix：历史时间数据统一为中国标准时间（UTC+8）
--
-- 背景：schema_migrations.applied_at / platforms.created_at /
-- lint_suggestions.resolved_at 三列此前由 SQLite datetime('now') 写入，
-- 值为 UTC 且为空格分隔格式（YYYY-MM-DD HH:MM:SS）；
-- 其余全部时间列由 Python 侧写入，本就是东八区 T 分隔 ISO 格式。
--
-- 处理：仅匹配空格分隔格式的行（即 UTC 来源），+8 小时并统一为
-- T 分隔 ISO 格式。转换后不再匹配该格式，天然幂等，不会重复加 8 小时。
-- ============================================================
UPDATE schema_migrations
SET applied_at = strftime(
        '%Y-%m-%dT%H:%M:%S',
        datetime(applied_at, '+8 hours')
    )
WHERE applied_at LIKE '____-__-__ __:__:__';
UPDATE platforms
SET created_at = strftime(
        '%Y-%m-%dT%H:%M:%S',
        datetime(created_at, '+8 hours')
    )
WHERE created_at LIKE '____-__-__ __:__:__';
UPDATE lint_suggestions
SET resolved_at = strftime(
        '%Y-%m-%dT%H:%M:%S',
        datetime(resolved_at, '+8 hours')
    )
WHERE resolved_at LIKE '____-__-__ __:__:__';