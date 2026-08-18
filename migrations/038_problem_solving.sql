-- 038: 通用问题解决系统的安全分析元数据与可恢复长文交付任务。
-- 不保存模型原生推理；只保存模式决策、需求覆盖和质量状态等可验证摘要。
ALTER TABLE conversations ADD COLUMN analysis_metadata_json TEXT;

CREATE TABLE IF NOT EXISTS delivery_jobs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    request_key TEXT NOT NULL,
    status TEXT NOT NULL,
    contract_json TEXT NOT NULL,
    problem_model_json TEXT NOT NULL,
    base_system TEXT NOT NULL,
    result_text TEXT NOT NULL DEFAULT '',
    quality_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_delivery_jobs_session_status
ON delivery_jobs(session_id, status, updated_at);

CREATE TABLE IF NOT EXISTS delivery_sections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES delivery_jobs(id) ON DELETE CASCADE,
    section_key TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    title TEXT NOT NULL,
    requirement_ids_json TEXT NOT NULL,
    status TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    quality_json TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(job_id, section_key)
);

CREATE INDEX IF NOT EXISTS idx_delivery_sections_job_sequence
ON delivery_sections(job_id, sequence);
