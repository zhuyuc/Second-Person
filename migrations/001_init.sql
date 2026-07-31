-- ============================================================
-- Second Person 初始化 Schema（SQLite 派生索引层）
-- md 文件是 source of truth，本库为派生索引，--rebuild-index 可从 md 重建
-- 建表顺序：被引用表在前；无引用关系按业务模块聚合
-- ============================================================
-- ============================================================
-- 模块 A：凭证与 LLM Provider
-- ============================================================
CREATE TABLE IF NOT EXISTS credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    credential_type TEXT,
    -- connector / platform_bot
    encrypted_value BLOB NOT NULL,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS providers (
    id TEXT PRIMARY KEY,
    -- prov_001
    display_name TEXT NOT NULL,
    provider_type TEXT NOT NULL,
    -- openai_compatible/anthropic/google/custom
    base_url TEXT NOT NULL,
    model_id TEXT NOT NULL,
    credential_id INTEGER NOT NULL,
    input_price REAL,
    -- ¥/M tokens
    output_price REAL,
    context_window INTEGER NOT NULL,
    status TEXT DEFAULT 'healthy',
    -- healthy/unavailable
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS model_assignment (
    task_type TEXT PRIMARY KEY,
    -- chat/agent/embedding
    provider_id TEXT NOT NULL,
    updated_at TEXT
);
-- ============================================================
-- 模块 B：接入渠道与 IM 会话映射
-- ============================================================
CREATE TABLE IF NOT EXISTS platforms (
    id TEXT PRIMARY KEY,
    platform_type TEXT NOT NULL,
    -- web / feishu / dingtalk / telegram
    enabled INTEGER DEFAULT 1,
    status TEXT DEFAULT 'healthy',
    -- healthy / paused / error
    whitelist_user_id TEXT,
    callback_url TEXT,
    credential_id INTEGER,
    failure_count INTEGER DEFAULT 0,
    last_failure_time TEXT,
    last_failure_reason TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS platform_sessions (
    platform TEXT NOT NULL,
    platform_user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    created_at TEXT,
    PRIMARY KEY (platform, platform_user_id)
);
CREATE TABLE IF NOT EXISTS message_dedup (
    platform TEXT NOT NULL,
    message_id TEXT NOT NULL,
    processed_at TEXT NOT NULL,
    PRIMARY KEY (platform, message_id)
);
CREATE INDEX IF NOT EXISTS idx_dedup_time ON message_dedup(processed_at);
-- ============================================================
-- 模块 C：对话与会话
-- ============================================================
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    message_type TEXT DEFAULT 'normal',
    -- normal / system_notification
    notification_type TEXT,
    -- 系统通知类型，24h 内同类去重
    content TEXT NOT NULL,
    citations TEXT,
    feedback INTEGER DEFAULT 0,
    create_time TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv_session ON conversations(session_id);
-- 对话原文全文搜索（外部内容表模式，靠触发器同步）
CREATE VIRTUAL TABLE IF NOT EXISTS conversations_fts USING fts5(
    content,
    content = conversations,
    content_rowid = id
);
CREATE TRIGGER IF NOT EXISTS conv_ai
AFTER
INSERT ON conversations BEGIN
INSERT INTO conversations_fts(rowid, content)
VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS conv_ad
AFTER DELETE ON conversations BEGIN
INSERT INTO conversations_fts(conversations_fts, rowid, content)
VALUES('delete', old.id, old.content);
END;
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    title TEXT,
    title_source TEXT DEFAULT 'auto',
    -- auto / manual
    compressed_summary_path TEXT,
    last_compressed_message_id INTEGER,
    last_active TEXT,
    message_count INTEGER DEFAULT 0
);
-- ============================================================
-- 模块 D：记忆索引（md 文件的派生索引）
-- ============================================================
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    -- mem_000001
    title TEXT NOT NULL,
    summary TEXT,
    domain TEXT NOT NULL,
    confidence TEXT NOT NULL,
    -- strong/medium/low/disputed
    lifecycle TEXT NOT NULL DEFAULT 'active',
    -- active/stable/stale/archived/missing
    source_type TEXT NOT NULL,
    -- memory/knowledge/hybrid
    access_count INTEGER DEFAULT 0,
    last_accessed TEXT,
    is_important INTEGER DEFAULT 0,
    implicit_use_count INTEGER DEFAULT 0,
    md_missing INTEGER DEFAULT 0,
    user_marked_stale INTEGER DEFAULT 0,
    dedup_pending INTEGER DEFAULT 0,
    created_by TEXT DEFAULT 'distiller',
    -- distiller / user_explicit / import
    md_path TEXT NOT NULL,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_mem_lifecycle ON memories(lifecycle);
CREATE INDEX IF NOT EXISTS idx_mem_domain ON memories(domain);
CREATE INDEX IF NOT EXISTS idx_mem_important ON memories(is_important);
CREATE INDEX IF NOT EXISTS idx_mem_vectorpending ON memories(dedup_pending);
-- 记忆全文搜索（应用层双写，非外部内容表模式）
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(memory_id, title, summary, detail, domain);
CREATE TABLE IF NOT EXISTS vectors (
    memory_id TEXT PRIMARY KEY,
    embedding BLOB,
    -- 占位行时为 NULL
    vector_status TEXT DEFAULT 'pending',
    -- pending / ready / failed
    dim INTEGER,
    embedding_version TEXT NOT NULL,
    is_stale INTEGER DEFAULT 0,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS memory_links (
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    link_type TEXT NOT NULL,
    -- related/evolved_from/superseded_by/contradicts/supports
    PRIMARY KEY (source_id, target_id)
);
CREATE INDEX IF NOT EXISTS idx_links_target ON memory_links(target_id);
CREATE TABLE IF NOT EXISTS memory_entities (
    entity_id TEXT PRIMARY KEY,
    entity_name TEXT NOT NULL,
    entity_type TEXT,
    -- company/person/concept/technology/event/metric
    first_seen TEXT,
    memory_count INTEGER DEFAULT 0,
    primary_domain TEXT
);
CREATE TABLE IF NOT EXISTS memory_entity_links (
    memory_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    PRIMARY KEY (memory_id, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_mel_entity ON memory_entity_links(entity_id);
CREATE TABLE IF NOT EXISTS memory_timeline (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    -- created/updated/evolved/imported/archived/missing/merged
    detail TEXT,
    event_time TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_timeline_time ON memory_timeline(event_time);
CREATE TABLE IF NOT EXISTS lint_suggestions (
    suggestion_id TEXT PRIMARY KEY,
    -- sug_{uuid8}
    lint_run_id TEXT NOT NULL,
    suggestion_type TEXT NOT NULL,
    -- orphan / duplicate
    primary_memory_id TEXT NOT NULL,
    related_memory_id TEXT,
    detail TEXT,
    status TEXT DEFAULT 'open',
    -- open / adopted / dismissed
    dismiss_reason TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_lintsug_status ON lint_suggestions(status);
-- ============================================================
-- 模块 E：原始素材与技能
-- ============================================================
CREATE TABLE IF NOT EXISTS raw_docs (
    id TEXT PRIMARY KEY,
    -- doc_0001
    filename TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    mime_type TEXT,
    source TEXT,
    -- web_ui / im_platform / url
    source_url TEXT,
    extracted_memory_ids TEXT,
    -- JSON 数组
    imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS skill_usage (
    skill_id TEXT PRIMARY KEY,
    skill_name TEXT NOT NULL,
    status TEXT DEFAULT 'active',
    -- active/draft/archived
    trigger_embedding BLOB,
    use_count INTEGER DEFAULT 0,
    last_used TEXT
);
-- ============================================================
-- 模块 F：写入队列与信号采集
-- ============================================================
CREATE TABLE IF NOT EXISTS pending_writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    write_type TEXT NOT NULL,
    -- memory/profile/soul_style/skill
    payload TEXT NOT NULL,
    -- JSON
    status TEXT DEFAULT 'pending',
    -- pending/processing/done/failed
    retry_count INTEGER DEFAULT 0,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_writes(status);
CREATE TABLE IF NOT EXISTS response_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    char_count INTEGER,
    paragraph_count INTEGER,
    bullet_count INTEGER,
    code_block_count INTEGER,
    table_count INTEGER,
    conclusion_position TEXT,
    -- start / middle / end
    explicit_reaction INTEGER,
    -- 0=无 1=赞 2=踩
    implicit_reaction TEXT,
    -- follow_up_clarify / new_topic
    explicit_keywords TEXT,
    context_label TEXT,
    -- fact_query/opinion/chat/tech_help/other
    create_time TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signals_time ON response_signals(create_time);
-- ============================================================
-- 模块 G：Token 用量与调度日志
-- ============================================================
CREATE TABLE IF NOT EXISTS token_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name TEXT,
    source TEXT,
    -- main_chat / system_agent / title_gen / embedding
    session_id TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    trace_id TEXT,
    create_time TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_session ON token_usage(session_id);
CREATE INDEX IF NOT EXISTS idx_usage_date ON token_usage(create_time);
CREATE INDEX IF NOT EXISTS idx_usage_source ON token_usage(source);
CREATE INDEX IF NOT EXISTS idx_usage_model ON token_usage(model_name);
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    task_id TEXT PRIMARY KEY,
    name TEXT,
    schedule TEXT,
    status TEXT,
    last_run TEXT,
    next_run TEXT
);
CREATE TABLE IF NOT EXISTS task_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    run_time TEXT NOT NULL,
    duration_ms INTEGER,
    result TEXT,
    -- success / failed / skipped
    fail_reason TEXT,
    trigger_source TEXT DEFAULT 'schedule' -- schedule / manual
);
CREATE INDEX IF NOT EXISTS idx_task_logs_time ON task_logs(run_time);
CREATE TABLE IF NOT EXISTS operation_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation TEXT,
    detail TEXT,
    trace_id TEXT,
    create_time TEXT
);
CREATE TABLE IF NOT EXISTS embedding_migration (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_model TEXT,
    to_model TEXT,
    total_count INTEGER,
    done_count INTEGER,
    status TEXT,
    -- running / paused / completed / failed
    started_at TEXT
);
-- ============================================================
-- Seed：Web 默认渠道（不可删除，DELETE 该 id 返回 400）
-- ============================================================
INSERT
    OR IGNORE INTO platforms(id, platform_type, enabled, status, created_at)
VALUES(
        'web_default',
        'web',
        1,
        'healthy',
        datetime('now')
    );