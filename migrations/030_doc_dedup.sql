-- 030: 文档内容去重——raw_docs 增加 content_hash 列 + 索引
-- 摄入时按 SHA256 内容哈希检查是否已存在相同内容，避免同一文档/URL 重复解析与提炼。
-- 历史记录 content_hash 为 NULL，不影响向后兼容；新导入文档自动计算并写入哈希。
ALTER TABLE raw_docs
ADD COLUMN content_hash TEXT;
CREATE INDEX IF NOT EXISTS idx_raw_docs_content_hash ON raw_docs(content_hash);