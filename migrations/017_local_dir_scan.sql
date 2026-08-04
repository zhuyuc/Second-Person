-- 017: 本地目录全域接入（个人知识接入）
-- 用户配置本地目录后，系统按扫描间隔检测新增/变更文件并自动提炼为记忆；
-- 文件复制进 raw_docs（不可变素材语义），本表仅记录跟踪状态与来源映射。
CREATE TABLE IF NOT EXISTS local_dirs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    -- 源目录绝对路径
    enabled INTEGER NOT NULL DEFAULT 1,
    -- 启用/暂停扫描
    recursive INTEGER NOT NULL DEFAULT 1,
    -- 是否包含子目录
    last_scan_at TEXT,
    -- 上次扫描时间（CST ISO）
    last_scan_summary TEXT,
    -- 上次扫描结果摘要 JSON
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS local_dir_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dir_id INTEGER NOT NULL,
    -- 所属目录
    path TEXT NOT NULL,
    -- 源文件绝对路径
    fingerprint TEXT NOT NULL,
    -- size_mtime_ns 指纹（变更检测）
    doc_id TEXT,
    -- 关联 raw_docs.id（导入后回填）
    status TEXT NOT NULL DEFAULT 'pending',
    -- pending/imported/failed/deleted
    fail_reason TEXT,
    last_seen_at TEXT NOT NULL,
    -- 最近一次扫描见到该文件的时间
    imported_at TEXT,
    UNIQUE(dir_id, path)
);
CREATE INDEX IF NOT EXISTS idx_ldf_dir ON local_dir_files(dir_id);
CREATE INDEX IF NOT EXISTS idx_ldf_status ON local_dir_files(dir_id, status);