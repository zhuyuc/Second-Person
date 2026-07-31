-- ============================================================
-- 004：会话置顶支持
-- ============================================================
ALTER TABLE sessions
ADD COLUMN pinned INTEGER DEFAULT 0;
ALTER TABLE sessions
ADD COLUMN pinned_at TEXT;