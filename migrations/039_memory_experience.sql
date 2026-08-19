-- 记忆使用体验闭环：证据、版本、反馈、检索轨迹和治理队列
ALTER TABLE memories ADD COLUMN verification_state TEXT DEFAULT 'unverified';
ALTER TABLE memories ADD COLUMN freshness_state TEXT DEFAULT 'current';
ALTER TABLE memories ADD COLUMN usefulness_score REAL DEFAULT 0;
ALTER TABLE memories ADD COLUMN valid_from TEXT;
ALTER TABLE memories ADD COLUMN review_after TEXT;
ALTER TABLE memories ADD COLUMN superseded_by TEXT;
ALTER TABLE memories ADD COLUMN retrieval_negative_count INTEGER DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_mem_verification ON memories(verification_state);
CREATE INDEX IF NOT EXISTS idx_mem_freshness ON memories(freshness_state);
CREATE INDEX IF NOT EXISTS idx_mem_review_after ON memories(review_after);

CREATE TABLE IF NOT EXISTS memory_evidence (
    evidence_id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_ref TEXT,
    locator TEXT,
    excerpt TEXT,
    excerpt_hash TEXT,
    captured_at TEXT NOT NULL,
    status TEXT DEFAULT 'active',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_evidence_memory ON memory_evidence(memory_id);

CREATE TABLE IF NOT EXISTS memory_revisions (
    revision_id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL,
    revision_no INTEGER NOT NULL,
    operation TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT,
    reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(memory_id, revision_no)
);
CREATE INDEX IF NOT EXISTS idx_memory_revisions_memory ON memory_revisions(memory_id, revision_no DESC);

CREATE TABLE IF NOT EXISTS memory_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id TEXT NOT NULL,
    message_id INTEGER,
    feedback_type TEXT NOT NULL,
    query_text TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_feedback_memory ON memory_feedback(memory_id, created_at DESC);

CREATE TABLE IF NOT EXISTS retrieval_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    message_id INTEGER,
    query_text TEXT NOT NULL,
    query_type TEXT,
    gate TEXT,
    candidates_json TEXT,
    selected_json TEXT,
    rejected_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_retrieval_events_session ON retrieval_events(session_id, created_at DESC);

CREATE TABLE IF NOT EXISTS memory_governance_items (
    item_id TEXT PRIMARY KEY,
    item_type TEXT NOT NULL,
    primary_memory_id TEXT NOT NULL,
    related_memory_id TEXT,
    priority REAL DEFAULT 0,
    status TEXT DEFAULT 'open',
    reason TEXT,
    detail_json TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_memory_governance_status ON memory_governance_items(status, priority DESC);
