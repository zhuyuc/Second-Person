-- 028_protected_messages.sql
-- 关键内容防压缩机制：conversations 表新增 protected_from_compression 字段，
-- 标记后被压缩流程跳过，确保方案/代码/长文本等关键内容不在压缩时丢失。

ALTER TABLE conversations ADD COLUMN protected_from_compression INTEGER NOT NULL DEFAULT 0;

CREATE INDEX idx_conversations_protected ON conversations(session_id, protected_from_compression, id);
