-- ============================================================
-- 045 项目说明书 baseline reconciliation 状态表
-- - 每个 session 一行，记录已注入的 workspace instruction 快照
-- - files_hash：所有文件（path + per-file hash）的聚合 sha256
-- - paths_json：{display_path: file_hash} 用于变化时对比出 changed / removed
-- - payload：已注入的完整 baseline 文本（诊断/回放用，不参与命中判断）
-- 存在此行 → 该 session 已注入过 baseline，后续按 hash 比对走 reconcile
-- 不存在   → 首次注入，走 "initial" 分支
-- ============================================================

CREATE TABLE IF NOT EXISTS session_project_baseline (
    session_id  TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL,
    files_hash  TEXT NOT NULL,
    paths_json  TEXT NOT NULL,        -- JSON: {display_path: file_hash}
    payload     TEXT,                 -- 已注入内容（可空，用于事后诊断）
    total_bytes INTEGER NOT NULL DEFAULT 0,
    truncated   INTEGER NOT NULL DEFAULT 0,
    injected_at TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_spb_project
    ON session_project_baseline(project_id);
