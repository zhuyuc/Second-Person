-- ============================================================
-- 044 项目工作区（Projects）
-- - 新表 projects / session_policy_events / fs_observations
-- - sessions / memories / raw_docs / local_dirs / 图谱四表 加 project_id
-- - memories_fts 重建含 project_id 列（应用层双写）
-- - graph_layout 复合 PK 迁移（(project_id, entity_id)）
-- 历史数据全部 project_id=NULL（= 全局记忆 / 无项目会话），零回归
-- ============================================================

-- 项目主表
CREATE TABLE IF NOT EXISTS projects (
    id            TEXT PRIMARY KEY,             -- proj_[0-9a-f]{8}
    path          TEXT NOT NULL,                -- realpath 归一化（正斜杠、原大小写）
    path_key      TEXT NOT NULL,                -- 归一化比较键（小写盘符）
    title         TEXT NOT NULL,                -- 默认 basename(path)
    display_order INTEGER NOT NULL,             -- 越小越靠上
    sandbox_mode  TEXT NOT NULL DEFAULT 'workspace-write',
                  -- read-only / workspace-write / danger-full-access
    ignore_extra  TEXT,                         -- JSON 数组
    status        TEXT NOT NULL DEFAULT 'active',   -- active / archived
    archived_at   TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE(path_key)
);
CREATE INDEX IF NOT EXISTS idx_projects_order  ON projects(display_order);
CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status, display_order);

-- 会话内沙箱档位变更事件（可 replay）
CREATE TABLE IF NOT EXISTS session_policy_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    event_type  TEXT NOT NULL,        -- sandbox_mode_change
    payload     TEXT NOT NULL,        -- JSON: {mode, reason}
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spe_session ON session_policy_events(session_id, id);

-- 文件观察缓存：fs_read 后记录 version，fs_write/edit 版本乐观锁
CREATE TABLE IF NOT EXISTS fs_observations (
    session_id  TEXT NOT NULL,
    target_key  TEXT NOT NULL,        -- realpath 归一化
    version     TEXT NOT NULL,        -- {st_ino}:{st_size}:{st_mtime_ns}
    observed_at TEXT NOT NULL,
    PRIMARY KEY (session_id, target_key)
);
CREATE INDEX IF NOT EXISTS idx_fs_obs_session ON fs_observations(session_id);

-- 会话表增字段（SQLite ALTER 无 IF NOT EXISTS，本项目通过 schema_migrations 幂等，
-- 单次应用不重复；应用失败可从此处开始逐条 rollback 手工修）
ALTER TABLE sessions ADD COLUMN project_id      TEXT;
ALTER TABLE sessions ADD COLUMN archived        INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN archived_source TEXT;   -- project / manual
ALTER TABLE sessions ADD COLUMN archived_at     TEXT;
ALTER TABLE sessions ADD COLUMN sandbox_mode    TEXT;   -- NULL = 继承项目档
CREATE INDEX IF NOT EXISTS idx_sessions_project  ON sessions(project_id, last_active);
CREATE INDEX IF NOT EXISTS idx_sessions_archived ON sessions(archived, project_id);

-- 记忆
ALTER TABLE memories ADD COLUMN project_id TEXT;
CREATE INDEX IF NOT EXISTS idx_mem_project ON memories(project_id, lifecycle);

-- 知识素材
ALTER TABLE raw_docs   ADD COLUMN project_id TEXT;
ALTER TABLE local_dirs ADD COLUMN project_id TEXT;
CREATE INDEX IF NOT EXISTS idx_raw_project ON raw_docs(project_id);
CREATE INDEX IF NOT EXISTS idx_ld_project  ON local_dirs(project_id);

-- 图谱表加列（graph_layout 的 PK 迁移放最后单事务处理）
ALTER TABLE memory_entities     ADD COLUMN project_id TEXT;
ALTER TABLE memory_entity_links ADD COLUMN project_id TEXT;
ALTER TABLE memory_links        ADD COLUMN project_id TEXT;
CREATE INDEX IF NOT EXISTS idx_ent_project      ON memory_entities(project_id);
CREATE INDEX IF NOT EXISTS idx_entlinks_project ON memory_entity_links(project_id);
CREATE INDEX IF NOT EXISTS idx_memlinks_project ON memory_links(project_id);

-- memories_fts 重建含 project_id 列（应用层双写；FTS5 不支持 ALTER）
DROP TABLE IF EXISTS memories_fts;
CREATE VIRTUAL TABLE memories_fts USING fts5(
    memory_id, project_id UNINDEXED, title, summary, detail, domain
);
-- 从 memories 回灌 title/summary/domain（detail 需 md 文件层重建）
INSERT INTO memories_fts(memory_id, project_id, title, summary, detail, domain)
    SELECT id, project_id, title, COALESCE(summary,''), '', domain FROM memories;

-- graph_layout 加 project_id 列
-- 注：entity_id 的项目归属已由 memory.naming.entity_id(name, disamb, project_id)
-- 通过 sha1 哈希隔离——同名实体在不同项目自然得到不同 entity_id，
-- 因此 graph_layout 的 PK 保持单列 entity_id 即可；这里补一列冗余方便查询过滤。
ALTER TABLE graph_layout ADD COLUMN project_id TEXT;
CREATE INDEX IF NOT EXISTS idx_graph_layout_project ON graph_layout(project_id);
