-- 领域中文标签缓存（方案 B）：新领域首次出现时 LLM 翻译一次并入库
CREATE TABLE IF NOT EXISTS domain_labels (
    domain TEXT PRIMARY KEY,
    -- 原始领域名（英文 slug，与 memories.domain 一致）
    label TEXT NOT NULL,
    -- 中文展示标签
    source TEXT DEFAULT 'llm',
    -- seed=内置种子 / llm=模型翻译
    created_at TEXT
);