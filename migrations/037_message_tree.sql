-- 037: 消息树形结构（编辑 & 重新生成分支化）
-- parent_id: 树中父消息 ID，根消息为 NULL
-- version_group_id: 版本组 = 该位置首条消息 id，同组为编辑/重试产生的兄弟
-- is_active: 同组中当前展示的版本（1=活跃，0=非活跃）

ALTER TABLE conversations ADD COLUMN parent_id INTEGER REFERENCES conversations(id);
ALTER TABLE conversations ADD COLUMN version_group_id INTEGER;
ALTER TABLE conversations ADD COLUMN is_active INTEGER DEFAULT 1;

CREATE INDEX IF NOT EXISTS idx_conv_parent ON conversations(parent_id);
CREATE INDEX IF NOT EXISTS idx_conv_vgroup ON conversations(version_group_id);

-- 历史数据迁移：为已有消息补充 parent_id 链和 version_group_id
-- parent_id: 每条消息指向同 session 中前一条消息（按 id 顺序）
-- version_group_id: 历史消息无分支，组 id = 自身 id
-- is_active: 全部为 1（全部活跃）
UPDATE conversations SET
    version_group_id = id,
    is_active = 1;

UPDATE conversations SET parent_id = (
    SELECT c2.id FROM conversations c2
    WHERE c2.session_id = conversations.session_id
      AND c2.id < conversations.id
    ORDER BY c2.id DESC LIMIT 1
);
