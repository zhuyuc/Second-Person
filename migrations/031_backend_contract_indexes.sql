-- 031：后端契约与性能索引补齐
-- 补齐本轮后端健康审计发现的高频查询索引与去重约束。
-- content_hash 历史数据可能存在 NULL/空值或重复；不直接创建 UNIQUE INDEX，
-- 改用触发器约束新写入/回填的有效哈希，避免迁移因历史脏数据失败。
CREATE INDEX IF NOT EXISTS idx_response_signals_message_id ON response_signals(message_id);
CREATE INDEX IF NOT EXISTS idx_task_logs_task_time ON task_logs(task_id, run_time DESC);
CREATE INDEX IF NOT EXISTS idx_raw_docs_source_url ON raw_docs(source, source_url);
CREATE TRIGGER IF NOT EXISTS trg_raw_docs_content_hash_unique_insert BEFORE
INSERT ON raw_docs
    WHEN NEW.content_hash IS NOT NULL
    AND NEW.content_hash != '' BEGIN
SELECT RAISE(ABORT, 'duplicate raw_docs.content_hash')
WHERE EXISTS (
        SELECT 1
        FROM raw_docs
        WHERE content_hash = NEW.content_hash
    );
END;
CREATE TRIGGER IF NOT EXISTS trg_raw_docs_content_hash_unique_update BEFORE
UPDATE OF content_hash ON raw_docs
    WHEN NEW.content_hash IS NOT NULL
    AND NEW.content_hash != '' BEGIN
SELECT RAISE(ABORT, 'duplicate raw_docs.content_hash')
WHERE EXISTS (
        SELECT 1
        FROM raw_docs
        WHERE content_hash = NEW.content_hash
            AND id != NEW.id
    );
END;