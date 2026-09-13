-- ============================================================
-- 053 跨轮文件工作集 / 操作卡片
-- - fs_observations 增 last_op（read/write/edit）
-- - session_working_set_state：尾部注入指纹（不变则静默）
-- - session_file_cards：滚动操作痕迹（含 spill 定位）
-- ============================================================

ALTER TABLE fs_observations ADD COLUMN last_op TEXT;

CREATE TABLE IF NOT EXISTS session_working_set_state (
    session_id   TEXT PRIMARY KEY,
    fingerprint  TEXT NOT NULL,
    cards_fingerprint TEXT,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_file_cards (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    path        TEXT NOT NULL,
    op          TEXT NOT NULL,          -- read / write / edit / spill / list / glob / grep
    version     TEXT,
    spill_path  TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_file_cards_session
    ON session_file_cards(session_id, id DESC);
