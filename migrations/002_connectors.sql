-- ============================================================
-- 002：MCP 连接器配置表（产品文档 §外部系统连接器 / 开发文档 §3.5）
-- 说明：v5.1 DDL 列举了 API 但未给出 connectors 建表，此处补齐。
-- env 中的敏感值加密存入 credentials 表（credential_type='connector'），
-- 本表 config 只存非敏感部分 + credential_id 引用。
-- ============================================================
CREATE TABLE IF NOT EXISTS connectors (
    id TEXT PRIMARY KEY,
    -- conn_xxxxxx
    name TEXT NOT NULL,
    transport TEXT NOT NULL,
    -- stdio / http
    config TEXT NOT NULL,
    -- JSON：stdio(command/args) 或 http(url/headers/auth)
    credential_id INTEGER,
    -- 加密的 env / auth 敏感值
    timeout INTEGER DEFAULT 120,
    tools_filter TEXT,
    -- JSON：{include:[], exclude:[]}
    status TEXT DEFAULT 'connected',
    -- connected / disabled
    tools_cache TEXT,
    -- JSON：最近一次 tools/list 结果
    created_at TEXT
);
-- OAuth state 暂存（5 分钟有效）
CREATE TABLE IF NOT EXISTS oauth_states (
    state TEXT PRIMARY KEY,
    connector_id TEXT,
    created_at TEXT
);