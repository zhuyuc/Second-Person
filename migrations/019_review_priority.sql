-- 019: review_candidates 表增加 priority 列
-- 支持约束类候选优先处理（priority=1 表示高优先级，0 默认）
ALTER TABLE review_candidates ADD COLUMN priority INTEGER DEFAULT 0;
