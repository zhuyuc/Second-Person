-- 009: 引用明细表（记忆资产的"使用凭证"）
-- 每次 AI 回复正文引用记忆时记录一条事件；记忆源自知识库文档时同步回溯 doc_id，
-- 使记忆与知识库共用同一套引用溯源体系（详情页"被引用记录"/纠错影响面评估）。
CREATE TABLE IF NOT EXISTS citation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id TEXT NOT NULL,
    doc_id TEXT,
    -- 记忆源自知识库文档时为 raw_docs.id，否则 NULL
    message_id INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    cited_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cite_memory ON citation_events(memory_id);
CREATE INDEX IF NOT EXISTS idx_cite_doc ON citation_events(doc_id);