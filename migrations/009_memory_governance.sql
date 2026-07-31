-- 记忆治理补齐：自动归档周期计数 / low 待确认询问时间 / 用户手动清除重要标记守卫
ALTER TABLE memories
ADD COLUMN stale_lint_runs INTEGER DEFAULT 0;
ALTER TABLE memories
ADD COLUMN low_confirm_asked_at TEXT;
ALTER TABLE memories
ADD COLUMN user_cleared_important INTEGER DEFAULT 0;
-- 文档导入预览模式：raw_docs 增加人工筛选状态（NULL=静默直入库 / pending=待筛选 / reviewed=已筛选）
ALTER TABLE raw_docs
ADD COLUMN review_status TEXT;
-- 预览暂存表：silent_doc_import=false 时提炼结果先落此表，用户勾选确认后才写入记忆
CREATE TABLE IF NOT EXISTS pending_imports (
    doc_id TEXT PRIMARY KEY,
    items TEXT NOT NULL,
    -- JSON: [{title,summary,detail,domain,entities,attribution,confidence,reason}]
    created_at TEXT
);
