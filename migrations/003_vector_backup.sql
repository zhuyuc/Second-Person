-- ============================================================
-- 003：Embedding 迁移的旧向量备份（保留 30 天可回滚，产品文档 §LLM Provider）
-- ============================================================
CREATE TABLE IF NOT EXISTS vectors_old_backup (
    memory_id TEXT PRIMARY KEY,
    embedding BLOB,
    dim INTEGER,
    embedding_version TEXT,
    backed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vecbak_time ON vectors_old_backup(backed_at);