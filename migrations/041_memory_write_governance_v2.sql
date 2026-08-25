-- 长期记忆写入治理 v2：候选池、证据、状态机和写入来源元数据。
CREATE TABLE IF NOT EXISTS memory_write_candidates (
    candidate_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    source_message_id INTEGER,
    session_id TEXT,
    source_type TEXT NOT NULL,
    attribution TEXT NOT NULL,
    title TEXT,
    summary TEXT,
    detail TEXT,
    domain TEXT,
    score REAL NOT NULL DEFAULT 0,
    stability REAL NOT NULL DEFAULT 0,
    reuse REAL NOT NULL DEFAULT 0,
    user_specificity REAL NOT NULL DEFAULT 0,
    evidence_count INTEGER NOT NULL DEFAULT 0,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending',
    decision_reason TEXT,
    expires_at TEXT,
    confirmed_at TEXT,
    written_memory_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mwc_status_expiry
ON memory_write_candidates(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_mwc_fingerprint
ON memory_write_candidates(fingerprint, status);

ALTER TABLE memories ADD COLUMN write_channel TEXT DEFAULT 'system';
ALTER TABLE memories ADD COLUMN write_score REAL DEFAULT 0;
ALTER TABLE memories ADD COLUMN evidence_count INTEGER DEFAULT 0;
ALTER TABLE memories ADD COLUMN last_verified_at TEXT;
ALTER TABLE memories ADD COLUMN expires_at TEXT;
ALTER TABLE memories ADD COLUMN sensitivity_level TEXT DEFAULT 'none';

CREATE INDEX IF NOT EXISTS idx_mem_write_channel ON memories(write_channel);
CREATE INDEX IF NOT EXISTS idx_mem_expires ON memories(expires_at);
