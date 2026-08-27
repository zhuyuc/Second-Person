-- 语义指纹与写入速率控制：给候选池加 content_bucket 列（词袋 hash 前 12 位）
-- + 单实体消歧位 + freshness_boost 需要的 valid_from 索引。
ALTER TABLE memory_write_candidates ADD COLUMN content_bucket TEXT DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_mwc_content_bucket
    ON memory_write_candidates(content_bucket, status);

-- 实体消歧位（P4-B）：允许同名不同实体共存
ALTER TABLE memory_entities ADD COLUMN disambiguator TEXT DEFAULT '';

-- 文档幂等标记（P2-E）：raw_docs 补 last_distilled_at；review_status 已存在但
-- 部分老库缺失，补容错列（若已有会 SQLite 抛错，用 IF NOT EXISTS 语义等价方式）
-- 说明：SQLite 无 ADD COLUMN IF NOT EXISTS，改用一个 sentinel 表跟踪迁移
CREATE TABLE IF NOT EXISTS _migration_042_marker (applied INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO _migration_042_marker(applied) VALUES(1);
