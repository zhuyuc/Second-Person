-- ============================================================
-- 054 视频工坊（Video Workshop）
-- 单视频资产表：列表库 + 创作参数 + 成片引用
-- ============================================================

CREATE TABLE IF NOT EXISTS video_projects (
    id            TEXT PRIMARY KEY,              -- vp_[0-9a-f]{12}
    title         TEXT NOT NULL,
    type          TEXT NOT NULL,                 -- 暂定枚举：广告片/AI短剧/…
    status        TEXT NOT NULL DEFAULT 'draft', -- draft|doing|done|failed
    script        TEXT NOT NULL DEFAULT '',
    aspect        TEXT NOT NULL DEFAULT '16:9',
    duration_sec  REAL NOT NULL DEFAULT 0,       -- 0 = 使用当前模型默认
    params_json   TEXT NOT NULL DEFAULT '{}',
    refs_json     TEXT NOT NULL DEFAULT '[]',     -- 参考图 dataURL 或文件名列表
    filename      TEXT,
    public_url    TEXT,
    poster_url    TEXT,
    progress      INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    session_id    TEXT,                          -- channel=workshop 代写会话
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_video_projects_updated
    ON video_projects(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_video_projects_type
    ON video_projects(type);
CREATE INDEX IF NOT EXISTS idx_video_projects_status
    ON video_projects(status);
