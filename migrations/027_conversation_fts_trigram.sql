-- 027_conversation_fts_trigram.sql
-- 将 conversations_fts 从默认 porter tokenizer 重建为 trigram tokenizer，
-- 提升中文全文检索精度（从单字匹配升级为 3-gram 词组匹配）。

-- 步骤 1：删除旧触发器（external content 模式触发器不自动删除）
DROP TRIGGER IF EXISTS conv_ai;
DROP TRIGGER IF EXISTS conv_ad;

-- 步骤 2：删除旧 FTS 表（external content 模式，数据在主表 conversations，不丢数据）
DROP TABLE IF EXISTS conversations_fts;

-- 步骤 3：用 trigram tokenizer 重建
-- trigram：中文 3-gram 级分词（等效 2-3 字自然词组），英文按 3-gram
-- session_id/role 标记为 UNINDEXED 以减小索引体积
CREATE VIRTUAL TABLE conversations_fts USING fts5(
    content,
    session_id UNINDEXED,
    role UNINDEXED,
    content = conversations,
    content_rowid = id,
    tokenize = 'trigram'
);

-- 步骤 4：重建触发器（AFTER INSERT/DELETE 自动同步）
CREATE TRIGGER IF NOT EXISTS conv_ai
AFTER INSERT ON conversations BEGIN
    INSERT INTO conversations_fts(rowid, content, session_id, role)
    VALUES (new.id, new.content, new.session_id, new.role);
END;

CREATE TRIGGER IF NOT EXISTS conv_ad
AFTER DELETE ON conversations BEGIN
    INSERT INTO conversations_fts(conversations_fts, rowid, content, session_id, role)
    VALUES ('delete', old.id, old.content, old.session_id, old.role);
END;

-- 步骤 5：重建已有数据索引（全量，仅普通消息）
INSERT INTO conversations_fts(rowid, content, session_id, role)
SELECT id, content, session_id, role FROM conversations
WHERE message_type = 'normal';
