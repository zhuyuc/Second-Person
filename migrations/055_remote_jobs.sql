-- ============================================================
-- 055 远端长任务句柄（RemoteJob）
-- 提交后立即落盘 remote_id；本地等待不得撕票；仅显式取消或远端终态结案
-- ============================================================

CREATE TABLE IF NOT EXISTS remote_jobs (
    id              TEXT PRIMARY KEY,           -- rj_[0-9a-f]{12}
    kind            TEXT NOT NULL,              -- kling_video|comfy_video|comfy_image|cloud_image
    backend         TEXT NOT NULL,              -- kling|comfyui|openai_image|...
    remote_id       TEXT NOT NULL DEFAULT '',   -- 云端 task_id / Comfy prompt_id
    status          TEXT NOT NULL DEFAULT 'running',
    -- running|succeeded|failed|cancelled
    session_id      TEXT,
    owner_type      TEXT NOT NULL DEFAULT 'chat_tool',
    -- chat_tool|workshop|gateway|system
    owner_ref       TEXT,                       -- project_id / tool_call / crid
    base_url        TEXT,
    result_ref      TEXT,                       -- 本地文件名或 URL
    error_message   TEXT,
    meta_json       TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    settled_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_remote_jobs_status_updated
    ON remote_jobs(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_remote_jobs_session
    ON remote_jobs(session_id, status);
CREATE INDEX IF NOT EXISTS idx_remote_jobs_owner
    ON remote_jobs(owner_type, owner_ref);
CREATE INDEX IF NOT EXISTS idx_remote_jobs_remote
    ON remote_jobs(backend, remote_id);
